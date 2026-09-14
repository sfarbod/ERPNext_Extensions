# Copyright (c) 2026, ERPNext Extensions contributors
"""Recalculate Work Order / Job Card manufacturing counters after a reference move.

Preserves Work Order.qty exactly. Bypasses only the native planning-capacity check in
WorkOrder.update_operation_status that refuses completed_qty > WO.qty — that check is
valid for new manufacturing transactions, not for historical reference repair.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt


def refresh_job_card(job_card_name: str) -> None:
	jc = frappe.get_doc("Job Card", job_card_name)
	jc.set_transferred_qty(update_status=True)
	if cint_docstatus(jc) == 1:
		# Prefer SFG path that db_sets manufactured counters without capacity throw.
		jc.set_manufactured_qty()
	else:
		jc.set_status(update_status=True)


def cint_docstatus(doc) -> int:
	return int(getattr(doc, "docstatus", 0) or 0)


def _zero_operation(op_name: str) -> None:
	frappe.db.set_value(
		"Work Order Operation",
		op_name,
		{
			"completed_qty": 0,
			"process_loss_qty": 0,
			"pending_qty": 0,
			"actual_operation_time": 0,
			"actual_operating_cost": 0,
			"actual_start_time": None,
			"actual_end_time": None,
			"status": "Pending",
		},
		update_modified=False,
	)


def update_operation_status_allow_historical_excess(wo) -> None:
	"""Mirror WorkOrder.update_operation_status without the planned-qty capacity throw.

	Native ERPNext (work_order.py::update_operation_status) raises when
	completed_qty + process_loss_qty > WO.qty (+ overproduction %).

	For this repair tool, historical Job Card totals may exceed the original planned
	WO.qty. Status is still derived from planned qty, but excess never mutates WO.qty
	and never aborts recalculation.
	"""
	planned = flt(wo.qty)
	for d in wo.get("operations") or []:
		precision = d.precision("completed_qty") if hasattr(d, "precision") else 6
		qty = flt(flt(d.completed_qty, precision) + flt(d.process_loss_qty, precision), precision)
		if not qty:
			d.status = "Pending"
		elif planned and qty < flt(planned, precision):
			d.status = "Work in Progress"
		else:
			# qty >= planned, or planned is 0 with historical qty present
			d.status = "Completed"


def _aggregate_operation_from_job_cards(work_order: str, operation_id: str) -> dict:
	"""Derive operation execution counters from Job Cards without calling update_work_order."""
	rows = frappe.get_all(
		"Job Card",
		filters={
			"work_order": work_order,
			"operation_id": operation_id,
			"docstatus": 1,
			"is_corrective_job_card": 0,
		},
		fields=[
			"manufactured_qty",
			"total_completed_qty",
			"process_loss_qty",
			"pending_qty",
			"total_time_in_mins",
			"workstation",
			"hour_rate",
		],
	)
	completed = 0.0
	process_loss = 0.0
	pending = 0.0
	time_in_mins = 0.0
	for row in rows:
		completed += max(flt(row.manufactured_qty), flt(row.total_completed_qty))
		process_loss += flt(row.process_loss_qty)
		pending += flt(row.pending_qty)
		time_in_mins += flt(row.total_time_in_mins)

	time_data = frappe.db.sql(
		"""
		SELECT MIN(jctl.from_time) AS start_time, MAX(jctl.to_time) AS end_time
		FROM `tabJob Card` jc
		INNER JOIN `tabJob Card Time Log` jctl ON jctl.parent = jc.name
		WHERE jc.work_order = %s
			AND jc.operation_id = %s
			AND jc.docstatus = 1
			AND IFNULL(jc.is_corrective_job_card, 0) = 0
		""",
		(work_order, operation_id),
		as_dict=True,
	)
	start_time = time_data[0].start_time if time_data else None
	end_time = time_data[0].end_time if time_data else None

	hour_rate = 0.0
	workstation = None
	if rows:
		workstation = rows[0].workstation
		hour_rate = flt(rows[0].hour_rate)
		if workstation and not hour_rate:
			hour_rate = flt(frappe.db.get_value("Workstation", workstation, "hour_rate"))

	return {
		"completed_qty": completed,
		"process_loss_qty": process_loss,
		"pending_qty": pending,
		"actual_operation_time": time_in_mins,
		"actual_operating_cost": (hour_rate / 60.0) * time_in_mins if time_in_mins else 0.0,
		"actual_start_time": start_time,
		"actual_end_time": end_time,
		"workstation": workstation,
	}


def recalculate_work_order(work_order: str) -> None:
	"""Rebuild WO derived counters from Job Cards / Stock Entries. Never changes WO.qty."""
	planned_qty = flt(frappe.db.get_value("Work Order", work_order, "qty"))
	wo = frappe.get_doc("Work Order", work_order)
	wo.flags.ignore_validate_update_after_submit = True
	wo.flags.job_card_reassign_historical = True

	submitted = frappe.get_all(
		"Job Card",
		filters={
			"work_order": work_order,
			"docstatus": 1,
			"is_corrective_job_card": 0,
		},
		fields=["name", "operation_id"],
		order_by="creation",
	)
	by_op: dict[str, list[str]] = defaultdict(list)
	for row in submitted:
		if row.operation_id:
			by_op[row.operation_id].append(row.name)

	# Refresh Job Card transferred/manufactured counters first (no WO.qty mutation).
	for op in wo.operations:
		names = by_op.get(op.name) or []
		if not names:
			_zero_operation(op.name)
			continue
		for jc_name in names:
			jc = frappe.get_doc("Job Card", jc_name)
			jc.set_transferred_qty(update_status=False)
			jc.set_manufactured_qty()

	wo.reload()
	wo.flags.ignore_validate_update_after_submit = True
	wo.flags.job_card_reassign_historical = True

	# Apply aggregated operation counters without native update_work_order capacity gate.
	for op in wo.operations:
		names = by_op.get(op.name) or []
		if not names:
			op.completed_qty = 0
			op.process_loss_qty = 0
			op.pending_qty = 0
			op.actual_operation_time = 0
			op.actual_operating_cost = 0
			op.actual_start_time = None
			op.actual_end_time = None
			continue
		agg = _aggregate_operation_from_job_cards(work_order, op.name)
		op.completed_qty = agg["completed_qty"]
		op.process_loss_qty = agg["process_loss_qty"]
		op.pending_qty = agg["pending_qty"]
		op.actual_operation_time = agg["actual_operation_time"]
		op.actual_operating_cost = agg["actual_operating_cost"]
		op.actual_start_time = agg["actual_start_time"]
		op.actual_end_time = agg["actual_end_time"]
		if agg.get("workstation"):
			op.workstation = agg["workstation"]

	update_operation_status_allow_historical_excess(wo)
	wo.calculate_operating_cost()
	wo.set_actual_dates()
	if wo.track_semi_finished_goods:
		wo.set_process_loss_qty()
		_refresh_sfg_produced_qty(wo)
	else:
		# update_work_order_qty can throw StockOverProductionError when produced > qty.
		# Aggregate transferred/produced from Stock Entries while preserving WO.qty.
		_refresh_non_sfg_counters(wo)

	corrective = frappe.get_all(
		"Job Card",
		filters={"work_order": work_order, "docstatus": 1, "is_corrective_job_card": 1},
		pluck="name",
		limit=1,
	)
	if corrective:
		jc = frappe.get_doc("Job Card", corrective[0])
		jc.update_corrective_in_work_order(wo)

	# Hard guarantee: planned qty never changes during repair recalc.
	wo.qty = planned_qty
	wo.flags.ignore_validate_update_after_submit = True
	wo.save()
	frappe.db.set_value("Work Order", work_order, "qty", planned_qty, update_modified=False)

	wo.reload()
	wo.flags.ignore_validate_update_after_submit = True
	wo.qty = planned_qty
	wo.update_status()
	# update_status/save paths must not leave qty altered.
	if flt(frappe.db.get_value("Work Order", work_order, "qty")) != planned_qty:
		frappe.db.set_value("Work Order", work_order, "qty", planned_qty, update_modified=False)


def _refresh_sfg_produced_qty(wo) -> None:
	rows = frappe.get_all(
		"Job Card",
		filters={
			"work_order": wo.name,
			"docstatus": 1,
			"is_corrective_job_card": 0,
			"finished_good": wo.production_item,
		},
		fields=["manufactured_qty"],
	)
	produced = sum(flt(r.manufactured_qty) for r in rows)
	wo.db_set("produced_qty", produced)
	wo.produced_qty = produced


def _refresh_non_sfg_counters(wo) -> None:
	"""Set produced/transferred from Stock Entries without capacity validation against WO.qty."""
	wo.set_process_loss_qty()
	for purpose, fieldname in (
		("Manufacture", "produced_qty"),
		("Material Transfer for Manufacture", "material_transferred_for_manufacturing"),
	):
		qty = wo.get_transferred_or_manufactured_qty(purpose, fieldname)
		wo.db_set(fieldname, qty)
		setattr(wo, fieldname, qty)
