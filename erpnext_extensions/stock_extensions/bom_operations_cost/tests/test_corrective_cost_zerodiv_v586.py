# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Regression: ERPNext #68700 — ZeroDivisionError on semi-FG Job Card Record Output.

Run::

    bench --site <site> run-tests --module \\
      erpnext_extensions.stock_extensions.bom_operations_cost.tests.test_corrective_cost_zerodiv_v586
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.stock_extensions.bom_operations_cost.patch import (
	add_corrective_operation_cost,
	add_operations_cost,
	apply_patch,
	makes_semi_finished_good,
)


class TestMakesSemiFinishedGoodV586(unittest.TestCase):
	def test_semi_fg_when_finished_good_differs(self):
		jc = frappe._dict(finished_good="SEMI-ITEM")
		self.assertTrue(makes_semi_finished_good(frappe._dict(production_item="FG-ITEM"), jc))

	def test_fg_job_card_not_semi(self):
		wo = frappe._dict(production_item="FG-ITEM")
		jc = frappe._dict(finished_good="FG-ITEM")
		self.assertFalse(makes_semi_finished_good(wo, jc))

	def test_missing_job_card_not_semi(self):
		wo = frappe._dict(production_item="FG-ITEM")
		self.assertFalse(makes_semi_finished_good(wo, None))


class TestCorrectiveCostSafeDenominatorV586(unittest.TestCase):
	def test_semi_fg_skips_corrective_append(self):
		"""PO-JOB10628 shape: semi-FG JC must not divide by max_qty − produced_qty."""
		stock_entry = frappe._dict(fg_completed_qty=10, additional_costs=[])
		stock_entry.append = lambda child, row: stock_entry.additional_costs.append(frappe._dict(row))
		wo = frappe._dict(
			name="WO-X",
			production_item="20100067",
			corrective_operation_cost=116083386.76,
			produced_qty=584,
			track_semi_finished_goods=1,
		)
		jc = frappe._dict(finished_good="30300020")

		with mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._corrective_cost_enabled",
			return_value=True,
		):
			add_corrective_operation_cost(stock_entry, wo, "Expense - X", jc)

		self.assertEqual(stock_entry.additional_costs, [])

	def test_zero_remaining_qty_skips_without_zerodivision(self):
		stock_entry = frappe._dict(fg_completed_qty=1, additional_costs=[])
		stock_entry.append = lambda child, row: stock_entry.additional_costs.append(frappe._dict(row))
		wo = frappe._dict(
			name="WO-Y",
			production_item="FG",
			corrective_operation_cost=100,
			produced_qty=50,
			track_semi_finished_goods=1,
		)

		with mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._corrective_cost_enabled",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._max_operation_quantity",
			return_value=50,  # == produced_qty → denominator 0
		), mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._utilised_corrective_cost",
			return_value=0,
		):
			add_corrective_operation_cost(stock_entry, wo, "Expense - X", job_card=None)

		self.assertEqual(stock_entry.additional_costs, [])

	def test_fg_with_positive_denominator_appends_amount(self):
		stock_entry = frappe._dict(fg_completed_qty=2, additional_costs=[])
		stock_entry.append = lambda child, row: stock_entry.additional_costs.append(frappe._dict(row))
		wo = frappe._dict(
			name="WO-Z",
			production_item="FG",
			corrective_operation_cost=100,
			produced_qty=0,
			track_semi_finished_goods=1,
		)
		jc = frappe._dict(finished_good="FG")

		with mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._corrective_cost_enabled",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._max_operation_quantity",
			return_value=10,
		), mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch._utilised_corrective_cost",
			return_value=0,
		):
			add_corrective_operation_cost(stock_entry, wo, "Expense - X", jc)

		self.assertEqual(len(stock_entry.additional_costs), 1)
		self.assertEqual(stock_entry.additional_costs[0].description, "Corrective Operation Cost")
		self.assertEqual(flt(stock_entry.additional_costs[0].amount), 20.0)  # 100/10*2

	def test_wrapper_disables_core_corrective_then_applies_safe(self):
		"""Core path must never see corrective_operation_cost (avoids ZeroDivision)."""
		seen = {}

		def fake_original(stock_entry, work_order=None, expense_account=None, job_card=None):
			seen["corr"] = work_order.corrective_operation_cost if work_order else None

		stock_entry = frappe._dict(fg_completed_qty=1, additional_costs=[])
		stock_entry.append = lambda child, row: stock_entry.additional_costs.append(frappe._dict(row))
		wo = frappe._dict(
			name="WO-W",
			production_item="FG",
			corrective_operation_cost=50,
			produced_qty=0,
			track_semi_finished_goods=0,
		)

		import erpnext.manufacturing.doctype.bom.bom as bom_mod

		with mock.patch.object(bom_mod, "_ee_bom_ops_cost_orig", fake_original, create=True), mock.patch(
			"erpnext_extensions.stock_extensions.bom_operations_cost.patch.add_corrective_operation_cost"
		) as add_corr:
			add_operations_cost(stock_entry, wo, "Expense - X", job_card=None)

		self.assertEqual(seen.get("corr"), 0)
		self.assertEqual(wo.corrective_operation_cost, 50)  # restored
		add_corr.assert_called_once()

	def test_apply_patch_idempotent(self):
		import erpnext.manufacturing.doctype.bom.bom as bom_mod

		apply_patch()
		first = bom_mod.add_operations_cost
		apply_patch()
		self.assertIs(bom_mod.add_operations_cost, first)
		self.assertTrue(getattr(bom_mod, "_ee_bom_ops_cost_patched", False))


class TestPOJOB10628ScenarioV586(unittest.TestCase):
	"""Live-shaped scenario for the affected Job Card (skipped if missing)."""

	SITE_JC = "PO-JOB10628"

	def test_record_output_corrective_cost_does_not_zerodivide(self):
		if not frappe.db.exists("Job Card", self.SITE_JC):
			self.skipTest(f"{self.SITE_JC} not on site")

		apply_patch()
		jc = frappe.get_doc("Job Card", self.SITE_JC)
		wo = frappe.get_doc("Work Order", jc.work_order)
		self.assertTrue(makes_semi_finished_good(wo, jc))

		stock_entry = frappe.new_doc("Stock Entry")
		stock_entry.purpose = "Manufacture"
		stock_entry.company = wo.company
		stock_entry.work_order = wo.name
		stock_entry.fg_completed_qty = 1
		stock_entry.additional_costs = []

		# Must not raise ZeroDivisionError.
		from erpnext.manufacturing.doctype.bom.bom import add_additional_cost

		add_additional_cost(stock_entry, wo, jc)
		corrective = [
			r for r in (stock_entry.additional_costs or []) if r.get("description") == "Corrective Operation Cost"
		]
		self.assertEqual(corrective, [])


if __name__ == "__main__":
	unittest.main()
