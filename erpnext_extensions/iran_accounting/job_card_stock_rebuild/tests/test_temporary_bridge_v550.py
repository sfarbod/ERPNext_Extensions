# Copyright (c) 2026, ERPNext Extensions contributors
"""Temporary Material Receipt bridge — TR01–TR11 + delete lifecycle (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.sync_valuation import (
	suppress_auto_riv,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.temporary_stock_bridge import (
	TEMP_REMARK_MARKER,
	cancel_temp_receipt,
	compute_cancel_shortages,
	create_and_submit_temp_receipt,
	delete_temp_receipt,
	plan_temporary_bridge,
	verify_temp_absent,
)


JC = "PO-JOB08760"
FOREIGN_7 = [
	"MAT-STE-2026-32617",
	"MAT-STE-2026-40364",
	"MAT-STE-2026-33377",
	"MAT-STE-2026-33928",
	"MAT-STE-2026-40369",
	"MAT-STE-2026-37643",
	"MAT-STE-2026-37777",
]
SCOPE = (
	"MAT-STE-2026-31724-1",
	"MAT-STE-2026-31725",
	"MAT-STE-2026-31726",
)


class TestTemporaryBridgeV550(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Job Card", JC):
			self.skipTest("missing PO-JOB08760")
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def _plan_input(self):
		return {
			"dispositions": [
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 1148,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			"merge_documents": ["MAT-STE-2026-31724-1"],
			"stamp_mode": "HISTORICAL",
		}

	def _baseline(self):
		base = {n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in SCOPE}
		base.update({n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in FOREIGN_7})
		base["temps"] = frappe.db.sql(
			"select count(*) from `tabStock Entry` where remarks like %s",
			(f"%{TEMP_REMARK_MARKER}%",),
		)[0][0]
		return base

	def test_shortage_exact_three_rows(self):
		short = compute_cancel_shortages(["MAT-STE-2026-31726", "MAT-STE-2026-31725"])
		self.assertEqual(len(short), 3)
		by = {(r["item_code"], r["batch_no"], r["warehouse"]): flt(r["shortage_qty"]) for r in short}
		approved = "انبار approved محصول نهایی اسپاد"
		milan = "انبار آماده فروش -میلان پارس"
		self.assertAlmostEqual(by[("20100041", "505124-20100041-MB262003A11", approved)], 61)
		self.assertAlmostEqual(by[("20100193", "505141-20100193-BF262411A11", approved)], 1)
		self.assertAlmostEqual(by[("20100041", "505124-20100041-MB262003A11", milan)], 20)
		for r in short:
			self.assertGreater(flt(r["valuation_rate"]), 0)

	def test_plan_bridge_unlock(self):
		plan = build_manufacture_plan(JC, **self._plan_input())
		self.assertTrue(plan.get("apply_allowed"))
		self.assertFalse(any("SHARED_BLOCKED" in b for b in plan["blockers"]))
		bridge = plan.get("temporary_bridge") or {}
		self.assertTrue(bridge.get("required"))
		self.assertTrue(bridge.get("unlock"))
		self.assertEqual(len(bridge.get("shortages") or []), 3)
		self.assertIn("MAT-STE-2026-31725", plan.get("minimal_cancel_set") or [])
		self.assertIn("MAT-STE-2026-31726", plan.get("minimal_cancel_set") or [])

	def test_tr20_create_submit_cancel_delete_lifecycle(self):
		"""Explicit temp receipt lifecycle leaves zero artifacts."""
		plan = plan_temporary_bridge(["MAT-STE-2026-31726", "MAT-STE-2026-31725"])
		frappe.db.begin()
		sp = "tr20_temp_lifecycle"
		frappe.db.savepoint(sp)
		try:
			with suppress_auto_riv():
				name = create_and_submit_temp_receipt(plan, "tr20")
				self.assertTrue(name)
				self.assertEqual(cint(frappe.db.get_value("Stock Entry", name, "docstatus")), 1)
				cancel_temp_receipt(name)
				self.assertEqual(cint(frappe.db.get_value("Stock Entry", name, "docstatus")), 2)
				delete_temp_receipt(name)
				absent = verify_temp_absent(name)
				self.assertTrue(absent["ok"], msg=absent.get("errors"))
		finally:
			frappe.db.rollback(save_point=sp)
			frappe.db.rollback()
		self.assertFalse(frappe.db.exists("Stock Entry", name))

	def test_tr01_tr11_failpoints_restore_baseline(self):
		points = (
			"after_temp_receipt_submit",  # TR01
			"after_31726_cancel",  # TR02
			"after_31725_cancel",  # TR03
			"after_mfg_cancel",  # TR04
			"after_canonical_submit",  # TR05
			"after_31725_recreate",  # TR06
			"after_31726_recreate",  # TR07
			"after_temp_receipt_cancel",  # TR08
			"after_temp_receipt_delete",  # TR09
			"after_valuation",  # TR10
			"during_sle_verify",  # TR11
		)
		for point in points:
			before = self._baseline()
			frappe.flags.jc_repair_fail_at = point
			res = run_repair(JC, plan_input=self._plan_input(), dry_run=True)
			after = self._baseline()
			self.assertEqual(before, after, msg=f"{point}: baseline drift")
			self.assertFalse(res.get("mutated"), msg=point)
			self.assertEqual(res.get("status"), "DRY_RUN_FAIL", msg=f"{point}: {res.get('status')}")
			self.assertIn("INJECTED_FAILURE", res.get("error") or "", msg=point)
			# No leftover temp bridge docs
			self.assertEqual(
				frappe.db.sql(
					"select count(*) from `tabStock Entry` where remarks like %s",
					(f"%{TEMP_REMARK_MARKER}%",),
				)[0][0],
				0,
				msg=point,
			)
			frappe.flags.jc_repair_fail_at = None

	def test_dry_run_pass_zero_mutation(self):
		before = self._baseline()
		res = run_repair(JC, plan_input=self._plan_input(), dry_run=True)
		after = self._baseline()
		self.assertEqual(before, after)
		self.assertTrue(res.get("ok"), msg=res.get("error"))
		self.assertEqual(res.get("status"), "DRY_RUN_PASS")
		self.assertFalse(res.get("mutated"))
		self.assertFalse(res.get("committed"))
		self.assertTrue((res.get("temporary_bridge") or {}).get("required"))
		self.assertIsNone(res.get("temporary_receipt"))
		self.assertTrue((res.get("temporary_receipt_absent") or {}).get("ok"))
		for n in FOREIGN_7:
			self.assertEqual(cint(frappe.db.get_value("Stock Entry", n, "docstatus")), 1)
