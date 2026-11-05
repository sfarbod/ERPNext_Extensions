# Copyright (c) 2026, ERPNext Extensions contributors
"""Process one campaign item: source rebuild → Core RIV → settle → TVA."""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext.controllers.stock_controller import create_item_wise_repost_entries
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	execute_reposting_entry,
)

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_BLOCKED,
	ITEM_COMPLETED,
	ITEM_FAILED,
	ITEM_PROCESSING,
	ITEM_RIV_LINKED,
	ITEM_SKIPPED,
	ITEM_SOURCE,
	MAX_ITEM_RETRIES,
	RIV_SETTLE_MAX_WAITS,
	RIV_SETTLE_POLL_SECONDS,
	SOURCE_BLOCKED,
	SOURCE_DONE,
	SOURCE_FAILED,
	SOURCE_NOT_REQUIRED,
	TVA_NA,
)
from erpnext_extensions.iran_accounting.historical_rebuild.source_rebuild import (
	apply_source_reconstruction,
	diagnose_source_needs,
)
from erpnext_extensions.iran_accounting.historical_rebuild.tva_reconcile import (
	reconcile_tva_for_voucher,
)


def _parse_riv_names(raw) -> list[str]:
	if not raw:
		return []
	if isinstance(raw, list):
		return [str(x) for x in raw if x]
	try:
		data = json.loads(raw)
		if isinstance(data, list):
			return [str(x) for x in data if x]
	except Exception:
		pass
	return [p.strip() for p in str(raw).split(",") if p.strip()]


def find_covering_riv(item_code: str, warehouse: str, posting_date, posting_time) -> str | None:
	"""Return a Completed Core RIV covering this identity from an earlier/equal boundary.

	Coverage requires RIV posting datetime <= voucher SLE posting datetime.
	Same-day RIV that starts after the voucher does NOT cover it.
	"""
	pt = str(posting_time or "00:00:00")
	rows = frappe.db.sql(
		"""
		select name from `tabRepost Item Valuation`
		where docstatus=1
		  and based_on='Item and Warehouse'
		  and item_code=%s and warehouse=%s
		  and status='Completed'
		  and (
		    posting_date < %s
		    or (posting_date = %s and posting_time <= %s)
		  )
		order by posting_date asc, posting_time asc, creation asc
		limit 1
		""",
		(item_code, warehouse, posting_date, posting_date, pt),
	)
	return rows[0][0] if rows else None


def _sle_identities(voucher_type: str, voucher_no: str) -> list[dict]:
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"is_cancelled": 0,
		},
		fields=["item_code", "warehouse", "posting_date", "posting_time"],
		limit_page_length=0,
	)


