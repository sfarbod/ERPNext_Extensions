# Copyright (c) 2026, ERPNext Extensions contributors
"""ETO SCR valuation chain inspection and supported RIV orchestration."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate

from erpnext_extensions.iran_accounting.domain.eto_scr_repost_scope import (
	is_eto_finished_good,
	is_eto_subcontracting_receipt,
)
from erpnext_extensions.iran_accounting.eto_scr_valuation_propagation import (
	API_VERSION,
	CONFIRM_APPLY,
	MAX_ALLOWLIST,
	RATE_TOLERANCE,
)

TRANSFER_PURPOSES = frozenset({"Material Transfer for Manufacture"})
MANUFACTURE_PURPOSES = frozenset({"Manufacture"})


def require_privileged_user() -> None:
	roles = set(frappe.get_roles())
	if "Administrator" not in roles and "System Manager" not in roles:
		frappe.throw(
			_("ETO SCR valuation propagation requires Administrator or System Manager"),
			frappe.PermissionError,
		)


def _approx(a: float, b: float, tol: float = RATE_TOLERANCE) -> bool:
	return abs(flt(a) - flt(b)) <= tol


def _scr_fg_rows(scr_name: str) -> list[dict]:
	return frappe.get_all(
		"Subcontracting Receipt Item",
		filters={"parent": scr_name},
		fields=[
			"name",
			"item_code",
			"qty",
			"rate",
			"amount",
			"rm_cost_per_qty",
			"service_cost_per_qty",
			"additional_cost_per_qty",
			"landed_cost_voucher_amount",
			"batch_no",
			"warehouse",
			"purchase_order",
			"subcontracting_order",
		],
		order_by="idx",
	)


def _active_scr_fg_sle(scr_name: str, item_code: str) -> dict | None:
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, actual_qty, incoming_rate, valuation_rate,
		       stock_value_difference, batch_no, posting_date, posting_time, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Subcontracting Receipt'
		  AND voucher_no=%s
		  AND item_code=%s
		  AND is_cancelled=0
		  AND actual_qty>0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(scr_name, item_code),
		as_dict=True,
	)
	return rows[0] if rows else None


def _lcv_state(scr_name: str) -> dict:
	links = frappe.db.sql(
		"""
		SELECT lcp.parent AS landed_cost_voucher, lcv.docstatus, lcv.modified
		FROM `tabLanded Cost Purchase Receipt` lcp
		LEFT JOIN `tabLanded Cost Voucher` lcv ON lcv.name=lcp.parent
		WHERE lcp.receipt_document=%s
		""",
		(scr_name,),
		as_dict=True,
	)
	deleted = frappe.db.sql(
		"""
		SELECT deleted_name, creation
		FROM `tabDeleted Document`
		WHERE deleted_doctype='Landed Cost Voucher'
		  AND data LIKE %s
		ORDER BY creation DESC
		LIMIT 10
		""",
		(f"%{scr_name}%",),
		as_dict=True,
	)
	active = [r for r in links if cint(r.get("docstatus")) == 1]
	cancelled = [r for r in links if cint(r.get("docstatus")) == 2]
	return {
		"active_links": active,
		"cancelled_links": cancelled,
		"deleted": [{"name": d.deleted_name, "creation": d.creation} for d in deleted],
		"has_active_lcv": bool(active),
		"deleted_lcv_names": [d.deleted_name for d in deleted],
	}


def _batch_sabe_history(batch_no: str | None) -> list[dict]:
	if not batch_no:
		return []
	return frappe.db.sql(
		"""
		SELECT sbb.voucher_type, sbb.voucher_no, sbb.posting_date, sbb.posting_time,
		       sbe.warehouse, sbe.qty, sbe.incoming_rate, sbe.outgoing_rate,
		       sbe.stock_value_difference, sbe.batch_no, sbb.name AS bundle
		FROM `tabSerial and Batch Entry` sbe
		JOIN `tabSerial and Batch Bundle` sbb ON sbb.name=sbe.parent
		WHERE sbe.batch_no=%s
		  AND IFNULL(sbb.is_cancelled,0)=0
		  AND IFNULL(sbb.docstatus,1)=1
		ORDER BY sbb.posting_date, sbb.posting_time, sbb.creation, sbe.idx
		""",
		(batch_no,),
		as_dict=True,
	)


def _downstream_stock_entries(batch_no: str | None, item_code: str) -> list[dict]:
	if not batch_no:
		return []
	return frappe.db.sql(
		"""
		SELECT se.name, se.stock_entry_type, se.purpose, se.posting_date, se.posting_time,
		       se.docstatus, sed.name AS stock_entry_detail, sed.item_code, sed.qty,
		       sed.basic_rate, sed.amount, sed.valuation_rate, sed.s_warehouse, sed.t_warehouse,
		       sed.batch_no, sed.is_finished_item, sed.is_scrap_item,
		       sed.serial_and_batch_bundle
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name=sed.parent
		WHERE sed.batch_no=%s
		  AND sed.item_code=%s
		  AND se.docstatus=1
		ORDER BY se.posting_date, se.posting_time, se.name, sed.idx
		""",
		(batch_no, item_code),
		as_dict=True,
	)


def _ste_sle_for_item(voucher_no: str, item_code: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT name, warehouse, actual_qty, incoming_rate, valuation_rate,
		       stock_value_difference, serial_and_batch_bundle, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry'
		  AND voucher_no=%s
		  AND item_code=%s
		  AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		(voucher_no, item_code),
		as_dict=True,
	)


def _manufacture_fg_rows(manufacture_names: list[str]) -> list[dict]:
	if not manufacture_names:
		return []
	return frappe.db.sql(
		"""
		SELECT se.name, sed.name AS stock_entry_detail, sed.item_code, sed.qty,
		       sed.basic_rate, sed.amount, sed.batch_no, sed.is_finished_item
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name=sed.parent
		WHERE se.name IN %s
		  AND sed.is_finished_item=1
		  AND se.docstatus=1
		ORDER BY se.name, sed.idx
		""",
		(tuple(manufacture_names),),
		as_dict=True,
	)


def _riv_history_for_scr(scr_name: str, item_code: str | None, warehouse: str | None) -> list[dict]:
	rows = frappe.get_all(
		"Repost Item Valuation",
		filters={"voucher_type": "Subcontracting Receipt", "voucher_no": scr_name},
		fields=[
			"name",
			"status",
			"based_on",
			"repost_only_accounting_ledgers",
			"creation",
			"modified",
		],
		order_by="creation desc",
		limit_page_length=20,
	)
	out = [{"scope": "scr_transaction", **r} for r in rows]
	if item_code and warehouse:
		iw = frappe.get_all(
			"Repost Item Valuation",
			filters={
				"based_on": "Item and Warehouse",
				"item_code": item_code,
				"warehouse": warehouse,
			},
			fields=[
				"name",
				"status",
				"posting_date",
				"posting_time",
				"repost_only_accounting_ledgers",
				"creation",
				"modified",
			],
			order_by="creation desc",
			limit_page_length=10,
		)
		out.extend({"scope": "item_warehouse", **r} for r in iw)
	return out


def _open_riv_conflicts(item_code: str, warehouse: str | None = None) -> list[dict]:
	filters: dict[str, Any] = {
		"status": ("in", ["Queued", "In Progress"]),
		"item_code": item_code,
	}
	rows = frappe.get_all(
		"Repost Item Valuation",
		filters=filters,
		fields=["name", "status", "item_code", "warehouse", "based_on", "voucher_no", "creation"],
		limit_page_length=50,
	)
	# Also Transaction RIVs on SCRs for same FG can conflict via dependants.
	tx = frappe.db.sql(
		"""
		SELECT name, status, based_on, voucher_type, voucher_no, creation
		FROM `tabRepost Item Valuation`
		WHERE status IN ('Queued','In Progress')
		  AND based_on='Transaction'
		  AND voucher_type='Subcontracting Receipt'
		LIMIT 50
		""",
		as_dict=True,
	)
	merged = {r["name"]: r for r in rows}
	for r in tx:
		merged[r["name"]] = r
	if warehouse:
		return [
			r
			for r in merged.values()
			if (not r.get("warehouse")) or r.get("warehouse") == warehouse or r.get("based_on") == "Transaction"
		]
	return list(merged.values())


def _stage(
	stage: str,
	document: str,
	qty,
	rate,
	value,
	expected_rate,
	*,
	note: str = "",
) -> dict:
	diff = None
	if expected_rate is not None and rate is not None:
		diff = flt(rate) - flt(expected_rate)
	return {
		"stage": stage,
		"document": document,
		"qty": qty,
		"incoming_rate": rate,
		"stock_value": value,
		"expected_rate": expected_rate,
		"difference": diff,
		"ok": diff is None or abs(flt(diff)) <= RATE_TOLERANCE,
		"note": note,
	}


def build_valuation_chain(scr_name: str) -> dict:
	"""Read-only chronological valuation chain for one SCR + downstream ETO docs."""
	require_privileged_user()
	if not scr_name:
		frappe.throw(_("subcontracting_receipt is required"))
	if not frappe.db.exists("Subcontracting Receipt", scr_name):
		frappe.throw(_("Subcontracting Receipt {0} not found").format(scr_name))

	meta = frappe.db.get_value(
		"Subcontracting Receipt",
		scr_name,
		["name", "company", "posting_date", "posting_time", "docstatus", "is_return", "supplier"],
		as_dict=True,
	)
	eto = is_eto_subcontracting_receipt(scr_name)
	lcv = _lcv_state(scr_name)
	fg_rows = _scr_fg_rows(scr_name)
	chains = []
	first_break = None

	for fg in fg_rows:
		expected_doc = (
			flt(fg.rm_cost_per_qty) + flt(fg.service_cost_per_qty) + flt(fg.additional_cost_per_qty)
		)
		sle = _active_scr_fg_sle(scr_name, fg.item_code)
		scr_rate = flt(sle.incoming_rate) if sle else None
		scr_value = flt(sle.stock_value_difference) if sle else None
		stages = [
			_stage("SCR document", scr_name, fg.qty, fg.rate, fg.amount, expected_doc, note="document fields"),
			_stage(
				"SCR SLE",
				scr_name,
				sle.actual_qty if sle else None,
				scr_rate,
				scr_value,
				expected_doc,
				note="active FG SLE (source of truth for propagation after LCV settle)",
			),
		]
		# Prefer SCR SLE as expected for downstream when present.
		downstream_expected = scr_rate if scr_rate is not None else expected_doc

		sabe = _batch_sabe_history(fg.batch_no)
		downstream = _downstream_stock_entries(fg.batch_no, fg.item_code)
		xfer = [d for d in downstream if d.stock_entry_type in TRANSFER_PURPOSES]
		consume = [
			d
			for d in downstream
			if d.stock_entry_type in MANUFACTURE_PURPOSES and not cint(d.is_finished_item)
		]
		mfg_names = sorted({d.name for d in consume})
		fg_out = _manufacture_fg_rows(mfg_names)

		for d in xfer:
			stages.append(
				_stage(
					"Transfer SED",
					d.name,
					d.qty,
					d.basic_rate,
					d.amount,
					downstream_expected,
					note="Material Transfer for Manufacture",
				)
			)
			for s in _ste_sle_for_item(d.name, fg.item_code):
				rate = flt(s.incoming_rate) if flt(s.actual_qty) > 0 else (
					abs(flt(s.stock_value_difference) / flt(s.actual_qty)) if flt(s.actual_qty) else None
				)
				stages.append(
					_stage(
						"Transfer SLE",
						d.name,
						s.actual_qty,
						rate,
						s.stock_value_difference,
						downstream_expected,
						note=(s.warehouse or "")[:40],
					)
				)

		for d in consume:
			stages.append(
				_stage(
					"Manufacture consume SED",
					d.name,
					d.qty,
					d.basic_rate,
					d.amount,
					downstream_expected,
					note="consume row",
				)
			)

		for d in fg_out:
			stages.append(
				_stage(
					"Manufacture FG SED",
					d.name,
					d.qty,
					d.basic_rate,
					d.amount,
					None,
					note=f"FG {d.item_code}",
				)
			)

		break_stage = next((s for s in stages if s["ok"] is False), None)
		if break_stage and first_break is None:
			first_break = {
				"subcontracting_receipt": scr_name,
				"item_code": fg.item_code,
				"batch_no": fg.batch_no,
				**break_stage,
			}

		# SABE vs SED divergence on transfers (00011 pattern)
		sabe_transfer_issues = []
		for d in xfer:
			sabe_rows = [r for r in sabe if r.voucher_no == d.name and flt(r.qty) < 0]
			if not sabe_rows:
				continue
			sabe_rate = flt(sabe_rows[0].outgoing_rate) or flt(sabe_rows[0].incoming_rate)
			if not _approx(sabe_rate, flt(d.basic_rate)):
				sabe_transfer_issues.append(
					{
						"stock_entry": d.name,
						"sed_basic_rate": d.basic_rate,
						"sabe_rate": sabe_rate,
						"sabe_svd": sabe_rows[0].stock_value_difference,
						"sed_amount": d.amount,
					}
				)
				if first_break is None or (
					first_break.get("stage") == "Transfer SED"
					and first_break.get("document") == d.name
				):
					first_break = {
						"subcontracting_receipt": scr_name,
						"item_code": fg.item_code,
						"batch_no": fg.batch_no,
						"stage": "Transfer SABE vs SED",
						"document": d.name,
						"incoming_rate": d.basic_rate,
						"expected_rate": sabe_rate,
						"difference": flt(d.basic_rate) - sabe_rate,
						"ok": False,
						"note": "SABE batch rate disagrees with SED/SLE (ledger drift)",
					}

		chains.append(
			{
				"item_code": fg.item_code,
				"batch_no": fg.batch_no,
				"warehouse": fg.warehouse,
				"qty": fg.qty,
				"document_rate": fg.rate,
				"service_cost_per_qty": fg.service_cost_per_qty,
				"rm_cost_per_qty": fg.rm_cost_per_qty,
				"landed_cost_voucher_amount": fg.landed_cost_voucher_amount,
				"scr_sle_rate": scr_rate,
				"scr_sle_value": scr_value,
				"is_eto_finished_good": is_eto_finished_good(fg.item_code),
				"stages": stages,
				"sabe_history": sabe,
				"downstream_stock_entries": downstream,
				"manufacture_finished_goods": fg_out,
				"sabe_sed_divergences": sabe_transfer_issues,
				"first_incorrect_transition": break_stage,
			}
		)

	gl = frappe.db.sql(
		"""
		SELECT account, SUM(debit) AS debit, SUM(credit) AS credit
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND is_cancelled=0
		GROUP BY account
		ORDER BY account
		""",
		(scr_name,),
		as_dict=True,
	)
	debit = sum(flt(r.debit) for r in gl)
	credit = sum(flt(r.credit) for r in gl)

	return {
		"api_version": API_VERSION,
		"subcontracting_receipt": scr_name,
		"meta": meta,
		"is_eto_subcontracting_receipt": eto,
		"lcv": lcv,
		"finished_goods": chains,
		"gl": gl,
		"gl_balanced": abs(debit - credit) <= RATE_TOLERANCE,
		"gl_debit": debit,
		"gl_credit": credit,
		"riv_history": _riv_history_for_scr(
			scr_name,
			chains[0]["item_code"] if chains else None,
			chains[0]["warehouse"] if chains else None,
		),
		"first_incorrect_transition": first_break,
		"chain_consistent": first_break is None
		and not any(c.get("sabe_sed_divergences") for c in chains),
	}


def plan_propagation(
	scr_name: str,
	*,
	include_service_cost_correction: bool | None = None,
) -> dict:
	"""Dry-run plan: optional service-cost correction + Item/Warehouse stock RIV order."""
	require_privileged_user()
	chain = build_valuation_chain(scr_name)
	if not chain["is_eto_subcontracting_receipt"]:
		return {
			**chain,
			"eligible": False,
			"action": "skip_not_eto",
			"reasons": ["not_an_eto_subcontracting_receipt"],
			"plan": [],
		}
	if cint(chain["meta"].get("docstatus")) != 1:
		return {
			**chain,
			"eligible": False,
			"action": "skip_not_submitted",
			"reasons": ["scr_not_submitted"],
			"plan": [],
		}
	if cint(chain["meta"].get("is_return")):
		return {
			**chain,
			"eligible": False,
			"action": "skip_return",
			"reasons": ["return_scr_out_of_scope"],
			"plan": [],
		}

	# Active LCV is an open valuation dependency — do not auto-propagate over it.
	if chain["lcv"].get("has_active_lcv"):
		return {
			**chain,
			"eligible": False,
			"action": "stop_active_lcv",
			"reasons": ["active_landed_cost_voucher_present"],
			"plan": [],
			"ambiguous": True,
		}

	plan = []
	reasons = []
	svc_needed = False
	try:
		from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service import (
			inspect_subcontracting_receipt,
		)

		svc = inspect_subcontracting_receipt(scr_name)
		svc_needed = bool(svc.get("eligible") and svc.get("action") == "correct")
		chain["service_cost_inspection"] = {
			"eligible": svc.get("eligible"),
			"action": svc.get("action"),
			"items": svc.get("items"),
			"allocation_checks": svc.get("allocation_checks"),
		}
	except Exception as exc:
		chain["service_cost_inspection"] = {"error": str(exc)}
		reasons.append("service_cost_inspect_failed")

	if include_service_cost_correction is None:
		# Default: apply PO-based service-cost correction whenever eligible.
		# Callers verifying a post-LCV-deletion consistent chain (e.g. 00023)
		# must pass include_service_cost_correction=False explicitly.
		include_service_cost_correction = bool(svc_needed)

	if include_service_cost_correction and svc_needed:
		plan.append(
			{
				"step": 1,
				"action": "correct_service_cost",
				"api": "correct_subcontracting_receipt_service_cost",
				"subcontracting_receipt": scr_name,
			}
		)
	elif svc_needed and not include_service_cost_correction:
		reasons.append("service_cost_correction_explicitly_skipped")

	fg = (chain.get("finished_goods") or [None])[0]
	if not fg:
		return {
			**chain,
			"eligible": False,
			"action": "skip_no_fg",
			"reasons": ["no_finished_good_rows"],
			"plan": plan,
		}

	conflicts = _open_riv_conflicts(fg["item_code"], fg["warehouse"])
	# Exclude ourselves later; for planning any open conflict blocks apply.
	if conflicts:
		reasons.append("open_riv_conflict")

	posting_date = chain["meta"].get("posting_date")
	posting_time = chain["meta"].get("posting_time")
	plan.append(
		{
			"step": len(plan) + 1,
			"action": "repost_item_warehouse",
			"based_on": "Item and Warehouse",
			"item_code": fg["item_code"],
			"warehouse": fg["warehouse"],
			"posting_date": posting_date,
			"posting_time": posting_time,
			"allow_negative_stock": 1,
			"repost_only_accounting_ledgers": 0,
			"note": "Stock RIV so Core BatchNoValuation + ETO SABE scope rewrite Transfer/Manufacture",
		}
	)

	# Additional warehouses touched by transfers (WIP) — Core usually creates
	# dependant RIVs; list them for the dry-run report.
	extra_wh = sorted(
		{
			d.t_warehouse
			for d in fg.get("downstream_stock_entries") or []
			if d.t_warehouse and d.t_warehouse != fg["warehouse"]
		}
	)
	for wh in extra_wh:
		plan.append(
			{
				"step": len(plan) + 1,
				"action": "expect_dependant_or_followup_iw",
				"based_on": "Item and Warehouse",
				"item_code": fg["item_code"],
				"warehouse": wh,
				"note": "WIP/destination warehouse — Core dependant RIV or follow-up IW if rates remain stale",
			}
		)

	needs_work = (not chain.get("chain_consistent")) or (
		include_service_cost_correction and svc_needed
	)
	eligible = bool(needs_work and not conflicts)
	action = "propagate" if eligible else (
		"noop_consistent" if chain.get("chain_consistent") and not (include_service_cost_correction and svc_needed)
		else "blocked"
	)

	return {
		**chain,
		"eligible": eligible,
		"action": action,
		"reasons": reasons,
		"plan": plan,
		"open_riv_conflicts": conflicts,
		"include_service_cost_correction": bool(include_service_cost_correction and svc_needed),
		"confirm_apply": CONFIRM_APPLY,
	}


def _execute_item_warehouse_riv(
	*,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	allow_negative_stock: int = 1,
) -> dict:
	"""Create + execute one stock Item/Warehouse RIV via supported DocType APIs."""
	conflicts = _open_riv_conflicts(item_code, warehouse)
	if conflicts:
		frappe.throw(
			_("Open Repost Item Valuation conflicts: {0}").format(
				", ".join(c["name"] for c in conflicts)
			),
			title="ETO Propagation Blocked",
		)

	riv = frappe.get_doc(
		{
			"doctype": "Repost Item Valuation",
			"based_on": "Item and Warehouse",
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": getdate(posting_date),
			"posting_time": posting_time,
			"allow_negative_stock": cint(allow_negative_stock),
			"repost_only_accounting_ledgers": 0,
		}
	)
	riv.insert(ignore_permissions=True)
	riv.submit()
	frappe.db.commit()

	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		execute_reposting_entry,
	)

	execute_reposting_entry(riv.name)
	frappe.db.commit()
	riv.reload()
	return {
		"riv": riv.name,
		"status": riv.status,
		"error_log": (riv.error_log or "")[:2000],
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": str(posting_date),
		"posting_time": str(posting_time),
	}


def apply_propagation(
	scr_name: str,
	*,
	dry_run: bool = True,
	confirm: str | None = None,
	include_service_cost_correction: bool | None = None,
	execute_riv: bool = True,
) -> dict:
	"""Apply (or dry-run) ETO SCR valuation propagation for one receipt."""
	require_privileged_user()
	plan_doc = plan_propagation(
		scr_name, include_service_cost_correction=include_service_cost_correction
	)
	if dry_run:
		return {**plan_doc, "dry_run": True, "applied": False}

	if confirm != CONFIRM_APPLY:
		frappe.throw(
			_("confirm must be {0}").format(CONFIRM_APPLY),
			frappe.ValidationError,
		)
	if not plan_doc.get("eligible"):
		return {**plan_doc, "dry_run": False, "applied": False}

	results = {"service_cost": None, "rivs": [], "verification": None}

	if plan_doc.get("include_service_cost_correction"):
		from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.api import (
			correct_subcontracting_receipt_service_cost,
		)
		from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost import (
			CONFIRM_APPLY as SVC_CONFIRM,
		)

		fp = None
		insp = plan_doc.get("service_cost_inspection") or {}
		# fingerprint via dedicated call
		from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service import (
			fingerprint_scr,
		)

		fp = fingerprint_scr(scr_name)
		results["service_cost"] = correct_subcontracting_receipt_service_cost(
			subcontracting_receipts=[scr_name],
			dry_run=0,
			confirm=SVC_CONFIRM,
			fingerprints={scr_name: fp},
			enqueue_riv=0,
		)

	if execute_riv:
		fg = plan_doc["finished_goods"][0]
		meta = plan_doc["meta"]
		riv_result = _execute_item_warehouse_riv(
			item_code=fg["item_code"],
			warehouse=fg["warehouse"],
			posting_date=meta["posting_date"],
			posting_time=meta["posting_time"],
		)
		results["rivs"].append(riv_result)

		# If destination warehouses still diverge, follow-up IW RIVs (idempotent).
		after = build_valuation_chain(scr_name)
		for step in plan_doc.get("plan") or []:
			if step.get("action") != "expect_dependant_or_followup_iw":
				continue
			wh = step["warehouse"]
			# Check whether any transfer into this warehouse still mismatches SCR SLE.
			still_bad = False
			for c in after.get("finished_goods") or []:
				for st in c.get("stages") or []:
					if st.get("stage") in ("Transfer SED", "Transfer SLE", "Manufacture consume SED") and not st.get(
						"ok"
					):
						still_bad = True
						break
			if not still_bad:
				continue
			# Avoid duplicate concurrent jobs.
			if _open_riv_conflicts(fg["item_code"], wh):
				results["rivs"].append(
					{"warehouse": wh, "status": "skipped_conflict", "item_code": fg["item_code"]}
				)
				continue
			# Use earliest transfer posting into this warehouse as RIV start.
			posting_date = meta["posting_date"]
			posting_time = meta["posting_time"]
			for d in fg.get("downstream_stock_entries") or []:
				if d.t_warehouse == wh:
					posting_date = d.posting_date
					posting_time = d.posting_time
					break
			results["rivs"].append(
				_execute_item_warehouse_riv(
					item_code=fg["item_code"],
					warehouse=wh,
					posting_date=posting_date,
					posting_time=posting_time,
				)
			)

	results["verification"] = build_valuation_chain(scr_name)
	return {
		**plan_doc,
		"dry_run": False,
		"applied": True,
		"results": results,
		"final_chain_consistent": bool(results["verification"].get("chain_consistent")),
	}


def process_allowlist(
	subcontracting_receipts,
	*,
	dry_run: bool = True,
	confirm: str | None = None,
	include_service_cost_correction: bool | None = None,
	execute_riv: bool = True,
) -> dict:
	"""Process an allowlist of SCR names (max MAX_ALLOWLIST)."""
	require_privileged_user()
	if isinstance(subcontracting_receipts, str):
		import json

		try:
			subcontracting_receipts = json.loads(subcontracting_receipts)
		except Exception:
			subcontracting_receipts = [subcontracting_receipts]
	names = [str(n).strip() for n in (subcontracting_receipts or []) if str(n).strip()]
	if len(names) > MAX_ALLOWLIST:
		frappe.throw(_("Allowlist exceeds MAX_ALLOWLIST={0}").format(MAX_ALLOWLIST))

	out = []
	for name in names:
		if dry_run:
			out.append(plan_propagation(name, include_service_cost_correction=include_service_cost_correction))
		else:
			out.append(
				apply_propagation(
					name,
					dry_run=False,
					confirm=confirm,
					include_service_cost_correction=include_service_cost_correction,
					execute_riv=execute_riv,
				)
			)
	return {
		"api_version": API_VERSION,
		"dry_run": dry_run,
		"count": len(out),
		"results": out,
	}
