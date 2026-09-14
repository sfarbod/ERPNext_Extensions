# Copyright (c) 2026, ERPNext Extensions contributors
"""Informational historical qty analysis for Job Card reassignment.

Work Order.qty is planned production quantity and must never be modified by this
repair feature. This module only reports when historical execution exceeds plan.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.stock_extensions.job_card_reassign.mapping import completed_qty_estimate


def _jc_completed_plus_loss(row: dict) -> float:
	return completed_qty_estimate(row) + flt(row.get("process_loss_qty"))


def cstr_fg(value) -> str:
	return (value or "") if isinstance(value, str) else str(value or "")


def analyze_historical_qty(
	*,
	target: dict,
	moved_job_cards: list[dict],
	mappings: dict[str, dict],
) -> dict:
	"""Compare planned Target WO.qty to post-move historical execution totals.

	Never proposes or applies a Work Order.qty change.
	"""
	production_item = target.get("production_item")
	current_qty = flt(target.get("qty"))

	existing = frappe.get_all(
		"Job Card",
		filters={"work_order": target.get("name"), "docstatus": ["!=", 2]},
		fields=[
			"name",
			"operation_id",
			"finished_good",
			"for_quantity",
			"total_completed_qty",
			"manufactured_qty",
			"process_loss_qty",
			"docstatus",
			"operation",
		],
	)
	moved_names = {j["name"] for j in moved_job_cards}
	remaining_existing = [r for r in existing if r.name not in moved_names]
	combined = list(remaining_existing) + list(moved_job_cards)

	final_fg_for_qty = sum(
		flt(r.get("for_quantity"))
		for r in combined
		if production_item and cstr_fg(r.get("finished_good")) == cstr_fg(production_item)
	)

	op_totals: dict[str, float] = defaultdict(float)
	for op in target.get("operations") or []:
		op_totals[op["name"]] = flt(op.get("completed_qty")) + flt(op.get("process_loss_qty"))

	recomputed: dict[str, float] = defaultdict(float)
	for jc in combined:
		if cint(jc.get("docstatus")) != 1:
			continue
		mapped = mappings.get(jc["name"]) if jc["name"] in moved_names else None
		if mapped and mapped.get("ok"):
			op_key = mapped.get("target_operation_id")
		else:
			op_key = jc.get("operation_id")
		if not op_key:
			continue
		recomputed[op_key] += _jc_completed_plus_loss(jc)

	for op in target.get("operations") or []:
		op_id = op["name"]
		if op_id in recomputed:
			op_totals[op_id] = recomputed[op_id]
	for key, total in recomputed.items():
		op_totals[key] = total

	max_op_completed = max(op_totals.values()) if op_totals else 0.0
	exceeds_plan = (
		max_op_completed > current_qty + 1e-9 or final_fg_for_qty > current_qty + 1e-9
	)

	return {
		"planned_qty": current_qty,
		"planned_qty_unchanged": True,
		"qty_change_required": False,
		"historical_exceeds_planned_qty": exceeds_plan,
		"final_fg_for_quantity_sum": flt(final_fg_for_qty),
		"max_operation_completed_plus_loss": flt(max_op_completed),
		"production_item": production_item,
		"operation_totals_after": {k: flt(v) for k, v in op_totals.items()},
		"message": (
			f"Historical execution exceeds current planned Work Order quantity. "
			f"Planned Qty remains unchanged: {current_qty}."
			if exceeds_plan
			else f"Planned Qty remains unchanged: {current_qty}."
		),
	}
