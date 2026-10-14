# Copyright (c) 2026, ERPNext Extensions contributors
"""Process one campaign item: source rebuild → Core RIV → settle → TVA."""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

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
	"""Return a Completed Core RIV that already covers this identity from an earlier/equal boundary."""
	rows = frappe.db.sql(
		"""
		select name from `tabRepost Item Valuation`
		where docstatus=1
		  and based_on='Item and Warehouse'
		  and item_code=%s and warehouse=%s
		  and status='Completed'
		  and posting_date <= %s
		order by posting_date asc, posting_time asc, creation asc
		limit 1
		""",
		(item_code, warehouse, posting_date),
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

	covers: list[str] = []
	need_create: list[dict] = []
	seen: set[tuple[str, str]] = set()
	for sle in identities:
		key = (sle.item_code, sle.warehouse)
		if key in seen:
			continue
		seen.add(key)
		cov = find_covering_riv(sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time)
		if cov:
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
		if open_cov:
			covers.append(open_cov[0][0])
			continue
		need_create.append(sle)

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
				before = frappe.db.get_value(
					"Repost Item Valuation",
					{
						"based_on": "Item and Warehouse",
						"item_code": sle.item_code,
						"warehouse": sle.warehouse,
						"posting_date": sle.posting_date,
						"status": ("in", ["Queued", "In Progress", "Completed"]),
					},
					"name",
					order_by="creation desc",
				)
				if before:
					created_names.append(before)
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
	ok = sle_count > 0
	detail = f"sle={sle_count} gl={gl_count}"
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
	return {"ok": ok, "detail": detail}


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

	# 4) Verify
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

	# 5) TVA
	tva = reconcile_tva_for_voucher(voucher_type, voucher_no)
	frappe.db.commit()

	final = ITEM_SKIPPED if link.get("skipped_covered") else ITEM_COMPLETED
	frappe.db.set_value(
		"Historical Rebuild Campaign Item",
		item_name,
		{
			"item_status": final,
			"tva_reconcile_status": tva.get("status") or TVA_NA,
			"error_message": "",
			"last_processed_at": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	out["final_status"] = final
	out["tva"] = tva
	out["source"] = src
	out["link"] = link
	return out
