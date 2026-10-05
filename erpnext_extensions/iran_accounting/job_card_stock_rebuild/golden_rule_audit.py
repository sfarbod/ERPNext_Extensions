# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only Job Card Golden Rule Audit for date ranges (v5.5.0).

Date basis: Job Card.posting_date (authoritative manufacturing posting date).
Grain: Job Card × Component Item × Batch.
Reuses scan_golden_rule + MI ownership — does not repair.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt, getdate

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	STATUS_BLOCKED,
	STATUS_MISSING_CONSUMPTION,
	STATUS_MULTIPLE_MANUFACTURE,
	STATUS_OK,
	STATUS_SCRAP_MISMATCH,
	STATUS_UNRESOLVED_WIP,
	scan_golden_rule,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.mi_ownership import (
	MI_BLOCKED,
	MI_FINANCE_REVIEW,
	MI_MERGE_SAFE,
	MI_USER_DECISION,
	discover_material_issues,
)


# Report statuses (normalized labels)
ST_MISSING_CONSUMPTION = "MISSING_CONSUMPTION"
ST_UNEXPLAINED_WIP = "UNEXPLAINED_WIP"
ST_MISSING_RETURN = "MISSING_RETURN"
ST_OVER_CONSUMED = "OVER_CONSUMED"
ST_OVER_RETURNED = "OVER_RETURNED"
ST_SCRAP_PAIR_MISMATCH = "SCRAP_PAIR_MISMATCH"
ST_MULTIPLE_MANUFACTURE = "MULTIPLE_MANUFACTURE"
ST_MERGE_REVIEW = "MERGE_REVIEW"
ST_MATERIAL_ISSUE_REVIEW = "MATERIAL_ISSUE_REVIEW"
ST_BATCH_MISMATCH = "BATCH_MISMATCH"
ST_AMBIGUOUS_OWNERSHIP = "AMBIGUOUS_OWNERSHIP"
ST_BLOCKED = "BLOCKED"
ST_BALANCED = "BALANCED"

DATE_BASIS = "Job Card.posting_date"


def _map_row_status(row: dict, multi_mfg: bool) -> str:
	"""Map Golden Rule engine status + qty math to report exception labels."""
	issued = flt(row.get("issued"))
	returned = flt(row.get("returned"))
	consumed = flt(row.get("consumed"))
	mi = flt(row.get("mi_consumed"))
	rem = flt(row.get("remaining_wip"))
	engine = row.get("status") or ""

	if returned > issued + 1e-6:
		return ST_OVER_RETURNED
	if rem < -1e-6:
		return ST_OVER_CONSUMED
	if engine == STATUS_SCRAP_MISMATCH:
		return ST_SCRAP_PAIR_MISMATCH
	if engine == STATUS_MISSING_CONSUMPTION:
		return ST_MISSING_CONSUMPTION
	if engine == STATUS_UNRESOLVED_WIP:
		# Prefer MISSING_RETURN when unused remainder and no consume
		if consumed <= 1e-9 and mi <= 1e-9:
			return ST_MISSING_RETURN
		return ST_UNEXPLAINED_WIP
	if engine == STATUS_BLOCKED:
		return ST_BLOCKED
	if engine == STATUS_MULTIPLE_MANUFACTURE:
		return ST_MULTIPLE_MANUFACTURE
	if engine == STATUS_OK:
		return ST_BALANCED
	if rem > 1e-6:
		return ST_UNEXPLAINED_WIP
	return ST_BALANCED


def _mi_review_label(mi_docs: list[dict]) -> str:
	if not mi_docs:
		return ""
	labels = []
	for m in mi_docs:
		cls = m.get("classification")
		if cls == MI_MERGE_SAFE:
			labels.append("MERGE SAFE")
		elif cls == MI_USER_DECISION:
			labels.append("USER DECISION")
		elif cls == MI_FINANCE_REVIEW:
			labels.append("FINANCE REVIEW")
		elif cls == MI_BLOCKED:
			labels.append("BLOCKED")
		if m.get("shared_document") or m.get("reason") and "AMBIGUOUS" in str(m.get("reason") or "").upper():
			labels.append("AMBIGUOUS")
	# unique preserve order
	seen = set()
	out = []
	for x in labels:
		if x not in seen:
			seen.add(x)
			out.append(x)
	return ", ".join(out)