def create_or_link_rivs(voucher_type: str, voucher_no: str) -> dict[str, Any]:
	"""Create Item+WH RIVs via Core only for identities not already covered."""
	from erpnext.controllers.stock_controller import create_repost_item_valuation_entry

	identities = _sle_identities(voucher_type, voucher_no)
	if not identities:
		return {"riv_names": [], "skipped_covered": False, "covering": None, "detail": "no SLE"}

	from erpnext_extensions.iran_accounting.historical_rebuild.failure_registry import (
		is_blocked_identity,
		record_failure,
	)

	# Reject-only SED valued / SLE still zero: a chronologically covering Completed
	# RIV must not skip — the valuation source changed after that RIV finished.
	force_keys: set[tuple[str, str]] = set()
	if voucher_type == "Stock Entry":
		try:
			from erpnext_extensions.iran_accounting.domain.reject_only_riv_rate import (
				clear_reject_only_riv_cache,
				reject_only_sed_sle_lag_identities,
			)

			clear_reject_only_riv_cache()
			for lag in reject_only_sed_sle_lag_identities(voucher_no):
				force_keys.add((lag["item_code"], lag["warehouse"]))
		except Exception:
			force_keys = set()

	covers: list[str] = []
	need_create: list[dict] = []
	blocked_ids: list[tuple[str, str]] = []
	seen: set[tuple[str, str]] = set()
	for sle in identities:
		key = (sle.item_code, sle.warehouse)
		if key in seen:
			continue
		seen.add(key)
		force_fresh = key in force_keys
		cov = find_covering_riv(sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time)
		if cov and not force_fresh:
			covers.append(cov)
			continue
		# Also reuse Queued/In Progress covering same identity at earlier/equal boundary.
		open_cov = frappe.db.sql(
			"""
			select name from `tabRepost Item Valuation`
			where docstatus=1 and based_on='Item and Warehouse'
			  and item_code=%s and warehouse=%s
			  and status in ('Queued', 'In Progress')
			  and posting_date <= %s
			order by posting_date asc, creation asc limit 1
			""",
			(sle.item_code, sle.warehouse, sle.posting_date),
		)
		if open_cov and not force_fresh:
			covers.append(open_cov[0][0])
			continue
		# Never create replacement RIV for an unresolved Failed identity.
		if is_blocked_identity(sle.item_code, sle.warehouse):
			blocked_ids.append(key)
			record_failure(
				item_code=sle.item_code,
				warehouse=sle.warehouse,
				riv_name=None,
				error="blocked: prior Failed RIV — explicit repair required",
				posting_date=sle.posting_date,
				category="blocked_prior_failure",
				voucher_hint=f"{voucher_type}/{voucher_no}",
			)
			continue
		# Tag forced identities so the create loop does not reuse Completed RIVs.
		if force_fresh:
			sle = frappe._dict(sle)
			sle["_iran_force_fresh_riv"] = True
		need_create.append(sle)

	if blocked_ids and not need_create and not covers:
		return {
			"riv_names": [],
			"skipped_covered": False,
			"covering": None,
			"blocked_identities": blocked_ids,
			"detail": f"blocked {len(blocked_ids)} failed identities — no new RIV",
		}

	if not need_create and covers:
		return {
			"riv_names": list(dict.fromkeys(covers)),
			"skipped_covered": True,
			"covering": covers[0],
			"detail": f"all {len(seen)} identities covered",
		}

	created_names: list[str] = []
	# Large multi-identity vouchers (e.g. Opening Stock Reconciliation): one
	# Transaction RIV is far cheaper than hundreds of Item+WH jobs.
	if len(need_create) > 15:
		try:
			existing_txn = frappe.db.get_value(
				"Repost Item Valuation",
				{
					"based_on": "Transaction",
					"voucher_type": voucher_type,
					"voucher_no": voucher_no,
					"docstatus": 1,
					"status": ("in", ["Queued", "In Progress", "Completed", "Skipped"]),
				},
				"name",
				order_by="creation desc",
			)
			if existing_txn:
				created_names.append(existing_txn)
			else:
				create_repost_item_valuation_entry(
					frappe._dict(
						based_on="Transaction",
						voucher_type=voucher_type,
						voucher_no=voucher_no,
						allow_zero_rate=False,
					)
				)
				name = frappe.db.get_value(
					"Repost Item Valuation",
					{
						"based_on": "Transaction",
						"voucher_type": voucher_type,
						"voucher_no": voucher_no,
						"status": ("in", ["Queued", "In Progress", "Completed"]),
					},
					"name",
					order_by="creation desc",
				)
				if name:
					created_names.append(name)
		except Exception:
			docs = create_item_wise_repost_entries(voucher_type, voucher_no) or []
			created_names.extend([d.name for d in docs if getattr(d, "name", None)])
	else:
		for sle in need_create:
			try:
				force_fresh = bool(
					sle.get("_iran_force_fresh_riv")
					if hasattr(sle, "get")
					else getattr(sle, "_iran_force_fresh_riv", False)
				)
				before = frappe.db.get_value(
					"Repost Item Valuation",
					{
						"based_on": "Item and Warehouse",
						"item_code": sle.item_code,
						"warehouse": sle.warehouse,
						"posting_date": sle.posting_date,
						"status": ("in", ["Queued", "In Progress", "Completed"]),
					},
					["name", "status"],
					as_dict=True,
					order_by="creation desc",
				)
				# Reuse Queued/In Progress always. Reuse Completed only when the
				# valuation source has not changed (no reject-only SED→SLE lag).
				if before:
					if before.status in ("Queued", "In Progress"):
						created_names.append(before.name)
						continue
					if before.status == "Completed" and not force_fresh:
						created_names.append(before.name)
						continue
				create_repost_item_valuation_entry(
					frappe._dict(
						based_on="Item and Warehouse",
						item_code=sle.item_code,
						warehouse=sle.warehouse,
						posting_date=sle.posting_date,
						posting_time=sle.posting_time,
						allow_zero_rate=False,
					)
				)
				name = frappe.db.get_value(
					"Repost Item Valuation",
					{
						"based_on": "Item and Warehouse",
						"item_code": sle.item_code,
						"warehouse": sle.warehouse,
						"posting_date": sle.posting_date,
						"status": "Queued",
					},
					"name",
					order_by="creation desc",
				)
				if name:
					created_names.append(name)
			except Exception:
				docs = create_item_wise_repost_entries(voucher_type, voucher_no) or []
				created_names.extend([d.name for d in docs if getattr(d, "name", None)])
				break

	all_names = list(dict.fromkeys(created_names + covers))
	return {
		"riv_names": all_names,
		"skipped_covered": False,
		"covering": covers[0] if covers else None,
		"detail": f"created={len(created_names)} reused={len(covers)} need={len(need_create)}",
	}


