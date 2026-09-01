# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.8 — PM Clearance pending remark save with Desk derived-field drift."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.petty_management.tests import test_pm_pending_remark_edit_v506 as base


class TestPMPendingRemarkClearanceDriftV508(base.TestPMPendingRemarkEditV506):
	def test_clearance_remark_save_with_derived_parent_drift(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)
		doc.remark = "remark with parent drift"
		doc.pending_amount = flt(doc.pending_amount) + 1
		doc.total_expense_amount = flt(doc.total_expense_amount) + 1
		doc.remaining_amount = flt(doc.remaining_amount) + 1
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "remark with parent drift")

	def test_clearance_remark_save_with_derived_child_drift(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)
		doc.remark = "remark with child drift"
		doc.details[0].amount_plus_tax = 0
		doc.details[0].outstanding_amount = flt(doc.details[0].outstanding_amount) + 1
		if doc.request_allocations:
			doc.request_allocations[0].available_amount = flt(doc.request_allocations[0].available_amount) + 1
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "remark with child drift")

	def test_clearance_blocks_allocated_amount_with_remark(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)
		doc.remark = "blocked"
		doc.details[0].allocated_amount = flt(doc.details[0].allocated_amount) + 1
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save()
		self.assertIn("Only Remarks may be edited", str(ctx.exception))

	def test_request_remark_save_with_derived_parent_drift(self):
		doc = self._pending_request("Pending Manager Approval")
		doc.remark = "request parent drift"
		doc.total_requested_amount = flt(doc.total_requested_amount) + 1
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "request parent drift")