def _jc_summary_status(row_statuses: list[str], multi_mfg: bool, mi_review: str, blocked: bool) -> str:
	"""Priority: BLOCKED > AMBIGUOUS > GOLDEN RULE FAIL > MERGE REVIEW > BALANCED."""
	if blocked or ST_BLOCKED in row_statuses:
		return ST_BLOCKED
	if ST_AMBIGUOUS_OWNERSHIP in row_statuses or "AMBIGUOUS" in (mi_review or ""):
		return ST_AMBIGUOUS_OWNERSHIP
	fail = {
		ST_MISSING_CONSUMPTION,
		ST_UNEXPLAINED_WIP,
		ST_MISSING_RETURN,
		ST_OVER_CONSUMED,
		ST_OVER_RETURNED,
		ST_SCRAP_PAIR_MISMATCH,
		ST_BATCH_MISMATCH,
	}
	if any(s in fail for s in row_statuses):
		return "GOLDEN_RULE_FAIL"
	if multi_mfg or ST_MULTIPLE_MANUFACTURE in row_statuses or ST_MERGE_REVIEW in row_statuses:
		return ST_MERGE_REVIEW
	if mi_review and mi_review != "BLOCKED":
		# MI review alone is MERGE_REVIEW / MATERIAL_ISSUE_REVIEW signal
		if "MERGE SAFE" in mi_review or "USER DECISION" in mi_review or "FINANCE" in mi_review:
			return ST_MATERIAL_ISSUE_REVIEW
	return ST_BALANCED


def list_job_cards_in_range(
	from_date,
	to_date,
	job_card: str | None = None,
	work_order: str | None = None,
) -> list[dict]:
	"""Bulk Job Card header fetch — filtered by posting_date."""
	if not from_date or not to_date:
		frappe.throw(frappe._("From Date and To Date are required"))
	from_date = getdate(from_date)
	to_date = getdate(to_date)
	if from_date > to_date:
		frappe.throw(frappe._("From Date cannot be after To Date"))

	conds = ["posting_date between %(from_date)s and %(to_date)s", "docstatus < 2"]
	params: dict[str, Any] = {"from_date": from_date, "to_date": to_date}
	if job_card:
		conds.append("name = %(job_card)s")
		params["job_card"] = job_card
	if work_order:
		conds.append("work_order = %(work_order)s")
		params["work_order"] = work_order

	return frappe.db.sql(
		f"""
		select name, status, work_order, posting_date, finished_good, production_item,
		       for_quantity, total_completed_qty, company, modified
		from `tabJob Card`
		where {' and '.join(conds)}
		order by posting_date, name
		""",
		params,
		as_dict=1,
	)