def execute_linked_rivs(riv_names: list[str], *, stop_check=None) -> dict[str, Any]:
	"""Drive Core RIV execution without relying on schedulers (Maintenance Mode safe)."""
	results = {"completed": [], "failed": [], "skipped": [], "pending": []}
	for name in riv_names:
		if stop_check and stop_check():
			results["pending"].append(name)
			break
		if not frappe.db.exists("Repost Item Valuation", name):
			results["failed"].append(name)
			continue
		st = frappe.db.get_value("Repost Item Valuation", name, "status")
		if st in ("Completed", "Skipped"):
			results["completed" if st == "Completed" else "skipped"].append(name)
			continue
		if st == "Failed":
			results["failed"].append(name)
			continue
		try:
			# Ensure Iran patches apply for worker-less sync execute.
			from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

			apply_iran()
			execute_reposting_entry(name, continue_reposting=False)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			# Recoverable Core timeouts leave In Progress — resume later.
			frappe.log_error(title=f"HRC RIV execute {name}", message=str(e)[:500])

	# Settle poll
	pending = list(riv_names)
	waits = 0
	while pending and waits < RIV_SETTLE_MAX_WAITS:
		if stop_check and stop_check():
			break
		still = []
		for name in pending:
			st = frappe.db.get_value("Repost Item Valuation", name, "status")
			if st == "Completed":
				if name not in results["completed"]:
					results["completed"].append(name)
			elif st == "Skipped":
				if name not in results["skipped"]:
					results["skipped"].append(name)
			elif st == "Failed":
				if name not in results["failed"]:
					results["failed"].append(name)
			else:
				# Still queued/in progress — try execute again if Queued.
				if st == "Queued":
					try:
						from erpnext_extensions.iran_accounting.integration.bootstrap import (
							apply as apply_iran,
						)

						apply_iran()
						execute_reposting_entry(name, continue_reposting=False)
						frappe.db.commit()
					except Exception:
						frappe.db.rollback()
				still.append(name)
		pending = still
		if not pending:
			break
		waits += 1
		time.sleep(RIV_SETTLE_POLL_SECONDS)

	results["pending"] = pending
	return results


def _is_fixed_asset_only_purchase_receipt(voucher_no: str) -> bool:
	"""True when PR has items and every item is a fixed asset (no stock SLE expected)."""
	rows = frappe.db.sql(
		"""
		select count(*) as n,
		       sum(case when ifnull(is_fixed_asset,0)=1 then 1 else 0 end) as assets
		from `tabPurchase Receipt Item`
		where parent=%s
		""",
		(voucher_no,),
		as_dict=True,
	)
	if not rows or not cint(rows[0].n):
		return False
	return cint(rows[0].n) == cint(rows[0].assets)


def _is_non_stock_only_purchase_receipt(voucher_no: str) -> bool:
	"""True when every PR item is a non-stock Item (no SLE expected)."""
	rows = frappe.db.sql(
		"""
		select count(*) as n,
		       sum(case when ifnull(i.is_stock_item,0)=0 then 1 else 0 end) as non_stock
		from `tabPurchase Receipt Item` pri
		left join `tabItem` i on i.name=pri.item_code
		where pri.parent=%s
		""",
		(voucher_no,),
		as_dict=True,
	)
	if not rows or not cint(rows[0].n):
		return False
	return cint(rows[0].n) == cint(rows[0].non_stock)


