# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical scan of Manufacture output contract (5.2.0 semantics, no policy change)."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	RATE_EPS,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_RECONSTRUCTABLE,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.scrap_costing import apply_iran_manufacture_output_contract


def scan_manufacture_anomalies(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
	limit=400,
) -> dict:
	conds = ["se.docstatus=1", "se.purpose='Manufacture'"]
	args: list = []
	join = ""
	if company:
		conds.append("se.company=%s")
		args.append(company)
	if voucher:
		conds.append("se.name=%s")
		args.append(voucher)
	if work_order:
		conds.append("se.work_order=%s")
		args.append(work_order)
	if from_date:
		conds.append("se.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("se.posting_date<=%s")
		args.append(to_date)
	if item_code or warehouse:
		join = " JOIN `tabStock Entry Detail` sed ON sed.parent=se.name "
		if item_code:
			conds.append("sed.item_code=%s")
			args.append(item_code)
		if warehouse:
			conds.append("(sed.s_warehouse=%s OR sed.t_warehouse=%s)")
			args.extend([warehouse, warehouse])
	names = frappe.db.sql(
		f"""
		SELECT DISTINCT se.name FROM `tabStock Entry` se
		{join}
		WHERE {" AND ".join(conds)}
		ORDER BY se.posting_date, se.creation
		LIMIT {int(limit)}
		""",
		args,
		pluck=True,
	)
	rows = []
	for name in names:
		preview = preview_manufacture_voucher(name)
		if preview.get("needs_repair"):
			rows.append(preview)
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result({"count": len(rows), "rows": rows, "scanned": len(names)})


def preview_manufacture_voucher(voucher_no: str) -> dict:
	doc = frappe.get_doc("Stock Entry", voucher_no)
	before = _snapshot(doc)
	try:
		apply_iran_manufacture_output_contract(doc)
		after = _snapshot(doc)
	finally:
		_restore(doc, before)
	changed = []
	for b, a in zip(before["rows"], after["rows"], strict=False):
		if abs(flt(b["basic_rate"]) - flt(a["basic_rate"])) > RATE_EPS or abs(
			flt(b["amount"]) - flt(a["amount"])
		) > VALUE_EPS:
			changed.append({"idx": b["idx"], "item": b["item_code"], "before": b, "after": a})
	fg_neg = any(flt(r["amount"]) < 0 or flt(r["basic_rate"]) < 0 for r in before["rows"] if r["is_finished_item"])
	needs = bool(changed) or fg_neg
	confidence = CONFIDENCE_EXACT if changed and not fg_neg else (
		CONFIDENCE_LIKELY if needs else CONFIDENCE_AMBIGUOUS
	)
	if not needs:
		confidence = CONFIDENCE_EXACT
	status = STATUS_RECONSTRUCTABLE if needs and confidence == CONFIDENCE_EXACT else (
		STATUS_MANUAL_REVIEW if needs else "HEALTHY"
	)
	zero_rm = any(
		flt(r["qty"]) and abs(flt(r["basic_rate"])) < RATE_EPS and r["s_warehouse"]
		for r in before["rows"]
	)
	if zero_rm and needs:
		status = STATUS_DEPENDENCY_REPAIR_REQUIRED
		confidence = CONFIDENCE_AMBIGUOUS
	return {
		"topic": "MANUFACTURE",
		"voucher": voucher_no,
		"purpose": "Manufacture",
		"needs_repair": needs,
		"changed_rows": changed,
		"fg_negative": fg_neg,
		"confidence": confidence,
		"status": status,
		"eligible": status == STATUS_RECONSTRUCTABLE,
		"source_of_truth": "5.2.0_manufacture_output_contract",
		"patient_zero": {"voucher_no": voucher_no} if needs else None,
	}


def apply_manufacture_preview_to_doc(doc) -> bool:
	return bool(apply_iran_manufacture_output_contract(doc))


def _snapshot(doc) -> dict:
	rows = []
	for d in doc.get("items") or []:
		rows.append(
			{
				"idx": d.idx,
				"name": d.name,
				"item_code": d.item_code,
				"qty": flt(d.qty),
				"basic_rate": flt(d.basic_rate),
				"basic_amount": flt(d.basic_amount),
				"valuation_rate": flt(d.valuation_rate),
				"amount": flt(d.amount),
				"additional_cost": flt(d.additional_cost),
				"is_finished_item": d.is_finished_item,
				"secondary_item_type": d.secondary_item_type,
				"s_warehouse": d.s_warehouse,
				"t_warehouse": d.t_warehouse,
			}
		)
	return {"rows": rows}


def _restore(doc, snap) -> None:
	by_name = {r["name"]: r for r in snap["rows"]}
	for d in doc.get("items") or []:
		src = by_name.get(d.name)
		if not src:
			continue
		d.basic_rate = src["basic_rate"]
		d.basic_amount = src["basic_amount"]
		d.valuation_rate = src["valuation_rate"]
		d.amount = src["amount"]
		d.additional_cost = src["additional_cost"]
