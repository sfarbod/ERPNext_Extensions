# Copyright (c) 2026, ERPNext Extensions contributors
"""Evidence-based campaign coverage verification (v5.6.1).

A Completed Core RIV is NOT sufficient proof that a campaign entry is clean.
Coverage requires correct identity, chronological scope, successful execution,
SLE quantity/valuation continuity, no unresolved Failed RIV / I3 / I4 / TVA,
Bin consistency, and recorded verification evidence.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	get_currency_precision,
)
from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_DOCTYPE,
	ITEM_PENDING,
	ITEM_SKIPPED,
	TVA_FAILED,
	TVA_NA,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	ANOMALY_BIN,
	ANOMALY_INVALID_INCOMING,
	ANOMALY_INVALID_OUTGOING,
	ANOMALY_NEGATIVE_STOCK_VALUE,
	ANOMALY_QTY_CONTINUITY,
	ANOMALY_RIV_FAILURE,
	ANOMALY_TVA,
	ANOMALY_VALUATION_CONTINUITY,
	ANOMALY_ZERO_QTY_RESIDUAL,
	CONFIRMED_CORRUPTION,
	DEFAULT_QTY_TOLERANCE,
	DEFAULT_VALUE_TOLERANCE,
	NEEDS_MANUAL_REVIEW,
	REPAIRED_AND_VERIFIED,
	REPOST_REQUIRED,
	SUSPECTED_CORRUPTION,
	VERIFIED_CLEAN,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	verify_voucher_basic,
)
from erpnext_extensions.iran_accounting.historical_rebuild.tva_reconcile import (
	reconcile_tva_for_voucher,
)


def _value_tol(company: str | None) -> float:
	try:
		currency = get_company_currency(company) if company else "IRR"
		prec = get_currency_precision(currency)
		return 1.0 if prec == 0 else (1.0 / (10**prec))
	except Exception:
		return DEFAULT_VALUE_TOLERANCE


def find_completed_covering_riv(
	item_code: str, warehouse: str, posting_date, posting_time
) -> str | None:
	"""Completed Item+WH RIV with posting boundary <= SLE posting (chronological cover)."""
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


def has_unresolved_failed_riv(item_code: str, warehouse: str) -> str | None:
	"""Return Failed RIV name if identity still has an unresolved Failed row."""
	rows = frappe.db.sql(
		"""
		select name from `tabRepost Item Valuation`
		where docstatus=1
		  and based_on='Item and Warehouse'
		  and item_code=%s and warehouse=%s
		  and status='Failed'
		order by modified desc
		limit 1
		""",
		(item_code, warehouse),
	)
	return rows[0][0] if rows else None


def _identity_sle_chain(
	item_code: str, warehouse: str, *, from_date=None, from_time=None
) -> list[dict]:
	"""Chronological SLE rows for an identity, optionally from an anchor forward."""
	params: list[Any] = [item_code, warehouse]
	anchor_sql = ""
	if from_date:
		pt = str(from_time or "00:00:00")
		anchor_sql = """
		  and (
		    posting_date > %s
		    or (posting_date = %s and posting_time >= %s)
		  )
		"""
		params.extend([from_date, from_date, pt])
	return (
		frappe.db.sql(
			f"""
			select name, item_code, warehouse, company, voucher_type, voucher_no,
			       posting_date, posting_time, creation,
			       actual_qty, qty_after_transaction, stock_value, stock_value_difference
			from `tabStock Ledger Entry`
			where is_cancelled=0
			  and item_code=%s and warehouse=%s
			  {anchor_sql}
			order by posting_date asc, posting_time asc, creation asc
			""",
			tuple(params),
			as_dict=True,
		)
		or []
	)


def inspect_sle_chain(
	item_code: str,
	warehouse: str,
	*,
	company: str | None = None,
	from_date=None,
	from_time=None,
) -> dict[str, Any]:
	"""Inspect SLE continuity / I3 / I4 / zero-qty / negative SV for one identity."""
	tol = _value_tol(company)
	qty_tol = DEFAULT_QTY_TOLERANCE
	rows = _identity_sle_chain(item_code, warehouse, from_date=from_date, from_time=from_time)
	issues: list[dict[str, Any]] = []
	prev = None
	for sle in rows:
		aq = flt(sle.actual_qty)
		qat = flt(sle.qty_after_transaction)
		sv = flt(sle.stock_value)
		svd = flt(sle.stock_value_difference)

		# A. Zero qty residual
		if abs(qat) <= qty_tol and abs(sv) > tol:
			issues.append(
				{
					"type": ANOMALY_ZERO_QTY_RESIDUAL,
					"sle": sle.name,
					"voucher": f"{sle.voucher_type}/{sle.voucher_no}",
					"qty": qat,
					"stock_value": sv,
					"classification": CONFIRMED_CORRUPTION,
				}
			)

		# B. Invalid outgoing (I3): actual_qty < 0 and SVD > 0
		if aq < -qty_tol and svd > tol:
			issues.append(
				{
					"type": ANOMALY_INVALID_OUTGOING,
					"sle": sle.name,
					"voucher": f"{sle.voucher_type}/{sle.voucher_no}",
					"actual_qty": aq,
					"stock_value_difference": svd,
					"classification": CONFIRMED_CORRUPTION,
				}
			)

		# C. Invalid incoming: actual_qty > 0 and SVD < 0
		# Legitimate cases exist (e.g. negative rate corrections); flag as suspected.
		if aq > qty_tol and svd < -tol:
			issues.append(
				{
					"type": ANOMALY_INVALID_INCOMING,
					"sle": sle.name,
					"voucher": f"{sle.voucher_type}/{sle.voucher_no}",
					"actual_qty": aq,
					"stock_value_difference": svd,
					"classification": SUSPECTED_CORRUPTION,
				}
			)

		# D. Positive qty with negative stock value
		if qat > qty_tol and sv < -tol:
			issues.append(
				{
					"type": ANOMALY_NEGATIVE_STOCK_VALUE,
					"sle": sle.name,
					"voucher": f"{sle.voucher_type}/{sle.voucher_no}",
					"qty": qat,
					"stock_value": sv,
					"classification": CONFIRMED_CORRUPTION,
				}
			)

		if prev is not None:
			# F. Qty continuity
			expected_qty = flt(prev.qty_after_transaction) + aq
			if abs(expected_qty - qat) > qty_tol:
				issues.append(
					{
						"type": ANOMALY_QTY_CONTINUITY,
						"sle": sle.name,
						"voucher": f"{sle.voucher_type}/{sle.voucher_no}",
						"expected": expected_qty,
						"actual": qat,
						"difference": qat - expected_qty,
						"classification": CONFIRMED_CORRUPTION,
					}
				)
			# E. Valuation continuity
			expected_sv = flt(prev.stock_value) + svd
			if abs(expected_sv - sv) > tol:
				issues.append(
					{
						"type": ANOMALY_VALUATION_CONTINUITY,
						"sle": sle.name,
						"voucher": f"{sle.voucher_type}/{sle.voucher_no}",
						"expected": expected_sv,
						"actual": sv,
						"difference": sv - expected_sv,
						"classification": CONFIRMED_CORRUPTION,
					}
				)
		prev = sle

	# K. Bin consistency vs final SLE
	bin_issue = None
	if rows:
		final = rows[-1]
		bin_row = frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			["actual_qty", "stock_value"],
			as_dict=True,
		)
		if bin_row:
			if abs(flt(bin_row.actual_qty) - flt(final.qty_after_transaction)) > qty_tol or abs(
				flt(bin_row.stock_value) - flt(final.stock_value)
			) > tol:
				bin_issue = {
					"type": ANOMALY_BIN,
					"bin_qty": flt(bin_row.actual_qty),
					"bin_sv": flt(bin_row.stock_value),
					"sle_qty": flt(final.qty_after_transaction),
					"sle_sv": flt(final.stock_value),
					"classification": CONFIRMED_CORRUPTION,
				}
				issues.append(bin_issue)

	dirty = any(
		i.get("classification") in (CONFIRMED_CORRUPTION, SUSPECTED_CORRUPTION, NEEDS_MANUAL_REVIEW)
		for i in issues
	)
	return {
		"item_code": item_code,
		"warehouse": warehouse,
		"sle_count": len(rows),
		"issues": issues,
		"dirty": dirty,
		"issue_count": len(issues),
	}


def open_tva_for_identity(item_code: str, warehouse: str) -> list[dict]:
	"""Open / unrepaired TVA rows for identity (when DocType exists)."""
	if not frappe.db.exists("DocType", "Temporary Valuation Adjustment"):
		return []
	# Field names vary slightly across versions; probe columns.
	cols = {c.fieldname for c in frappe.get_meta("Temporary Valuation Adjustment").fields}
	filters: dict[str, Any] = {}
	if "item_code" in cols:
		filters["item_code"] = item_code
	if "warehouse" in cols:
		filters["warehouse"] = warehouse
	if not filters:
		return []
	status_field = "repair_status" if "repair_status" in cols else None
	if status_field:
		filters[status_field] = ("in", ["OPEN", "SUBMITTED", "REPOST_PENDING", "MANUAL_REVIEW"])
	fields = ["name"]
	for f in ("repair_status", "stock_entry", "adjustment_delta", "posting_mode"):
		if f in cols:
			fields.append(f)
	return frappe.get_all(
		"Temporary Valuation Adjustment",
		filters=filters,
		fields=fields,
		limit_page_length=50,
	) or []


def verify_identity_coverage(
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	*,
	company: str | None = None,
) -> dict[str, Any]:
	"""Full evidence check for one Item/Warehouse at a voucher SLE boundary.

	Completed RIV alone is never enough.
	"""
	evidence: dict[str, Any] = {
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": str(posting_date),
		"posting_time": str(posting_time or ""),
		"checks": {},
		"issues": [],
		"covering_riv": None,
		"ok": False,
		"verification_class": REPOST_REQUIRED,
		"reason": "",
	}

	failed = has_unresolved_failed_riv(item_code, warehouse)
	evidence["checks"]["no_failed_riv"] = failed is None
	if failed:
		evidence["issues"].append({"type": ANOMALY_RIV_FAILURE, "riv": failed})
		evidence["reason"] = f"unresolved_failed_riv:{failed}"
		evidence["verification_class"] = REPOST_REQUIRED
		return evidence

	riv = find_completed_covering_riv(item_code, warehouse, posting_date, posting_time)
	evidence["covering_riv"] = riv
	evidence["checks"]["completed_riv"] = bool(riv)
	if not riv:
		evidence["reason"] = "no_completed_covering_riv"
		evidence["verification_class"] = REPOST_REQUIRED
		return evidence

	riv_row = frappe.db.get_value(
		"Repost Item Valuation",
		riv,
		["item_code", "warehouse", "posting_date", "posting_time", "status", "docstatus"],
		as_dict=True,
	)
	identity_ok = (
		riv_row
		and riv_row.item_code == item_code
		and riv_row.warehouse == warehouse
		and riv_row.status == "Completed"
		and cint(riv_row.docstatus) == 1
	)
	evidence["checks"]["riv_identity_match"] = bool(identity_ok)
	if not identity_ok:
		evidence["reason"] = f"riv_identity_mismatch:{riv}"
		evidence["verification_class"] = REPOST_REQUIRED
		return evidence

	# Continuity / integrity from RIV anchor forward (covers voucher chronologically)
	chain = inspect_sle_chain(
		item_code,
		warehouse,
		company=company,
		from_date=riv_row.posting_date,
		from_time=riv_row.posting_time,
	)
	evidence["checks"]["sle_chain_clean"] = not chain["dirty"]
	evidence["issues"].extend(chain["issues"][:20])
	if chain["dirty"]:
		evidence["reason"] = f"sle_chain_dirty:{chain['issue_count']}"
		evidence["verification_class"] = REPOST_REQUIRED
		# Also inspect full history for earliest corruption signal
		full = inspect_sle_chain(item_code, warehouse, company=company)
		if full["issues"]:
			evidence["earliest_issue"] = full["issues"][0]
		return evidence

	tva_open = open_tva_for_identity(item_code, warehouse)
	evidence["checks"]["no_open_tva"] = len(tva_open) == 0
	if tva_open:
		evidence["issues"].append(
			{"type": ANOMALY_TVA, "count": len(tva_open), "sample": [t.name for t in tva_open[:5]]}
		)
		evidence["reason"] = f"open_tva:{len(tva_open)}"
		evidence["verification_class"] = REPOST_REQUIRED
		return evidence

	evidence["ok"] = True
	evidence["verification_class"] = VERIFIED_CLEAN
	evidence["reason"] = f"verified:{riv}"
	return evidence


def verify_voucher_coverage(
	voucher_type: str,
	voucher_no: str,
	*,
	company: str | None = None,
) -> dict[str, Any]:
	"""Evidence-based coverage for one campaign voucher (all SLE identities)."""
	basic = verify_voucher_basic(voucher_type, voucher_no)
	out: dict[str, Any] = {
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"ok": False,
		"verification_class": REPOST_REQUIRED,
		"covering_rivs": [],
		"identity_results": [],
		"basic": basic,
		"tva": None,
		"reason": "",
	}
	if not basic.get("ok"):
		out["reason"] = f"verify_basic:{basic.get('detail')}"
		return out

	sles = (
		frappe.db.sql(
			"""
			select distinct item_code, warehouse, posting_date, posting_time, company
			from `tabStock Ledger Entry`
			where is_cancelled=0 and voucher_type=%s and voucher_no=%s
			  and item_code is not null and item_code!=''
			  and warehouse is not null and warehouse!=''
			""",
			(voucher_type, voucher_no),
			as_dict=True,
		)
		or []
	)

	if not sles:
		# Fixed-asset / non-stock: basic verify is enough; no identity cover needed.
		tva = reconcile_tva_for_voucher(voucher_type, voucher_no)
		out["tva"] = tva
		out["ok"] = True
		out["verification_class"] = VERIFIED_CLEAN
		out["reason"] = f"no_sle:{basic.get('detail')}"
		return out

	covers: list[str] = []
	for sle in sles:
		co = company or sle.company
		ev = verify_identity_coverage(
			sle.item_code,
			sle.warehouse,
			sle.posting_date,
			sle.posting_time,
			company=co,
		)
		out["identity_results"].append(ev)
		if not ev.get("ok"):
			out["reason"] = ev.get("reason") or "identity_not_verified"
			out["verification_class"] = ev.get("verification_class") or REPOST_REQUIRED
			return out
		if ev.get("covering_riv"):
			covers.append(ev["covering_riv"])

	tva = reconcile_tva_for_voucher(voucher_type, voucher_no)
	out["tva"] = tva
	if (tva.get("status") or "") == TVA_FAILED:
		out["reason"] = "tva_failed"
		out["verification_class"] = REPOST_REQUIRED
		return out

	out["covering_rivs"] = list(dict.fromkeys(covers))
	out["ok"] = True
	out["verification_class"] = VERIFIED_CLEAN
	out["reason"] = f"verified_identities:{len(sles)}"
	return out


def evidence_payload(result: dict[str, Any]) -> str:
	"""Compact JSON for campaign item verification_detail / error_message."""
	slim = {
		"ok": result.get("ok"),
		"verification_class": result.get("verification_class"),
		"reason": result.get("reason"),
		"covering_rivs": result.get("covering_rivs") or ([result.get("covering_riv")] if result.get("covering_riv") else []),
		"issue_types": sorted(
			{
				i.get("type")
				for ir in (result.get("identity_results") or [])
				for i in (ir.get("issues") or [])
				if i.get("type")
			}
			| {i.get("type") for i in (result.get("issues") or []) if i.get("type")}
		),
		"checked_at": str(now_datetime()),
	}
	return json.dumps(slim, ensure_ascii=False, default=str)[:2000]


def apply_verified_skip(item_name: str, result: dict[str, Any]) -> None:
	"""Mark campaign item Skipped Covered only after evidence-based verification."""
	covers = result.get("covering_rivs") or []
	tva = result.get("tva") or {}
	fields = {
		"item_status": ITEM_SKIPPED,
		"covering_riv": covers[0] if covers else None,
		"tva_reconcile_status": tva.get("status") or TVA_NA,
		"last_processed_at": now_datetime(),
		"error_message": "",
	}
	meta = frappe.get_meta(ITEM_DOCTYPE)
	if meta.has_field("verification_class"):
		fields["verification_class"] = VERIFIED_CLEAN
	if meta.has_field("verification_detail"):
		fields["verification_detail"] = evidence_payload(result)
	frappe.db.set_value(ITEM_DOCTYPE, item_name, fields, update_modified=False)


def apply_repost_required(item_name: str, result: dict[str, Any]) -> None:
	"""Return campaign item to Pending with evidence why Skipped Covered was invalid."""
	fields = {
		"item_status": ITEM_PENDING,
		"covering_riv": None,
		"last_processed_at": now_datetime(),
		"error_message": f"coverage_verify:{result.get('reason') or 'repost_required'}"[:1000],
	}
	meta = frappe.get_meta(ITEM_DOCTYPE)
	if meta.has_field("verification_class"):
		fields["verification_class"] = result.get("verification_class") or REPOST_REQUIRED
	if meta.has_field("verification_detail"):
		fields["verification_detail"] = evidence_payload(result)
	frappe.db.set_value(ITEM_DOCTYPE, item_name, fields, update_modified=False)


# Backward-compatible alias used by processor / fast_cover after v5.6.1
def find_covering_riv_verified(
	item_code: str, warehouse: str, posting_date, posting_time, *, company: str | None = None
) -> str | None:
	"""Return covering RIV name only when evidence-based identity verification passes."""
	ev = verify_identity_coverage(
		item_code, warehouse, posting_date, posting_time, company=company
	)
	if ev.get("ok"):
		return ev.get("covering_riv")
	return None
