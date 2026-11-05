# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only reassessment of Historical Rebuild Campaign cases."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_BLOCKED,
	ITEM_COMPLETED,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_RIV_LINKED,
	ITEM_SKIPPED,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	manufacture_product_reject_rate_issues,
)

STATUS_REQUIRES_REBUILD = "REQUIRES REBUILD"
STATUS_ALREADY_CORRECTED = "ALREADY CORRECTED"
STATUS_ALREADY_COVERED_BY_RIV = "ALREADY COVERED BY RIV"
STATUS_NOT_APPLICABLE = "NOT APPLICABLE"
STATUS_BLOCKED_UNKNOWN = "BLOCKED — ROOT CAUSE UNKNOWN"


def _sle_gl_ok(voucher_type: str, voucher_no: str) -> dict[str, Any]:
	sle_n = cint(
		frappe.db.count(
			"Stock Ledger Entry",
			{"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		)
	)
	gl = frappe.db.sql(
		"""
		select coalesce(sum(debit),0) d, coalesce(sum(credit),0) c, count(*) n
		from `tabGL Entry`
		where voucher_type=%s and voucher_no=%s and is_cancelled=0
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)[0]
	balanced = abs(flt(gl.d) - flt(gl.c)) <= 0.5
	qty0_val = frappe.db.sql(
		"""
		select count(*) from `tabStock Ledger Entry`
		where voucher_type=%s and voucher_no=%s and is_cancelled=0
		  and abs(qty_after_transaction) <= 1e-9
		  and abs(stock_value) > 0.5
		""",
		(voucher_type, voucher_no),
	)[0][0]
	return {
		"sle_count": sle_n,
		"gl_count": cint(gl.n),
		"gl_debit": flt(gl.d),
		"gl_credit": flt(gl.c),
		"gl_balanced": balanced,
		"qty0_nonzero_value": cint(qty0_val),
	}


def _primary_identity(voucher_type: str, voucher_no: str) -> tuple[str | None, str | None]:
	row = frappe.db.sql(
		"""
		select item_code, warehouse from `tabStock Ledger Entry`
		where voucher_type=%s and voucher_no=%s and is_cancelled=0
		order by abs(stock_value_difference) desc, creation asc
		limit 1
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	if not row:
		return None, None
	return row[0].item_code, row[0].warehouse


def _voucher_docstatus(voucher_type: str, voucher_no: str) -> int | None:
	if not voucher_type or not voucher_no:
		return None
	if not frappe.db.exists(voucher_type, voucher_no):
		return None
	return cint(frappe.db.get_value(voucher_type, voucher_no, "docstatus"))


def _classify_item(row: dict, *, force_check_reject: bool = False) -> dict[str, Any]:
	vt, vn = row["voucher_type"], row["voucher_no"]
	item_status = row.get("item_status")
	docstatus = _voucher_docstatus(vt, vn)
	item_code, warehouse = _primary_identity(vt, vn)
	ledger = _sle_gl_ok(vt, vn)
	reject_issues = []
	if vt == "Stock Entry" and docstatus == 1:
		reject_issues = manufacture_product_reject_rate_issues(vn) or []

	original_issue = row.get("error_message") or ""
	if reject_issues:
		original_issue = original_issue or reject_issues[0].get("reason") or "zero_reject_rate"
	elif docstatus == 2:
		original_issue = original_issue or "voucher cancelled (docstatus=2); no live SLE/GL expected"

	finding = STATUS_BLOCKED_UNKNOWN
	action = "Investigate manually"
	if docstatus == 2:
		# Cancelled vouchers remain in historical manifests but must not be rebuilt.
		finding = STATUS_NOT_APPLICABLE
		action = "None — cancelled voucher; exclude from Sequential rebuild / mark skipped"
	elif docstatus in (None,):
		finding = STATUS_BLOCKED_UNKNOWN
		action = "Voucher missing from database"
	elif reject_issues:
		finding = STATUS_REQUIRES_REBUILD
		action = "Source-repair reject valuation then Item+WH RIV"
	elif item_status == ITEM_SKIPPED and not reject_issues and ledger["gl_balanced"] and not ledger["qty0_nonzero_value"]:
		# Covered and currently invariant-clean.
		finding = STATUS_ALREADY_COVERED_BY_RIV
		action = "None — Sequential coverage holds; invariants pass"
	elif item_status == ITEM_COMPLETED and ledger["gl_balanced"] and not ledger["qty0_nonzero_value"]:
		finding = STATUS_ALREADY_CORRECTED
		action = "None"
	elif item_status in (ITEM_PENDING, ITEM_RIV_LINKED):
		if not ledger["sle_count"]:
			finding = STATUS_REQUIRES_REBUILD
			action = "Create/execute covering Item+WH RIV"
		elif not ledger["gl_balanced"] or ledger["qty0_nonzero_value"]:
			finding = STATUS_REQUIRES_REBUILD
			action = "Rebuild SLE/GL via Sequential RIV"
		else:
			# Pending but currently looks healthy — coverage lag / paused campaign.
			finding = STATUS_ALREADY_CORRECTED
			action = "Mark coverage / finalize sequential (ledger already invariant-clean)"
	elif item_status in (ITEM_FAILED, ITEM_BLOCKED):
		finding = STATUS_BLOCKED_UNKNOWN
		action = "Inspect error_message / Failed RIV identity"
	elif force_check_reject and not reject_issues:
		finding = STATUS_NOT_APPLICABLE
		action = "None"

	return {
		"case": row.get("name"),
		"item": item_code,
		"warehouse": warehouse,
		"source_voucher": f"{vt}/{vn}",
		"voucher_type": vt,
		"voucher_no": vn,
		"posting_date": str(row.get("posting_date") or ""),
		"docstatus": docstatus,
		"original_issue": original_issue or "(none recorded on Campaign Item)",
		"previous_status": item_status,
		"current_finding": finding,
		"required_action": action,
		"reject_issues": reject_issues,
		"ledger": ledger,
		"verification_class": row.get("verification_class") or "",
		"covering_riv": row.get("covering_riv") or "",
	}


def reassess_campaign_cases(
	campaign: str,
	*,
	limit: int | None = None,
	include_skipped_covered_zero_reject: bool = True,
) -> dict[str, Any]:
	"""Reassess pending (+ optional Skipped Covered zero-reject) campaign items."""
	if not frappe.db.exists("Historical Rebuild Campaign", campaign):
		frappe.throw(f"Campaign {campaign} not found")

	pending_statuses = [ITEM_PENDING, ITEM_RIV_LINKED, ITEM_FAILED, ITEM_BLOCKED]
	rows = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign, "item_status": ("in", pending_statuses)},
		fields=[
			"name",
			"voucher_type",
			"voucher_no",
			"item_status",
			"posting_date",
			"error_message",
			"verification_class",
			"covering_riv",
		],
		order_by="posting_date asc, ordinal asc",
		limit_page_length=cint(limit) or 0,
	)

	cases = [_classify_item(r) for r in rows]

	extra_reject = []
	if include_skipped_covered_zero_reject:
		# Skipped Covered Manufacture that still carry illegal zero reject rates.
		covered = frappe.get_all(
			ITEM_DOCTYPE,
			filters={
				"campaign": campaign,
				"item_status": ITEM_SKIPPED,
				"voucher_type": "Stock Entry",
			},
			fields=[
				"name",
				"voucher_type",
				"voucher_no",
				"item_status",
				"posting_date",
				"error_message",
				"verification_class",
				"covering_riv",
			],
			order_by="posting_date asc, ordinal asc",
			limit_page_length=cint(limit) or 0,
		)
		for r in covered:
			issues = manufacture_product_reject_rate_issues(r.voucher_no)
			if not issues:
				continue
			classified = _classify_item(r, force_check_reject=True)
			extra_reject.append(classified)

	summary: dict[str, int] = {}
	for c in cases + extra_reject:
		summary[c["current_finding"]] = summary.get(c["current_finding"], 0) + 1

	# Identify previously "open for unknown reasons": pending with no error and no reject issue.
	open_unknown = [
		c
		for c in cases
		if c["previous_status"] in (ITEM_PENDING, ITEM_RIV_LINKED)
		and c["current_finding"] == STATUS_BLOCKED_UNKNOWN
	]
	# Prefer the paused-coverage set: pending with clean ledger but not marked covered.
	paused_clean = [
		c
		for c in cases
		if c["previous_status"] in (ITEM_PENDING, ITEM_RIV_LINKED)
		and c["current_finding"] == STATUS_ALREADY_CORRECTED
	]
	requires = [c for c in cases + extra_reject if c["current_finding"] == STATUS_REQUIRES_REBUILD]

	return {
		"campaign": campaign,
		"pending_count": len(cases),
		"skipped_covered_zero_reject_count": len(extra_reject),
		"summary": summary,
		"cases": cases,
		"skipped_covered_zero_reject": extra_reject,
		"requires_rebuild": requires,
		"open_unknown": open_unknown,
		"paused_clean_pending": paused_clean,
		"read_only": True,
	}
