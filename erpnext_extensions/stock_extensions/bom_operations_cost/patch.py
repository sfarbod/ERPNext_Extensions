# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Extension-level backport of ERPNext PR #68701 (issue #68700).

``add_operations_cost`` divides remaining corrective cost by
``max_operation_qty − produced_qty``. On track-semi-finished Work Orders the
unfiltered ``max_qty`` collapses to the finished-good completed qty, so a
semi-finished Job Card Record Output hits ``ZeroDivisionError`` once the FG
has already been produced (PO-JOB10628 / MFG-WO-2026-00846).

Upstream behaviour:
- Skip WO-level corrective (and additional) operating cost on semi-FG Job Cards.
- When ``track_semi_finished_goods``, measure max operation qty only on Job
  Cards whose ``finished_good`` is the Work Order production item.
- Do not append corrective cost when the remaining FG denominator is ≤ 0.
"""

from __future__ import annotations

import frappe
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt

_ATTR_FLAG = "_ee_bom_ops_cost_patched"
_ATTR_ORIG = "_ee_bom_ops_cost_orig"


def makes_semi_finished_good(work_order, job_card=None) -> bool:
	"""True when the Job Card finishes a different item than the WO FG."""
	if not job_card or not work_order:
		return False
	finished_good = job_card.get("finished_good") if hasattr(job_card, "get") else getattr(job_card, "finished_good", None)
	production_item = (
		work_order.get("production_item")
		if hasattr(work_order, "get")
		else getattr(work_order, "production_item", None)
	)
	return bool(finished_good and production_item and finished_good != production_item)


def _corrective_cost_enabled() -> bool:
	return cint(
		frappe.db.get_single_value(
			"Manufacturing Settings",
			"add_corrective_operation_cost_in_finished_good_valuation",
		)
	)


def _max_operation_quantity(work_order) -> float:
	"""Min completed qty across non-corrective Job Cards (FG-only when track SFG)."""
	table = frappe.qb.DocType("Job Card")
	query = (
		frappe.qb.from_(table)
		.select(Sum(table.total_completed_qty).as_("qty"))
		.where(
			(table.docstatus == 1)
			& (table.work_order == work_order.name)
			& (table.is_corrective_job_card == 0)
		)
		.groupby(table.operation)
	)
	if cint(work_order.get("track_semi_finished_goods")):
		# Semi-finished quantities are not finished-good units (PR #68701).
		query = query.where(table.finished_good == work_order.production_item)
	rows = query.run(as_dict=True)
	return min([d.qty for d in rows], default=0)


def _utilised_corrective_cost(work_order) -> float:
	table = frappe.qb.DocType("Stock Entry")
	subquery = (
		frappe.qb.from_(table)
		.select(table.name)
		.where(
			(table.docstatus == 1)
			& (table.work_order == work_order.name)
			& (table.purpose == "Manufacture")
		)
	)
	table = frappe.qb.DocType("Landed Cost Taxes and Charges")
	query = (
		frappe.qb.from_(table)
		.select(Sum(table.amount).as_("amount"))
		.where(table.parent.isin(subquery) & (table.has_corrective_cost == 1))
	)
	row = query.run(as_dict=True)[0]
	return flt(row.amount) if row else 0.0


def add_corrective_operation_cost(stock_entry, work_order, expense_account, job_card=None) -> None:
	"""Append Corrective Operation Cost using PR #68701-safe denominators."""
	if not (work_order and work_order.corrective_operation_cost and _corrective_cost_enabled()):
		return
	if makes_semi_finished_good(work_order, job_card):
		return

	max_qty = flt(_max_operation_quantity(work_order)) - flt(work_order.produced_qty)
	if max_qty <= 0:
		# No remaining FG capacity — avoid ZeroDivisionError; leave cost for a
		# later FG manufacture that still has headroom, or skip when complete.
		return

	remaining = flt(work_order.corrective_operation_cost) - _utilised_corrective_cost(work_order)
	stock_entry.append(
		"additional_costs",
		{
			"expense_account": expense_account,
			"description": "Corrective Operation Cost",
			"has_corrective_cost": 1,
			"amount": remaining / max_qty * flt(stock_entry.fg_completed_qty),
		},
	)


def add_operations_cost(stock_entry, work_order=None, expense_account=None, job_card=None):
	"""Wrap Core ``add_operations_cost`` with PR #68701 corrective-cost rules."""
	import erpnext.manufacturing.doctype.bom.bom as bom_mod

	original = getattr(bom_mod, _ATTR_ORIG, None)
	if original is None:
		original = bom_mod.add_operations_cost

	# Run Core with corrective disabled so the nested ZeroDivision path never
	# executes; then append corrective ourselves with the safe rules.
	saved = None
	if work_order is not None:
		saved = work_order.corrective_operation_cost
		work_order.corrective_operation_cost = 0
	try:
		original(stock_entry, work_order, expense_account, job_card)
	finally:
		if work_order is not None and saved is not None:
			work_order.corrective_operation_cost = saved

	if work_order is not None:
		add_corrective_operation_cost(stock_entry, work_order, expense_account, job_card)


def apply_patch() -> None:
	"""Idempotently replace ``bom.add_operations_cost`` with the safe wrapper."""
	import erpnext.manufacturing.doctype.bom.bom as bom_mod

	if getattr(bom_mod, _ATTR_FLAG, False):
		# Keep the bound wrapper current after hot reload.
		bom_mod.add_operations_cost = add_operations_cost
		return

	setattr(bom_mod, _ATTR_ORIG, bom_mod.add_operations_cost)
	bom_mod.add_operations_cost = add_operations_cost
	setattr(bom_mod, _ATTR_FLAG, True)