def manufacture_product_reject_rate_issues(voucher_no: str) -> list[dict[str, Any]]:
	"""Detect illegal zero MAIN_PRODUCT_REJECT rates on a Manufacture voucher.

	Coverage / Completed RIV alone must not certify such rows as verified.
	"""
	purpose = frappe.db.get_value("Stock Entry", voucher_no, "purpose")
	if purpose != "Manufacture":
		return []
	try:
		from erpnext_extensions.iran_accounting.scrap_costing import (
			CLASS_MAIN_PRODUCT_REJECT,
			classify_manufacture_outputs,
			historical_zero_product_reject_needs_repair,
			historical_zero_reject_only_needs_repair,
		)

		doc = frappe.get_doc("Stock Entry", voucher_no)
		fg_reject = historical_zero_product_reject_needs_repair(doc)
		reject_only = historical_zero_reject_only_needs_repair(doc)
		if not fg_reject and not reject_only:
			return []

		classified = classify_manufacture_outputs(doc)
		reason = (
			"zero_reject_only_manufacture_rate" if reject_only and not fg_reject else "zero_main_product_reject_rate"
		)
		issues = []
		for row in classified.get(CLASS_MAIN_PRODUCT_REJECT) or []:
			if cint(row.get("allow_zero_valuation_rate")):
				continue
			if abs(flt(row.get("basic_rate"))) > 1e-9 or abs(flt(row.get("valuation_rate"))) > 1e-9:
				continue
			qty = flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
			if qty <= 0:
				continue
			issues.append(
				{
					"sed": row.get("name"),
					"idx": row.get("idx"),
					"item_code": row.get("item_code"),
					"qty": qty,
					"basic_rate": row.get("basic_rate"),
					"reason": reason,
				}
			)
		return issues
	except Exception:
		return []


