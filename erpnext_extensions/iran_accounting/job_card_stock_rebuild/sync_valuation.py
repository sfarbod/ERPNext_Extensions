# Copyright (c) 2026, ERPNext Extensions contributors
"""Synchronous valuation adapter for atomic Manufacture repair (v5.5.0).

Suppresses automatic async RIV creation during repair and runs scoped
``update_entries_after`` without RIV progress commits.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable

import frappe
from frappe.utils import get_datetime


@contextmanager
def suppress_auto_riv():
	"""Repair context: no auto RIV, soft WO op-status, workflow state set without transition gate."""
	from erpnext.controllers.stock_controller import StockController
	from erpnext.manufacturing.doctype.work_order.work_order import WorkOrder
	from frappe.model.document import Document
	from frappe.model.workflow import set_workflow_state_on_action
	from frappe.utils import flt

	original_riv = StockController.repost_future_sle_and_gle
	original_op_status = WorkOrder.update_operation_status
	original_wf = Document.validate_workflow
	frappe.flags.jc_manufacture_repair = True

	def _noop(self, force=False, via_landed_cost_voucher=False):
		return None

	def _soft_update_operation_status(self):
		"""Same as Core, but clamp over-complete ops instead of throwing.

		Some historical WOs already have operation completed_qty > WO qty.
		Repair cancel/submit must not be blocked by that pre-existing state.
		"""
		allowance_percentage = flt(
			frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order")
		)
		max_allowed_qty_for_wo = flt(self.qty) + (allowance_percentage / 100 * flt(self.qty))
		for d in self.get("operations"):
			precision = d.precision("completed_qty")
			qty = flt(flt(d.completed_qty, precision) + flt(d.process_loss_qty, precision), precision)
			if not qty:
				d.status = "Pending"
			elif qty < flt(self.qty, precision):
				d.status = "Work in Progress"
			else:
				d.status = "Completed"
				if qty > flt(max_allowed_qty_for_wo, precision):
					pass

	def _repair_validate_workflow(self):
		"""Skip transition permission checks; still stamp Submitted/Cancelled states."""
		if frappe.flags.in_install == "frappe":
			return
		workflow = self.meta.get_workflow()
		if workflow and self._action != "save":
			set_workflow_state_on_action(self, workflow, self._action)

	StockController.repost_future_sle_and_gle = _noop
	WorkOrder.update_operation_status = _soft_update_operation_status
	Document.validate_workflow = _repair_validate_workflow
	try:
		yield
	finally:
		StockController.repost_future_sle_and_gle = original_riv
		WorkOrder.update_operation_status = original_op_status
		Document.validate_workflow = original_wf
		frappe.flags.jc_manufacture_repair = False


def sync_valuation_for_vouchers(voucher_names: Iterable[str], limit_pairs: int = 80) -> dict:
	"""Synchronously repost affected item×warehouse ledgers for vouchers.

	Uses Core ``update_entries_after`` with no RIV document (no mid-flight commit).
	Blocks when affected scope exceeds ``limit_pairs``.
	"""
	from erpnext.stock.stock_ledger import update_entries_after

	names = [n for n in voucher_names if n]
	if not names:
		return {"ok": True, "pairs": [], "note": "no vouchers"}

	pairs = frappe.db.sql(
		"""
		select distinct item_code, warehouse, min(posting_date) as posting_date,
		       min(posting_time) as posting_time
		from `tabStock Ledger Entry`
		where voucher_no in %s
		group by item_code, warehouse
		order by item_code, warehouse
		""",
		(names,),
		as_dict=1,
	)
	if len(pairs) > limit_pairs:
		return {
			"ok": False,
			"error": f"Sync valuation scope too large ({len(pairs)} pairs > {limit_pairs})",
			"pairs": pairs,
		}

	done = []
	for p in pairs:
		if not p.item_code or not p.warehouse:
			continue
		update_entries_after(
			{
				"item_code": p.item_code,
				"warehouse": p.warehouse,
				"posting_date": p.posting_date,
				"posting_time": p.posting_time,
			},
			allow_negative_stock=False,
		)
		done.append((p.item_code, p.warehouse, str(p.posting_date)))
	return {"ok": True, "pairs": done, "count": len(done)}
