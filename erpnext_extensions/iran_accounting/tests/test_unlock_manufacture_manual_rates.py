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


def _manufacture_doc(target, sibling, bulk=None):
	items = [target, sibling]
	if bulk is not None:
		items.append(bulk)
	return frappe._dict(
		name="MAT-STE-2026-24883-1",
		purpose="Manufacture",
		docstatus=1,
		items=items,
		job_card="PO-JOB07031",
		work_order=None,
		custom_manufacturing_costing_contract_version="5.3.43",
	)


def _classified(target, sibling, bulk=None):
	return {
		CLASS_MAIN_FG: [],
		CLASS_BULK_SCRAP: [bulk] if bulk is not None else [],
		"MAIN_PRODUCT_REJECT": [],
		CLASS_CO_PRODUCT: [target],
		"CO_PRODUCT_REJECT": [],
		CLASS_COMPONENT_SCRAP: [sibling],
		CLASS_OTHER_OUTPUT: [],
	}


class TestSelectiveUnlock(unittest.TestCase):
	def _target(self, **kwargs):
		fields = {
			"name": "aelcfduknt",
			"item_code": "30500006",
			"valuation_type": "Manual",
			"basic_rate": 1433287,
			"basic_amount": 1433287,
			"secondary_item_type": "By-Product",
		}
		fields.update(kwargs)
		return _row(**fields)

	def _sibling(self, **kwargs):
		return _row(
			name="aellosamt1",
			idx=22,
			item_code="13200187",
			qty=1,
			valuation_type="Valuation Rate",
			basic_rate=198546,
			basic_amount=198546,
			secondary_item_type="Scrap",
			t_warehouse="Reject Pack",
			**kwargs,
		)

	def _run(self, doc, classified, **kwargs):
		writes = []

		def _set_value(doctype, name, values, update_modified=False):
			writes.append({"doctype": doctype, "name": name, "values": dict(values)})

		with (
			mock.patch.object(um, "_list_manufacture_names", return_value=[doc.name]),
			mock.patch.object(um.frappe, "get_doc", return_value=doc),
			mock.patch.object(um, "classify_manufacture_outputs", return_value=classified),
			mock.patch.object(um, "uses_v533_contract", return_value=True),
			mock.patch.object(um.frappe.db, "set_value", side_effect=_set_value),
			mock.patch.object(um.frappe.db, "commit"),
			mock.patch.object(um.frappe, "clear_document_cache"),
		):
			out = um.unlock_manufacture_manual_rates(**kwargs)
		return out, writes

	def test_selective_dry_run_excludes_sibling(self):
		target = self._target()
		sibling = self._sibling()
		doc = _manufacture_doc(target, sibling)
		out, writes = self._run(
			doc,
			_classified(target, sibling),
			stock_entry=doc.name,
			row_name="aelcfduknt",
			item_code="30500006",
			dry_run=True,
		)
		self.assertEqual(out["selected_unlock_count"], 1)
		self.assertEqual(out["excluded_unlock_count"], 1)
		self.assertEqual(out["selected_row"]["item_code"], "30500006")
		self.assertEqual(out["selected_row"]["action"], "unlock")
		self.assertEqual(out["selected_row"]["set_basic_rate_manually_before"], 1)
		self.assertEqual(out["selected_row"]["set_basic_rate_manually_after"], 0)
		self.assertEqual(out["selected_row"]["valuation_type_before"], "Manual")
		self.assertEqual(out["selected_row"]["valuation_type_after"], "")
		self.assertEqual(out["excluded_rows"][0]["item_code"], "13200187")
		self.assertEqual(out["excluded_rows"][0]["row_name"], "aellosamt1")
		self.assertEqual(out["expected_writes"][0]["row_name"], "aelcfduknt")
		self.assertEqual(out["expected_writes"][0]["basic_rate_unchanged"], 1433287)
		self.assertEqual(writes, [])
		self.assertEqual(out["applied_count"], 0)

	def test_selective_apply_writes_only_target(self):
		target = self._target()
		sibling = self._sibling()
		doc = _manufacture_doc(target, sibling)
		out, writes = self._run(
			doc,
			_classified(target, sibling),
			stock_entry=doc.name,
			row_name="aelcfduknt",
			item_code="30500006",
			dry_run=False,
		)
		self.assertEqual(out["applied_count"], 1)
		self.assertEqual(len(writes), 1)
		self.assertEqual(writes[0]["doctype"], "Stock Entry Detail")
		self.assertEqual(writes[0]["name"], "aelcfduknt")
		self.assertEqual(writes[0]["values"]["set_basic_rate_manually"], 0)
		self.assertEqual(writes[0]["values"]["valuation_type"], "")
		self.assertNotIn("basic_rate", writes[0]["values"])
		self.assertNotIn("basic_amount", writes[0]["values"])
		self.assertNotIn("qty", writes[0]["values"])
		self.assertTrue(all(write["doctype"] == "Stock Entry Detail" for write in writes))
		self.assertNotIn("Repost Item Valuation", {write["doctype"] for write in writes})
		self.assertNotIn("Stock Ledger Entry", {write["doctype"] for write in writes})
		self.assertNotIn("GL Entry", {write["doctype"] for write in writes})

	def test_invalid_row_name_unlocks_nothing(self):
		target = self._target()
		sibling = self._sibling()
		doc = _manufacture_doc(target, sibling)
		with self.assertRaises(um.UnlockManufactureError):
			self._run(
				doc,
				_classified(target, sibling),
				stock_entry=doc.name,
				row_name="missing-row",
				item_code="30500006",
				dry_run=False,
			)

	def test_item_mismatch_unlocks_nothing(self):
		target = self._target()
		sibling = self._sibling()
		doc = _manufacture_doc(target, sibling)
		with self.assertRaises(um.UnlockManufactureError):
			self._run(
				doc,
				_classified(target, sibling),
				stock_entry=doc.name,
				row_name="aelcfduknt",
				item_code="13200187",
				dry_run=False,
			)

	def test_row_name_requires_stock_entry(self):
		with mock.patch.object(um, "_list_manufacture_names") as listed:
			with self.assertRaises(um.UnlockManufactureError):
				um.unlock_manufacture_manual_rates(row_name="aelcfduknt", dry_run=True)
		listed.assert_not_called()

	def test_item_only_selection_rejected(self):
		with mock.patch.object(um, "_list_manufacture_names") as listed:
			with self.assertRaises(um.UnlockManufactureError):
				um.unlock_manufacture_manual_rates(
					stock_entry="MAT-STE-2026-24883-1",
					item_code="30500006",
					dry_run=False,
				)
		listed.assert_not_called()

	def test_bulk_scrap_selection_is_not_unlocked(self):
		target = self._target()
		sibling = self._sibling()
		bulk = _row(
			name="bulkrow",
			idx=3,
			item_code="30100101",
			valuation_type="Manual",
			basic_rate=1,
			set_basic_rate_manually=1,
			secondary_item_type="Scrap",
		)
		doc = _manufacture_doc(target, sibling, bulk)
		classified = _classified(target, sibling, bulk)
		out, writes = self._run(
			doc,
			classified,
			stock_entry=doc.name,
			row_name="bulkrow",
			item_code="30100101",
			dry_run=False,
		)
		self.assertEqual(out["selected_row"]["action"], "preserve")
		self.assertEqual(out["applied_count"], 0)
		self.assertEqual(writes, [])
		self.assertEqual(out["excluded_unlock_count"], 2)

	def test_already_unlocked_row_is_idempotent(self):
		target = self._target(set_basic_rate_manually=0, valuation_type="")
		sibling = self._sibling()
		doc = _manufacture_doc(target, sibling)
		out, writes = self._run(
			doc,
			_classified(target, sibling),
			stock_entry=doc.name,
			row_name="aelcfduknt",
			item_code="30500006",
			dry_run=False,
		)
		self.assertEqual(out["selected_row"]["action"], "skip")
		self.assertEqual(out["applied_count"], 0)
		self.assertEqual(writes, [])
		self.assertEqual(out["excluded_rows"][0]["item_code"], "13200187")

	def test_unfiltered_still_unlocks_every_eligible_row(self):
		target = self._target()
		sibling = self._sibling()
		doc = _manufacture_doc(target, sibling)
		out, writes = self._run(
			doc,
			_classified(target, sibling),
			stock_entry=doc.name,
			dry_run=False,
		)
		self.assertIsNone(out["row_filter"]["row_name"])
		self.assertEqual(out["unlock_count"], 2)
		self.assertEqual(out["excluded_unlock_count"], 0)
		self.assertEqual({write["name"] for write in writes}, {"aelcfduknt", "aellosamt1"})