def verify_voucher_basic(voucher_type: str, voucher_no: str) -> dict[str, Any]:
	"""Lightweight post-rebuild checks (non-destructive)."""
	sle_count = cint(
		frappe.db.count(
			"Stock Ledger Entry",
			{"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		)
	)
	gl_count = cint(
		frappe.db.count(
			"GL Entry",
			{"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		)
	)
	detail = f"sle={sle_count} gl={gl_count}"
	ok = sle_count > 0
	if not ok and voucher_type == "Purchase Receipt":
		if _is_fixed_asset_only_purchase_receipt(voucher_no):
			# Fixed-asset PRs post asset/GL only — no SLE by design.
			ok = gl_count > 0
			detail += "; fixed_asset_pr"
		elif _is_non_stock_only_purchase_receipt(voucher_no):
			# Service / non-stock PRs never create SLE; GL may land on the PI.
			ok = True
			detail += "; non_stock_pr"
	# Optional Iran determinism for SE/SR
	if voucher_type in ("Stock Entry", "Stock Reconciliation") and ok:
		try:
			from erpnext_extensions.iran_accounting.domain.repost_determinism import (
				validate_deterministic_state_after_repost,
			)

			doc = frappe.get_doc(voucher_type, voucher_no)
			validate_deterministic_state_after_repost(doc, raise_on_fail=False)
			detail += "; determinism_checked"
		except Exception as e:
			detail += f"; determinism_skip:{e}"
	# Manufacture: zero MAIN_PRODUCT_REJECT with qty>0 is not financially verified.
	if voucher_type == "Stock Entry" and ok:
		reject_issues = manufacture_product_reject_rate_issues(voucher_no)
		if reject_issues:
			ok = False
			detail += f"; zero_product_reject={len(reject_issues)}"
		# SED valued but Reject SLE still zero after historical repair — not covered.
		try:
			from erpnext_extensions.iran_accounting.domain.reject_only_riv_rate import (
				clear_reject_only_riv_cache,
				reject_only_sed_sle_lag_identities,
			)

			clear_reject_only_riv_cache()
			lags = reject_only_sed_sle_lag_identities(voucher_no)
			if lags:
				ok = False
				detail += f"; reject_only_sed_sle_lag={len(lags)}"
		except Exception:
			pass
		# Reject-only accounting evidence: Reject inventory GL debit ≈ SLE value;
		# Stock Adjustment must not still hold the full consume pool.
		try:
			gl_ev = _reject_only_gl_evidence(voucher_no)
			if gl_ev and not gl_ev.get("ok"):
				ok = False
				detail += f"; reject_only_gl:{gl_ev.get('reason')}"
		except Exception:
			pass
	return {"ok": ok, "detail": detail}


def _reject_only_gl_evidence(voucher_no: str) -> dict[str, Any] | None:
	"""Return None when not reject-only; else ok/reason for Reject GL vs SLE.

	Requires:
	* GL balanced for the Stock Entry
	* Reject warehouse inventory account debit ≈ incoming Reject SLE SVD
	* Stock Adjustment absolute net ≤ max(legitimate residual, 1) — not full pool
	"""
	from erpnext_extensions.iran_accounting.scrap_costing import _has_reject_only

	if frappe.db.get_value("Stock Entry", voucher_no, "purpose") != "Manufacture":
		return None
	doc = frappe.get_doc("Stock Entry", voucher_no)
	if not _has_reject_only(doc):
		return None

	company = doc.company
	sa = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	gl = frappe.db.sql(
		"""
		select account, sum(debit) d, sum(credit) c
		from `tabGL Entry`
		where voucher_type='Stock Entry' and voucher_no=%s and ifnull(is_cancelled,0)=0
		group by account
		""",
		voucher_no,
		as_dict=True,
	) or []
	if not gl:
		return {"ok": False, "reason": "no_gl"}
	debit = sum(flt(r.d) for r in gl)
	credit = sum(flt(r.c) for r in gl)
	if abs(debit - credit) > 0.5:
		return {"ok": False, "reason": f"gl_unbalanced:{debit-credit}"}

	from erpnext_extensions.iran_accounting.scrap_costing import (
		CLASS_MAIN_PRODUCT_REJECT,
		classify_manufacture_outputs,
	)

	rejects = classify_manufacture_outputs(doc).get(CLASS_MAIN_PRODUCT_REJECT) or []
	sle_value = 0.0
	reject_accounts: set[str] = set()
	for row in rejects:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			{
				"voucher_type": "Stock Entry",
				"voucher_no": voucher_no,
				"voucher_detail_no": row.name,
				"is_cancelled": 0,
				"actual_qty": (">", 0),
			},
			["stock_value_difference", "warehouse"],
			as_dict=True,
		)
		if not sle:
			return {"ok": False, "reason": f"missing_reject_sle:{row.name}"}
		sle_value += flt(sle.stock_value_difference)
		try:
			from erpnext.stock.doctype.warehouse.warehouse import get_warehouse_account

			acct = get_warehouse_account(
				frappe.get_cached_doc("Warehouse", sle.warehouse), company
			)
		except Exception:
			acct = frappe.db.get_value("Warehouse", sle.warehouse, "account")
		if acct:
			reject_accounts.add(acct)

	if sle_value <= 0.5:
		return {"ok": False, "reason": "reject_sle_value_zero"}

	rej_debit = sum(flt(r.d) for r in gl if r.account in reject_accounts)
	if abs(rej_debit - sle_value) > 0.5:
		return {
			"ok": False,
			"reason": f"reject_gl_debit_mismatch:gl={rej_debit},sle={sle_value}",
		}

	consume = sum(
		flt(r.basic_amount) for r in (doc.get("items") or []) if r.get("s_warehouse")
	)
	sa_net = 0.0
	if sa:
		sa_row = next((r for r in gl if r.account == sa), None)
		sa_net = flt(sa_row.d if sa_row else 0) - flt(sa_row.c if sa_row else 0)
	# Full-pool SA (pre-fix) is illegal once Reject carries the value.
	if consume > 0.5 and abs(sa_net) > 0.5 and abs(abs(sa_net) - consume) <= 0.5:
		return {"ok": False, "reason": f"full_pool_stock_adjustment:{sa_net}"}
	return {
		"ok": True,
		"reason": "reject_gl_ok",
		"reject_gl_debit": rej_debit,
		"sle_value": sle_value,
		"sa_net": sa_net,
	}


def process_campaign_item(item_name: str, *, stop_check=None) -> dict[str, Any]:
	"""Full pipeline for one Historical Rebuild Campaign Item."""
	item = frappe.get_doc("Historical Rebuild Campaign Item", item_name)
	voucher_type = item.voucher_type
	voucher_no = item.voucher_no
	out: dict[str, Any] = {
		"item": item_name,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"final_status": None,
	}

	frappe.db.set_value(
		"Historical Rebuild Campaign Item",
		item_name,
		{
			"item_status": ITEM_PROCESSING,
			"last_processed_at": now_datetime(),
			"retry_count": cint(item.retry_count) + 1,
		},
		update_modified=False,
	)
	frappe.db.commit()

	if cint(item.retry_count) + 1 > MAX_ITEM_RETRIES and item.item_status == ITEM_FAILED:
		# allow one more cycle via explicit resume; still process
		pass

	# 1) Source reconstruction
	frappe.db.set_value(
		"Historical Rebuild Campaign Item",
		item_name,
		"item_status",
		ITEM_SOURCE,
		update_modified=False,
	)
	diag = diagnose_source_needs(voucher_type, voucher_no)
	src = {"status": SOURCE_NOT_REQUIRED, "detail": diag.get("detail") or ""}
	if diag.get("required"):
		src = apply_source_reconstruction(voucher_type, voucher_no)
	frappe.db.set_value(
		"Historical Rebuild Campaign Item",
		item_name,
		{
			"source_rebuild_status": src.get("status") or SOURCE_NOT_REQUIRED,
			"source_rebuild_detail": (src.get("detail") or "")[:1000],
		},
		update_modified=False,
	)
	frappe.db.commit()

	if src.get("status") == SOURCE_BLOCKED and diag.get("required"):
		frappe.db.set_value(
			"Historical Rebuild Campaign Item",
			item_name,
			{
				"item_status": ITEM_BLOCKED,
				"error_message": src.get("detail") or "source blocked",
			},
			update_modified=False,
		)
		frappe.db.commit()
		out["final_status"] = ITEM_BLOCKED
		return out

	if src.get("status") == SOURCE_FAILED:
		frappe.db.set_value(
			"Historical Rebuild Campaign Item",
			item_name,
			{
				"item_status": ITEM_FAILED,
				"error_message": src.get("detail") or "source failed",
			},
			update_modified=False,
		)
		frappe.db.commit()
		out["final_status"] = ITEM_FAILED
		return out

	# 2) Core RIV create/link
	try:
		link = create_or_link_rivs(voucher_type, voucher_no)
	except Exception as e:
		frappe.db.rollback()
		frappe.db.set_value(
			"Historical Rebuild Campaign Item",
			item_name,
			{
				"item_status": ITEM_FAILED,
				"error_message": f"RIV create failed: {e}",
			},
			update_modified=False,
		)
		frappe.db.commit()
		out["final_status"] = ITEM_FAILED
		return out

	if link.get("blocked_identities") and not (link.get("riv_names") or []):
		reason = link.get("detail") or "blocked failed valuation identity"
		frappe.db.set_value(
			"Historical Rebuild Campaign Item",
			item_name,
			{
				"item_status": ITEM_BLOCKED,
				"error_message": reason[:1000],
			},
			update_modified=False,
		)
		frappe.db.commit()
		out["final_status"] = ITEM_BLOCKED
		out["blocked_identities"] = link.get("blocked_identities")
		return out

	riv_names = link.get("riv_names") or []
	frappe.db.set_value(
		"Historical Rebuild Campaign Item",
		item_name,
		{
			"item_status": ITEM_RIV_LINKED,
			"riv_names": json.dumps(riv_names),
			"covering_riv": link.get("covering"),
		},
		update_modified=False,
	)
	frappe.db.commit()

	# 3) Execute / settle (unless purely covered — still verify)
	if riv_names and not link.get("skipped_covered"):
		settle = execute_linked_rivs(riv_names, stop_check=stop_check)
		out["settle"] = settle
		if settle.get("failed") and not settle.get("completed") and not settle.get("skipped"):
			frappe.db.set_value(
				"Historical Rebuild Campaign Item",
				item_name,
				{
					"item_status": ITEM_FAILED,
					"error_message": f"RIV failed: {settle.get('failed')}",
				},
				update_modified=False,
			)
			frappe.db.commit()
			out["final_status"] = ITEM_FAILED
			return out
		if settle.get("pending"):
			# Interrupted / timeout — leave RIV Linked for resume
			frappe.db.set_value(
				"Historical Rebuild Campaign Item",
				item_name,
				{
					"item_status": ITEM_RIV_LINKED,
					"error_message": f"RIV pending settle: {settle.get('pending')}",
				},
				update_modified=False,
			)
			frappe.db.commit()
			out["final_status"] = ITEM_RIV_LINKED
			return out

	# 4) Basic voucher presence + 5) evidence-based accounting coverage
	# Completed / chronologically covering RIV alone is NEVER enough for Skipped Covered.
	ver = verify_voucher_basic(voucher_type, voucher_no)
	out["verify"] = ver
	if not ver.get("ok"):
		frappe.db.set_value(
			"Historical Rebuild Campaign Item",
			item_name,
			{
				"item_status": ITEM_FAILED,
				"error_message": f"verify failed: {ver.get('detail')}",
			},
			update_modified=False,
		)
		frappe.db.commit()
		out["final_status"] = ITEM_FAILED
		return out

	from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
		apply_verified_skip,
		evidence_payload,
		verify_voucher_coverage,
	)

	cov = verify_voucher_coverage(voucher_type, voucher_no, company=item.company)
	out["coverage"] = {
		"ok": cov.get("ok"),
		"reason": cov.get("reason"),
		"verification_class": cov.get("verification_class"),
	}
	tva = reconcile_tva_for_voucher(voucher_type, voucher_no)
	frappe.db.commit()

	if cov.get("ok"):
		apply_verified_skip(item_name, cov)
		frappe.db.commit()
		out["final_status"] = ITEM_SKIPPED
		out["tva"] = tva
		out["source"] = src
		out["link"] = link
		return out

	# RIV ran (or linked) but accounting evidence still dirty — Completed pending re-verify
	# or Failed when no RIV progress was made under false skipped_covered pretence.
	if link.get("skipped_covered") and not riv_names:
		# Previously would have marked Skipped Covered on Completed RIV alone — refuse.
		fields = {
			"item_status": ITEM_PENDING,
			"covering_riv": None,
			"tva_reconcile_status": tva.get("status") or TVA_NA,
			"error_message": f"coverage_insufficient:{cov.get('reason')}"[:1000],
			"last_processed_at": now_datetime(),
		}
		meta = frappe.get_meta("Historical Rebuild Campaign Item")
		if meta.has_field("verification_class"):
			fields["verification_class"] = cov.get("verification_class") or "REPOST_REQUIRED"
		if meta.has_field("verification_detail"):
			fields["verification_detail"] = evidence_payload(cov)
		frappe.db.set_value(
			"Historical Rebuild Campaign Item",
			item_name,
			fields,
			update_modified=False,
		)
		frappe.db.commit()
		out["final_status"] = ITEM_PENDING
		out["tva"] = tva
		out["source"] = src
		out["link"] = link
		return out

	final = ITEM_COMPLETED
	fields = {
		"item_status": final,
		"tva_reconcile_status": tva.get("status") or TVA_NA,
		"error_message": f"pending_verification:{cov.get('reason')}"[:1000],
		"last_processed_at": now_datetime(),
	}
	meta = frappe.get_meta("Historical Rebuild Campaign Item")
	if meta.has_field("verification_class"):
		fields["verification_class"] = "REPOST_COMPLETED_PENDING_VERIFICATION"
	if meta.has_field("verification_detail"):
		fields["verification_detail"] = evidence_payload(cov)
	frappe.db.set_value(
		"Historical Rebuild Campaign Item",
		item_name,
		fields,
		update_modified=False,
	)
	frappe.db.commit()
	out["final_status"] = final
	out["tva"] = tva
	out["source"] = src
	out["link"] = link
	return out