def run_golden_rule_audit(filters: dict | None = None) -> dict[str, Any]:
	"""Execute read-only audit. Returns rows + summary counts."""
	filters = frappe._dict(filters or {})
	show_balanced = cint(filters.get("show_balanced"))
	item_filter = (filters.get("item") or "").strip()
	batch_filter = (filters.get("batch") or "").strip()
	status_filter = (filters.get("status") or "").strip()

	job_cards = list_job_cards_in_range(
		filters.get("from_date"),
		filters.get("to_date"),
		job_card=filters.get("job_card"),
		work_order=filters.get("work_order"),
	)

	rows_out: list[dict] = []
	jc_summaries: dict[str, str] = {}
	counts = {
		"total_job_cards": len(job_cards),
		"balanced_job_cards": 0,
		"review_job_cards": 0,
		"golden_rule_failures": 0,
		"multiple_manufacture_job_cards": 0,
		"merge_review_job_cards": 0,
		"material_issue_review_job_cards": 0,
		"blocked_job_cards": 0,
		"exception_rows": 0,
		"balanced_rows": 0,
	}

	for jc in job_cards:
		scan = scan_golden_rule(jc.name)
		mi_docs = discover_material_issues(jc.name, scan.get("evidence_items") or [])
		mi_label = _mi_review_label(mi_docs)
		mfg_count = len(scan.get("manufactures") or [])
		multi = bool(scan.get("multiple_manufacture"))
		mi_count = len(mi_docs)

		# Blocked if stamp conflict or scrap pairing hard-fail at JC level
		blocked = bool(scan.get("stamp_conflict"))
		row_statuses = []
		jc_rows = []

		for r in scan.get("rows") or []:
			if item_filter and r.get("item_code") != item_filter:
				continue
			if batch_filter and (r.get("batch_no") or "") != batch_filter:
				continue
			gst = _map_row_status(r, multi)
			# Elevate MI ambiguous
			if any(
				(m.get("shared_document") and m.get("classification") == MI_BLOCKED)
				for m in mi_docs
			) and gst == ST_BALANCED:
				# keep balanced component unless ownership ambiguous for this key
				pass
			for m in mi_docs:
				reason = str(m.get("reason") or "")
				if "AMBIGUOUS" in reason.upper() and gst == ST_BALANCED:
					gst = ST_AMBIGUOUS_OWNERSHIP
					break

			merge_review = "YES" if multi or any(
				m.get("classification") == MI_MERGE_SAFE for m in mi_docs
			) else "NO"
			if multi and gst == ST_BALANCED:
				# component balanced but JC has multiple mfg — still emit for review when show_balanced
				# or when we want MERGE_REVIEW visibility: emit with MULTIPLE_MANUFACTURE
				gst = ST_MULTIPLE_MANUFACTURE

			is_exception = gst != ST_BALANCED
			row_statuses.append(gst)
			if not show_balanced and not is_exception:
				counts["balanced_rows"] += 1
				continue
			if status_filter and gst != status_filter:
				continue

			sug = r.get("suggested_action") or ""
			if sug == "SCRAP":
				sug = "COMPONENT SCRAP"
			elif sug == "STILL IN WIP":
				sug = "STILL IN WIP"

			# Repair status hint (read-only)
			repair = "NONE"
			if blocked:
				repair = "BLOCKED"
			elif multi:
				repair = "MERGE REVIEW"
			elif gst in (ST_MISSING_CONSUMPTION, ST_UNEXPLAINED_WIP, ST_MISSING_RETURN):
				repair = "REVIEW"
			elif is_exception:
				repair = "REVIEW"

			rec = {
				"job_card": jc.name,
				"job_card_status": jc.status,
				"work_order": jc.work_order,
				"production_item": jc.finished_good or jc.production_item,
				"component_item": r.get("item_code"),
				"batch_no": r.get("batch_no") or "",
				"issued_qty": flt(r.get("issued")),
				"returned_qty": flt(r.get("returned")),
				"manufacture_consumed_qty": flt(r.get("consumed")),
				"component_scrap_qty": flt(r.get("scrap")),
				"other_proven_outflow": flt(r.get("mi_consumed")),
				"remaining_wip": flt(r.get("remaining_wip")),
				"golden_difference": flt(r.get("remaining_wip")),
				"golden_status": gst,
				"manufacture_count": mfg_count,
				"material_issue_count": mi_count,
				"merge_review": merge_review,
				"material_issue_review": mi_label,
				"suggested_disposition": sug or "",
				"confidence": r.get("confidence") or "",
				"repair_status": repair,
				"posting_date": str(jc.posting_date),
				"open_rebuild": jc.name,
			}
			jc_rows.append(rec)
			if is_exception:
				counts["exception_rows"] += 1
			else:
				counts["balanced_rows"] += 1

		# If multi-mfg and all components skipped as balanced (show_balanced=0),
		# still emit one review row so MULTIPLE_MANUFACTURE is visible.
		if multi and not jc_rows and not show_balanced:
			jc_rows.append(
				{
					"job_card": jc.name,
					"job_card_status": jc.status,
					"work_order": jc.work_order,
					"production_item": jc.finished_good or jc.production_item,
					"component_item": "",
					"batch_no": "",
					"issued_qty": 0,
					"returned_qty": 0,
					"manufacture_consumed_qty": 0,
					"component_scrap_qty": 0,
					"other_proven_outflow": 0,
					"remaining_wip": 0,
					"golden_difference": 0,
					"golden_status": ST_MULTIPLE_MANUFACTURE,
					"manufacture_count": mfg_count,
					"material_issue_count": mi_count,
					"merge_review": "YES",
					"material_issue_review": mi_label,
					"suggested_disposition": "MANUAL REVIEW",
					"confidence": "HIGH",
					"repair_status": "MERGE REVIEW",
					"posting_date": str(jc.posting_date),
					"open_rebuild": jc.name,
				}
			)
			row_statuses.append(ST_MULTIPLE_MANUFACTURE)
			counts["exception_rows"] += 1

		summary = _jc_summary_status(row_statuses or [ST_BALANCED], multi, mi_label, blocked)
		jc_summaries[jc.name] = summary
		if summary == ST_BALANCED:
			counts["balanced_job_cards"] += 1
		else:
			counts["review_job_cards"] += 1
		if summary == "GOLDEN_RULE_FAIL":
			counts["golden_rule_failures"] += 1
		if multi:
			counts["multiple_manufacture_job_cards"] += 1
		if summary == ST_MERGE_REVIEW or multi:
			counts["merge_review_job_cards"] += 1
		if mi_label and ("MERGE" in mi_label or "USER" in mi_label or "FINANCE" in mi_label):
			counts["material_issue_review_job_cards"] += 1
		if summary == ST_BLOCKED:
			counts["blocked_job_cards"] += 1

		for rec in jc_rows:
			rec["job_card_summary"] = summary
			rows_out.append(rec)

	return {
		"date_basis": DATE_BASIS,
		"rows": rows_out,
		"counts": counts,
		"job_card_summaries": jc_summaries,
		"filters": dict(filters),
	}
