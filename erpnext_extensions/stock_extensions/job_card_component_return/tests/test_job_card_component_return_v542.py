# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.4.2 Job Card component return — N01–N35 + PO-JOB10492 canaries.

Run:
  bench --site <site> execute \\
    erpnext_extensions.stock_extensions.job_card_component_return.tests.\\
    test_job_card_component_return_v542.run_all
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
from erpnext_extensions.stock_extensions.job_card_component_return.eligibility import (
	decide_work_order_status_bypass,
	looks_like_job_card_component_return,
)
from erpnext_extensions.stock_extensions.job_card_component_return.patch import (
	apply_patch,
	bypass_enabled,
)
from erpnext_extensions.stock_extensions.job_card_component_return.returnable import (
	aggregate_returnable_report,
	get_returnable_by_item_batch,
)

CANARY_JC = "PO-JOB10492"
CANARY_WO = "MFG-WO-2026-00837"
CANARY_ITEM = "18000001"
CANARY_BATCH = "6025-18000001-J100260050"
CANARY_JCI = "8cgmns10nv"


def _row(**kwargs):
	base = {
		"idx": 1,
		"item_code": CANARY_ITEM,
		"qty": 1,
		"transfer_qty": 1,
		"batch_no": CANARY_BATCH,
		"s_warehouse": "WIP-WH",
		"t_warehouse": "SRC-WH",
		"job_card_item": CANARY_JCI,
		"is_finished_item": 0,
		"is_scrap_item": 0,
		"secondary_item_type": None,
		"custom_output_class": None,
	}
	base.update(kwargs)
	return frappe._dict(base)


def _se(**kwargs):
	base = {
		"is_return": 1,
		"purpose": "Material Transfer for Manufacture",
		"job_card": CANARY_JC,
		"work_order": CANARY_WO,
		"items": [_row()],
		"pro_doc": frappe._dict(status="In Process", name=CANARY_WO),
	}
	base.update(kwargs)
	return frappe._dict(base)


def _ret_map(returnable=100.0, **kwargs):
	rec = {
		"item_code": CANARY_ITEM,
		"batch_no": CANARY_BATCH,
		"issued": 100.0,
		"returned": 0.0,
		"consumed": 0.0,
		"component_scrap": 0.0,
		"ordinary_scrap": 0.0,
		"product_reject": 0.0,
		"other": 0.0,
		"still_in_wip": returnable,
		"returnable": returnable,
		"wip_warehouses": {"WIP-WH"},
		"return_destinations": {"SRC-WH"},
		"job_card_items": {CANARY_JCI},
	}
	rec.update(kwargs)
	return {(CANARY_ITEM, CANARY_BATCH): rec}


