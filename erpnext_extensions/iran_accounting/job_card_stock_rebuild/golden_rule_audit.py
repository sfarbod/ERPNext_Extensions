# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only Job Card Golden Rule Audit (v5.5.10).

Date basis: Job Card.posting_date.
Grain: Job Card × Component Item (batch ignored for this report only).

Reuses scan_golden_rule evidence — does not repair and does not change
batch-sensitive Stock Rebuild / Manufacture planner semantics.
"""

from __future__ import annotations

from collections import defaultdict
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


ST_BALANCED = "BALANCED"
ST_REVIEW = "REVIEW"

# Concise reasons (never BATCH_MISMATCH)
REASON_MISSING_CONSUMPTION = "MISSING_CONSUMPTION"
REASON_UNEXPLAINED_WIP = "UNEXPLAINED_WIP"
REASON_OVER_CONSUMED = "OVER_CONSUMED"
REASON_OVER_RETURNED = "OVER_RETURNED"
REASON_MULTIPLE_MANUFACTURE = "MULTIPLE_MANUFACTURE"
REASON_MATERIAL_ISSUE_REVIEW = "MATERIAL_ISSUE_REVIEW"
REASON_AMBIGUOUS_OWNERSHIP = "AMBIGUOUS_OWNERSHIP"
REASON_BLOCKED = "BLOCKED"
REASON_SCRAP_PAIR_MISMATCH = "SCRAP_PAIR_MISMATCH"

DATE_BASIS = "Job Card.posting_date"


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
		if m.get("shared_document") or "AMBIGUOUS" in str(m.get("reason") or "").upper():
			labels.append("AMBIGUOUS")
	seen: set[str] = set()
	out = []
	for x in labels:
		if x not in seen:
			seen.add(x)
			out.append(x)
	return ", ".join(out)


def _aggregate_item_rows(scan_rows: list[dict]) -> list[dict]:
	"""Collapse Job Card × Item × Batch scan rows → Job Card × Item."""
	by_item: dict[str, dict[str, Any]] = {}
	engine_flags: dict[str, set[str]] = defaultdict(set)
	for r in scan_rows or []:
		item = (r.get("item_code") or "").strip()
		if not item:
			continue
		cur = by_item.setdefault(
			item,
			{
				"item_code": item,
				"item_name": r.get("item_name") or "",
				"issued": 0.0,
				"returned": 0.0,
				"consumed": 0.0,
				"mi_consumed": 0.0,
				"scrap": 0.0,
				"suggested_action": "",
				"confidence": "",
			},
		)
		cur["issued"] += flt(r.get("issued"))
		cur["returned"] += flt(r.get("returned"))
		cur["consumed"] += flt(r.get("consumed"))
		cur["mi_consumed"] += flt(r.get("mi_consumed"))
		cur["scrap"] += flt(r.get("scrap"))
		if not cur["item_name"] and r.get("item_name"):
			cur["item_name"] = r["item_name"]
		# Prefer non-empty suggestion from any batch row (display only)
		if not cur["suggested_action"] and r.get("suggested_action"):
			cur["suggested_action"] = r.get("suggested_action") or ""
			cur["confidence"] = r.get("confidence") or ""
		st = r.get("status") or ""
		if st:
			engine_flags[item].add(st)

	out = []
	for item, cur in sorted(by_item.items()):
		# Physical Golden Rule at item level — scrap is not a second WIP drain
		remaining = (
			flt(cur["issued"])
			- flt(cur["returned"])
			- flt(cur["consumed"])
			- flt(cur["mi_consumed"])
		)
		cur["remaining_wip"] = remaining
		cur["engine_statuses"] = engine_flags.get(item) or set()
		out.append(cur)
	return out


def _item_status_and_reason(
	agg: dict,
	*,
	multi_mfg: bool,
	mi_label: str,
	blocked: bool,
) -> tuple[str, str]:
	"""Return (BALANCED|REVIEW, reason). Batch identity never affects this."""
	issued = flt(agg.get("issued"))
	returned = flt(agg.get("returned"))
	consumed = flt(agg.get("consumed"))
	mi = flt(agg.get("mi_consumed"))
	rem = flt(agg.get("remaining_wip"))
	flags = agg.get("engine_statuses") or set()

	if blocked or STATUS_BLOCKED in flags:
		return ST_REVIEW, REASON_BLOCKED
	if "AMBIGUOUS" in (mi_label or "").upper():
		return ST_REVIEW, REASON_AMBIGUOUS_OWNERSHIP
	if returned > issued + 1e-6:
		return ST_REVIEW, REASON_OVER_RETURNED
	if rem < -1e-6:
		return ST_REVIEW, REASON_OVER_CONSUMED
	if STATUS_SCRAP_MISMATCH in flags:
		# Unpaired scrap signal from engine — still item-level, not batch mismatch
		return ST_REVIEW, REASON_SCRAP_PAIR_MISMATCH
	if multi_mfg or STATUS_MULTIPLE_MANUFACTURE in flags:
		return ST_REVIEW, REASON_MULTIPLE_MANUFACTURE
	if rem > 1e-6:
		if consumed <= 1e-9 and mi <= 1e-9:
			return ST_REVIEW, REASON_MISSING_CONSUMPTION
		if STATUS_MISSING_CONSUMPTION in flags:
			return ST_REVIEW, REASON_MISSING_CONSUMPTION
		if STATUS_UNRESOLVED_WIP in flags:
			return ST_REVIEW, REASON_UNEXPLAINED_WIP
		# Proven MI outflow with leftover still needs attention
		if mi > 1e-9 and mi_label:
			return ST_REVIEW, REASON_MATERIAL_ISSUE_REVIEW
		return ST_REVIEW, REASON_UNEXPLAINED_WIP
	# Item-level Golden Rule balanced — batch identity ignored
	return ST_BALANCED, ""


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
	"""Execute read-only Job Card × Item audit. Returns rows + summary counts."""
	filters = frappe._dict(filters or {})
	show_balanced = cint(filters.get("show_balanced"))
	item_filter = (filters.get("item") or "").strip()
	status_filter = (filters.get("status") or "").strip().upper()

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
		"total_item_rows": 0,
		"balanced_item_rows": 0,
		"review_item_rows": 0,
		# retained for older UI/tests (mapped from simplified statuses)
		"golden_rule_failures": 0,
		"multiple_manufacture_job_cards": 0,
		"merge_review_job_cards": 0,
		"material_issue_review_job_cards": 0,
		"blocked_job_cards": 0,
		"exception_rows": 0,
		"balanced_rows": 0,
	}

	for jc in job_cards:
		if not jc.name:
			continue
		scan = scan_golden_rule(jc.name)
		mi_docs = discover_material_issues(jc.name, scan.get("evidence_items") or [])
		mi_label = _mi_review_label(mi_docs)
		mfg_count = len(scan.get("manufactures") or [])
		multi = bool(scan.get("multiple_manufacture"))
		mi_count = len(mi_docs)
		blocked = bool(scan.get("stamp_conflict"))

		aggregated = _aggregate_item_rows(scan.get("rows") or [])
		item_statuses: list[str] = []
		jc_rows: list[dict] = []

		# Resolve item names in one query if missing
		need_names = [a["item_code"] for a in aggregated if not a.get("item_name")]
		name_map = {}
		if need_names:
			name_map = dict(
				frappe.db.sql(
					"select name, item_name from `tabItem` where name in %s",
					(need_names,),
				)
			)

		for agg in aggregated:
			if name_map and not agg.get("item_name"):
				agg["item_name"] = name_map.get(agg["item_code"]) or ""

			primary, reason = _item_status_and_reason(
				agg, multi_mfg=multi, mi_label=mi_label, blocked=blocked
			)
			# Always roll up every item for Job Card summary (ignore display filters)
			item_statuses.append(primary)
			counts["total_item_rows"] += 1
			is_review = primary == ST_REVIEW
			if is_review:
				counts["review_item_rows"] += 1
				counts["exception_rows"] += 1
			else:
				counts["balanced_item_rows"] += 1
				counts["balanced_rows"] += 1

			# Display filters only affect emitted rows
			if item_filter and agg["item_code"] != item_filter:
				continue
			if not show_balanced and not is_review:
				continue
			if status_filter and status_filter not in (primary, reason):
				continue

			sug = agg.get("suggested_action") or ""
			if sug == "SCRAP":
				sug = "COMPONENT SCRAP"

			jc_rows.append(
				{
					"job_card": jc.name,
					"job_card_status": jc.status,
					"work_order": jc.work_order,
					"production_item": jc.finished_good or jc.production_item,
					"component_item": agg["item_code"],
					"item_name": agg.get("item_name") or "",
					"issued_qty": flt(agg["issued"]),
					"returned_qty": flt(agg["returned"]),
					"manufacture_consumed_qty": flt(agg["consumed"]),
					"component_scrap_qty": flt(agg["scrap"]),
					"other_proven_outflow": flt(agg["mi_consumed"]),
					"remaining_wip": flt(agg["remaining_wip"]),
					"golden_status": primary,
					"reason": reason,
					"manufacture_count": mfg_count,
					"material_issue_count": mi_count,
					"material_issue_review": mi_label,
					"suggested_disposition": sug or "",
					"confidence": agg.get("confidence") or "",
					"posting_date": str(jc.posting_date),
					"open_rebuild": jc.name,
				}
			)

		# Multi-MFG with no component rows still needs a REVIEW marker
		if multi and not jc_rows and not show_balanced and not item_filter:
			jc_rows.append(
				{
					"job_card": jc.name,
					"job_card_status": jc.status,
					"work_order": jc.work_order,
					"production_item": jc.finished_good or jc.production_item,
					"component_item": "",
					"item_name": "",
					"issued_qty": 0,
					"returned_qty": 0,
					"manufacture_consumed_qty": 0,
					"component_scrap_qty": 0,
					"other_proven_outflow": 0,
					"remaining_wip": 0,
					"golden_status": ST_REVIEW,
					"reason": REASON_MULTIPLE_MANUFACTURE,
					"manufacture_count": mfg_count,
					"material_issue_count": mi_count,
					"material_issue_review": mi_label,
					"suggested_disposition": "MANUAL REVIEW",
					"confidence": "HIGH",
					"posting_date": str(jc.posting_date),
					"open_rebuild": jc.name,
				}
			)
			item_statuses.append(ST_REVIEW)
			counts["review_item_rows"] += 1
			counts["exception_rows"] += 1
			counts["total_item_rows"] += 1

		# Job Card rollup: any REVIEW item → REVIEW
		if not item_statuses:
			summary = ST_BALANCED if not blocked and not multi else ST_REVIEW
		elif any(s == ST_REVIEW for s in item_statuses):
			summary = ST_REVIEW
		else:
			summary = ST_BALANCED
		if blocked:
			summary = ST_REVIEW

		jc_summaries[jc.name] = summary
		if summary == ST_BALANCED:
			counts["balanced_job_cards"] += 1
		else:
			counts["review_job_cards"] += 1
			counts["golden_rule_failures"] += 1
		if multi:
			counts["multiple_manufacture_job_cards"] += 1
			counts["merge_review_job_cards"] += 1
		if mi_label and any(x in mi_label for x in ("MERGE", "USER", "FINANCE")):
			counts["material_issue_review_job_cards"] += 1
		if blocked:
			counts["blocked_job_cards"] += 1

		for rec in jc_rows:
			rec["job_card_summary"] = summary
			rows_out.append(rec)

	# Hard guarantee: every output row has a Job Card
	rows_out = [r for r in rows_out if (r.get("job_card") or "").strip()]

	return {
		"date_basis": DATE_BASIS,
		"grain": "Job Card × Item",
		"rows": rows_out,
		"counts": counts,
		"job_card_summaries": jc_summaries,
		"filters": dict(filters),
	}
