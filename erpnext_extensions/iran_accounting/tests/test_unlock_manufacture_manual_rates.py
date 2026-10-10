# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests for Manufacture manual-rate unlock planner."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting import unlock_manufacture_manual_rates as um
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_BULK_SCRAP,
	CLASS_CO_PRODUCT,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_OTHER_OUTPUT,
)


def _row(**kwargs):
	defaults = {
		"name": "row1",
		"idx": 1,
		"item_code": "ITEM",
		"set_basic_rate_manually": 1,
		"valuation_type": "",
		"allow_zero_valuation_rate": 0,
		"basic_rate": 100,
		"basic_amount": 100,
		"t_warehouse": "FG",
		"s_warehouse": None,
	}
	defaults.update(kwargs)
	return frappe._dict(defaults)


class TestPlanRowUnlock(unittest.TestCase):
	def test_main_fg_clears_manual_flag_only(self):
		doc = frappe._dict(name="SE", purpose="Manufacture", docstatus=1)
		plan = um._plan_row_unlock(_row(), CLASS_MAIN_FG, doc=doc)
		self.assertEqual(plan["action"], "unlock")
		self.assertFalse(plan.get("clear_valuation_type"))

	def test_component_scrap_manual_vt_cleared(self):
		doc = frappe._dict(name="SE", purpose="Manufacture", docstatus=1)
		plan = um._plan_row_unlock(_row(valuation_type="Manual"), CLASS_COMPONENT_SCRAP, doc=doc)
		self.assertEqual(plan["action"], "unlock")
		self.assertTrue(plan["clear_valuation_type"])

	def test_bulk_scrap_preserved(self):
		doc = frappe._dict(name="SE", purpose="Manufacture", docstatus=1)
		plan = um._plan_row_unlock(_row(valuation_type="Manual"), CLASS_BULK_SCRAP, doc=doc)
		self.assertEqual(plan["action"], "preserve")

	def test_co_product_manual_blocked_without_contract(self):
		doc = frappe._dict(
			name="SE",
			purpose="Manufacture",
			docstatus=1,
			custom_manufacturing_costing_contract_version=None,
		)
		with mock.patch.object(um, "uses_v533_contract", return_value=False):
			plan = um._plan_row_unlock(_row(valuation_type="Manual"), CLASS_CO_PRODUCT, doc=doc)
		self.assertEqual(plan["action"], "blocked")
		self.assertIn("contract stamp", plan["reason"])

	def test_co_product_manual_unlock_with_contract(self):
		doc = frappe._dict(
			name="SE",
			purpose="Manufacture",
			docstatus=1,
			custom_manufacturing_costing_contract_version="5.3.43",
		)
		with mock.patch.object(um, "uses_v533_contract", return_value=True):
			plan = um._plan_row_unlock(_row(valuation_type="Manual"), CLASS_CO_PRODUCT, doc=doc)
		self.assertEqual(plan["action"], "unlock")
		self.assertTrue(plan["clear_valuation_type"])

	def test_other_output_manual_blocked(self):
		doc = frappe._dict(name="SE", purpose="Manufacture", docstatus=1)
		plan = um._plan_row_unlock(_row(valuation_type="Manual"), CLASS_OTHER_OUTPUT, doc=doc)
		self.assertEqual(plan["action"], "blocked")
		self.assertIn("OTHER_OUTPUT", plan["reason"])

	def test_already_dynamic_skipped(self):
		doc = frappe._dict(name="SE", purpose="Manufacture", docstatus=1)
		plan = um._plan_row_unlock(_row(set_basic_rate_manually=0), CLASS_MAIN_FG, doc=doc)
		self.assertEqual(plan["action"], "skip")


class TestCollectDocPlans(unittest.TestCase):
	def test_bulk_scrap_excluded_from_unlock(self):
		fg = _row(name="fg", idx=1, is_finished_item=1, set_basic_rate_manually=1)
		bulk = _row(
			name="bulk",
			idx=2,
			item_code="SCRAP",
			is_finished_item=0,
			secondary_item_type="Scrap",
			valuation_type="Manual",
			basic_rate=0,
			allow_zero_valuation_rate=1,
			set_basic_rate_manually=1,
		)
		consume = _row(
			name="rm",
			idx=3,
			t_warehouse=None,
			s_warehouse="Stores",
			set_basic_rate_manually=1,
		)
		doc = frappe._dict(
			name="SE-1",
			purpose="Manufacture",
			docstatus=1,
			items=[fg, bulk, consume],
			job_card=None,
			work_order=None,
			custom_manufacturing_costing_contract_version="5.3.43",
		)

		classified = {
			CLASS_MAIN_FG: [fg],
			CLASS_BULK_SCRAP: [bulk],
			"MAIN_PRODUCT_REJECT": [],
			"CO_PRODUCT": [],
			"CO_PRODUCT_REJECT": [],
			"COMPONENT_SCRAP": [],
			"OTHER_OUTPUT": [],
		}
		with mock.patch.object(um, "classify_manufacture_outputs", return_value=classified):
			with mock.patch.object(um, "uses_v533_contract", return_value=True):
				plans = um._collect_doc_plans(doc)

		actions = {p["row_name"]: p["action"] for p in plans}
		self.assertEqual(actions["fg"], "unlock")
		self.assertEqual(actions["bulk"], "preserve")
		self.assertNotIn("rm", actions)


class TestContractAdoptionAndIdempotency(unittest.TestCase):
	def test_adopt_requires_manufacture(self):
		with mock.patch.object(um.frappe.db, "get_value", return_value=("Material Transfer", 1)):
			with self.assertRaises(um.UnlockManufactureError):
				um.adopt_manufacture_contract_version("SE-X", dry_run=True)

	def test_already_dynamic_idempotent(self):
		doc = frappe._dict(
			name="SE",
			purpose="Manufacture",
			docstatus=1,
			custom_manufacturing_costing_contract_version="5.3.43",
		)
		with mock.patch.object(um, "uses_v533_contract", return_value=True):
			plan = um._plan_row_unlock(
				_row(set_basic_rate_manually=0, valuation_type=""),
				CLASS_CO_PRODUCT,
				doc=doc,
			)
		self.assertEqual(plan["action"], "skip")

	def test_stamp_bulk_scrap_preserves_manual_rate(self):
		row = _row(
			name="bulkrow",
			item_code="BULK",
			secondary_item_type="Scrap",
			valuation_type="Manual",
			set_basic_rate_manually=1,
			basic_rate=1,
			allow_zero_valuation_rate=0,
		)

		class _Doc:
			name = "SE-BULK"
			purpose = "Manufacture"
			docstatus = 1
			items = [row]

		with mock.patch.object(um, "_list_manufacture_names", return_value=["SE-BULK"]):
			with mock.patch.object(um.frappe, "get_doc", return_value=_Doc()):
				out = um.stamp_bulk_scrap_output_class("SE-BULK", "BULK", dry_run=True)
		self.assertEqual(out["class_after"], CLASS_BULK_SCRAP)
		self.assertEqual(out["set_basic_rate_manually"], 1)
		self.assertEqual(out["basic_rate"], 1)
		self.assertTrue(out["dry_run"])