class TestJobCardComponentReturnV542(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_iran()
		apply_patch()
		frappe.set_user("Administrator")

	def test_N04_manual_wo_only_not_jc_return(self):
		se = _se(job_card=None)
		self.assertFalse(looks_like_job_card_component_return(se))
		d = decide_work_order_status_bypass(se)
		self.assertFalse(d.allow)
		self.assertFalse(d.is_attempted_jc_return)
		self.assertEqual(d.code, "NOT_JC_RETURN")

	def test_N10_zero_return(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			se = _se(items=[_row(qty=0, transfer_qty=0)])
			d = decide_work_order_status_bypass(se)
			self.assertFalse(d.allow)
			self.assertEqual(d.code, "NON_POSITIVE_QTY")

	def test_N11_negative_return(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			se = _se(items=[_row(qty=-1, transfer_qty=-1)])
			d = decide_work_order_status_bypass(se)
			self.assertFalse(d.allow)
			self.assertEqual(d.code, "NON_POSITIVE_QTY")

	def test_N07_over_return(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(returnable=10),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			se = _se(items=[_row(qty=10.001, transfer_qty=10.001)])
			d = decide_work_order_status_bypass(se)
			self.assertFalse(d.allow)
			self.assertEqual(d.code, "OVER_RETURN")

	def test_N08_N09_N24_exact_and_partial(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(returnable=10),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			d1 = decide_work_order_status_bypass(_se(items=[_row(qty=10, transfer_qty=10)]))
			self.assertTrue(d1.allow, d1)
			d2 = decide_work_order_status_bypass(_se(items=[_row(qty=3, transfer_qty=3)]))
			self.assertTrue(d2.allow, d2)

	def test_N06_unrelated_item(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			se = _se(items=[_row(item_code="ZZZ99999")])
			d = decide_work_order_status_bypass(se)
			self.assertFalse(d.allow)
			self.assertEqual(d.code, "UNRELATED_ITEM")

	def test_N12_N13_N28_batch_safety(self):
		rmap = _ret_map(returnable=10)
		rmap[(CANARY_ITEM, "BATCH-B")] = {
			**rmap[(CANARY_ITEM, CANARY_BATCH)],
			"batch_no": "BATCH-B",
			"returnable": 20,
			"still_in_wip": 20,
			"job_card_items": {CANARY_JCI},
		}
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=rmap,
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			# wrong batch with no WIP
			d1 = decide_work_order_status_bypass(_se(items=[_row(batch_no="BATCH-X", qty=5, transfer_qty=5)]))
			self.assertEqual(d1.code, "WRONG_BATCH")
			# batch A insufficient even though aggregate would allow
			d2 = decide_work_order_status_bypass(
				_se(items=[_row(batch_no=CANARY_BATCH, qty=25, transfer_qty=25)])
			)
			self.assertEqual(d2.code, "OVER_RETURN")

	def test_N14_N15_warehouse(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			d1 = decide_work_order_status_bypass(_se(items=[_row(s_warehouse="WRONG-WIP")]))
			self.assertEqual(d1.code, "WRONG_WIP_WAREHOUSE")
			d2 = decide_work_order_status_bypass(_se(items=[_row(t_warehouse="WRONG-DEST")]))
			self.assertEqual(d2.code, "INVALID_DESTINATION")

	def test_N16_mixed_rows(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			se = _se(items=[_row(), _row(idx=2, item_code="ZZZ99999")])
			d = decide_work_order_status_bypass(se)
			self.assertFalse(d.allow)
			self.assertEqual(d.code, "UNRELATED_ITEM")

	def test_N21_N22_N23_masquerade(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			self.assertEqual(
				decide_work_order_status_bypass(
					_se(items=[_row(custom_output_class="COMPONENT_SCRAP")])
				).code,
				"OUTPUT_CLASS_MASQUERADE",
			)
			self.assertEqual(
				decide_work_order_status_bypass(
					_se(items=[_row(custom_output_class="MAIN_PRODUCT_REJECT")])
				).code,
				"OUTPUT_CLASS_MASQUERADE",
			)
			self.assertEqual(
				decide_work_order_status_bypass(_se(items=[_row(secondary_item_type="Co-Product")])).code,
				"SECONDARY_MASQUERADE",
			)

	def test_N27_wo_cancelled(self):
		with patch(
			"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
		) as gv:
			gv.side_effect = self._gv_side_effect(wo_status="Cancelled", wo_docstatus=2)
			d = decide_work_order_status_bypass(_se(pro_doc=frappe._dict(status="Cancelled")))
			self.assertEqual(d.code, "WO_CANCELLED")

	def test_N01_draft_jc(self):
		with patch(
			"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
		) as gv:
			gv.side_effect = self._gv_side_effect(jc_docstatus=0)
			d = decide_work_order_status_bypass(_se())
			self.assertEqual(d.code, "JC_NOT_SUBMITTED")

	def test_N02_cancelled_jc(self):
		with patch(
			"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
		) as gv:
			gv.side_effect = self._gv_side_effect(jc_status="Cancelled")
			d = decide_work_order_status_bypass(_se())
			self.assertEqual(d.code, "JC_CANCELLED")

	def test_N03_other_wo(self):
		with patch(
			"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
		) as gv:
			gv.side_effect = self._gv_side_effect(jc_wo="MFG-WO-OTHER")
			d = decide_work_order_status_bypass(_se())
			self.assertEqual(d.code, "WO_MISMATCH")

	def test_N35_patch_fingerprint_enabled(self):
		self.assertTrue(bypass_enabled(), "bypass must be enabled on ERPNext 16.37 fingerprint")

	def test_N30_idempotent_decide(self):
		with (
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.frappe.db.get_value"
			) as gv,
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.get_returnable_by_item_batch",
				return_value=_ret_map(returnable=5),
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_codes",
				return_value={CANARY_ITEM},
			),
			patch(
				"erpnext_extensions.stock_extensions.job_card_component_return.eligibility.jc_item_names",
				return_value={CANARY_JCI},
			),
		):
			gv.side_effect = self._gv_side_effect()
			se = _se(items=[_row(qty=2, transfer_qty=2)])
			a = decide_work_order_status_bypass(se)
			b = decide_work_order_status_bypass(se)
			self.assertEqual(a.allow, b.allow)
			self.assertEqual(a.code, b.code)

	@staticmethod
	def _gv_side_effect(
		*,
		jc_docstatus=1,
		jc_status="Work In Progress",
		jc_wo=CANARY_WO,
		wo_status="In Process",
		wo_docstatus=1,
	):
		def _inner(doctype, name, fieldname=None, as_dict=False, **kwargs):
			if doctype == "Job Card":
				data = frappe._dict(
					name=name,
					docstatus=jc_docstatus,
					status=jc_status,
					work_order=jc_wo,
					company="X",
				)
				if as_dict or isinstance(fieldname, (list, tuple)):
					return data
				return data.get(fieldname)
			if doctype == "Work Order":
				if fieldname == "status":
					return wo_status
				if fieldname == "docstatus":
					return wo_docstatus
				return wo_status
			return None

		return _inner


class TestCanaryPOJOB10492(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_iran()
		apply_patch()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Job Card", CANARY_JC):
			raise unittest.SkipTest(f"{CANARY_JC} missing")

	def test_N31_N32_secondary_unchanged(self):
		row = frappe.db.get_value(
			"Job Card Secondary Item",
			{"parent": CANARY_JC, "item_code": "13100134"},
			["name", "secondary_item_type", "stock_qty"],
			as_dict=True,
		)
		self.assertTrue(row)
		self.assertEqual(row.secondary_item_type, "Scrap")
		self.assertEqual(flt(row.stock_qty), 125)
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import service

		prev = service.preview_rebuild(job_card=CANARY_JC)
		mr = prev.get("manufacture_readiness") or {}
		classified = (mr.get("classified") or {}) if isinstance(mr, dict) else {}
		self.assertIn("COMPONENT_SCRAP", classified)
		cs_items = [r.get("item_code") for r in classified.get("COMPONENT_SCRAP") or []]
		self.assertIn("13100134", cs_items)
		stage = mr.get("stage") or []
		stage_items = {r.get("item_code") for r in stage}
		self.assertNotIn("13100134", stage_items)
		self.assertIn("30100026", stage_items)

	def test_live_returnable_report(self):
		rows = aggregate_returnable_report(CANARY_JC)
		self.assertTrue(any(r["item_code"] == CANARY_ITEM and r["returnable"] > 0 for r in rows))

	def test_N24_canary_validate_bypass_savepoint(self):
		rmap = get_returnable_by_item_batch(CANARY_JC)
		rec = rmap.get((CANARY_ITEM, CANARY_BATCH))
		self.assertTrue(rec and rec["returnable"] > 0, rec)
		src_wh = next(iter(rec["wip_warehouses"]))
		dest_wh = next(iter(rec["return_destinations"]))
		frappe.db.savepoint("v542_canary")
		try:
			se = frappe.new_doc("Stock Entry")
			se.company = frappe.db.get_value("Job Card", CANARY_JC, "company")
			se.purpose = "Material Transfer for Manufacture"
			se.stock_entry_type = "Material Transfer for Manufacture"
			se.is_return = 1
			se.work_order = CANARY_WO
			se.job_card = CANARY_JC
			se.from_bom = 0
			se.fg_completed_qty = 0
			se.append(
				"items",
				{
					"item_code": CANARY_ITEM,
					"qty": 1,
					"uom": "Gram",
					"stock_uom": "Gram",
					"conversion_factor": 1,
					"s_warehouse": src_wh,
					"t_warehouse": dest_wh,
					"job_card_item": CANARY_JCI,
					"use_serial_batch_fields": 1,
					"batch_no": CANARY_BATCH,
				},
			)
			se.set_stock_entry_type()
			se.pro_doc = frappe.get_doc("Work Order", CANARY_WO)
			self.assertEqual(se.pro_doc.status, "In Process")
			# Must not throw Work Order Not Finished
			se.validate_work_order_status_for_return()
			# Over-return must block
			se.items[0].qty = flt(rec["returnable"]) + 1
			se.items[0].transfer_qty = se.items[0].qty
			with self.assertRaises(frappe.ValidationError) as ctx:
				se.validate_work_order_status_for_return()
			self.assertIn("exceeds current returnable", str(ctx.exception).lower() + str(ctx.exception))
		finally:
			frappe.db.rollback(save_point="v542_canary")

	def test_N05_forged_job_card_insufficient(self):
		"""job_card set alone is not enough without returnable evidence match."""
		frappe.db.savepoint("v542_forge")
		try:
			se = frappe.new_doc("Stock Entry")
			se.company = frappe.db.get_value("Job Card", CANARY_JC, "company")
			se.purpose = "Material Transfer for Manufacture"
			se.is_return = 1
			se.work_order = CANARY_WO
			se.job_card = CANARY_JC
			se.append(
				"items",
				{
					"item_code": "ZZZ-NOT-ON-JC",
					"qty": 1,
					"uom": "Nos",
					"stock_uom": "Nos",
					"conversion_factor": 1,
					"s_warehouse": "X",
					"t_warehouse": "Y",
				},
			)
			se.pro_doc = frappe.get_doc("Work Order", CANARY_WO)
			with self.assertRaises(frappe.ValidationError):
				se.validate_work_order_status_for_return()
		finally:
			frappe.db.rollback(save_point="v542_forge")

	def test_N25_N26_completed_closed_still_early_return(self):
		"""Core Completed/Closed path remains no-op (no throw)."""
		se = frappe.new_doc("Stock Entry")
		se.is_return = 1
		se.work_order = CANARY_WO
		se.pro_doc = frappe._dict(status="Completed")
		# Must not throw
		se.validate_work_order_status_for_return()
		se.pro_doc = frappe._dict(status="Closed")
		se.validate_work_order_status_for_return()


def run_all():
	suite = unittest.TestLoader().loadTestsFromModule(
		frappe.get_module(
			"erpnext_extensions.stock_extensions.job_card_component_return.tests."
			"test_job_card_component_return_v542"
		)
	)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"bypass_enabled": bypass_enabled(),
	}
