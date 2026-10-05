# Copyright (c) 2026, ERPNext Extensions contributors
"""Temporary Material Receipt bridge — post-Apply regression (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
	discover_downstream,
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


class TestTemporaryBridgeV550(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Job Card", JC):
			self.skipTest("missing PO-JOB08760")
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def _active(self):
		scan = scan_golden_rule(JC)
		mfg = scan["manufactures"][0].name
		logistics = discover_downstream(JC, [mfg]).get("minimal_cancel_set") or []
		return mfg, logistics

	def _baseline(self):
		mfg, logistics = self._active()
		names = [mfg, "MAT-STE-2026-31724-1", "MAT-STE-2026-31725", "MAT-STE-2026-31726"] + logistics
		base = {n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in names}
		base.update({n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in FOREIGN_7})
		base["temps"] = frappe.db.sql(
			"select count(*) from `tabStock Entry` where remarks like %s",
			(f"%{TEMP_REMARK_MARKER}%",),
		)[0][0]
		return base

	def test_shortage_on_active_replacements(self):
		_, logistics = self._active()
		self.assertGreaterEqual(len(logistics), 2)
		short = compute_cancel_shortages(logistics)
		# Exact qty may vary with live stock; require positive shortages + rates
		self.assertGreaterEqual(len(short), 1)
		for r in short:
			self.assertGreater(flt(r["shortage_qty"]), 0)
			self.assertGreater(flt(r["valuation_rate"]), 0)

	def test_plan_nothing_to_repair_after_apply(self):
		mfg, logistics = self._active()
		plan = build_manufacture_plan(
			JC,
			dispositions=[
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			merge_documents=[mfg],
			stamp_mode="HISTORICAL",
		)
		self.assertFalse(plan.get("apply_allowed"))
		self.assertTrue(any("Nothing to repair" in b for b in plan["blockers"]))
		# Bridge still calculable for active shared logistics
		bridge = plan_temporary_bridge(logistics)
		self.assertTrue(bridge.get("required"))

	def test_tr20_create_submit_cancel_delete_lifecycle(self):
		"""Explicit temp receipt lifecycle leaves zero artifacts."""
		_, logistics = self._active()
		plan = plan_temporary_bridge(logistics)
		if not plan.get("shortages"):
			self.skipTest("no shortages for lifecycle")
		frappe.db.begin()
		sp = "tr20_temp_lifecycle"
		frappe.db.savepoint(sp)
		name = ""
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
		if name:
			self.assertFalse(frappe.db.exists("Stock Entry", name))

	def test_tr01_tr11_failpoints_or_blocked_restore_baseline(self):
		"""Post-Apply: repair plan is Nothing-to-repair → BLOCKED; baseline unchanged."""
		mfg, _ = self._active()
		plan_input = {
			"dispositions": [
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			"merge_documents": [mfg],
			"stamp_mode": "HISTORICAL",
		}
		points = (
			"after_temp_receipt_submit",
			"after_31726_cancel",
			"after_mfg_cancel",
			"after_canonical_submit",
			"after_valuation",
			"during_sle_verify",
		)
		for point in points:
			before = self._baseline()
			frappe.flags.jc_repair_fail_at = point
			res = run_repair(JC, plan_input=plan_input, dry_run=True)
			after = self._baseline()
			self.assertEqual(before, after, msg=f"{point}: baseline drift")
			self.assertFalse(res.get("mutated"), msg=point)
			self.assertIn(
				res.get("status"),
				("BLOCKED", "DRY_RUN_FAIL", "STALE PLAN", "DRY_RUN_PASS"),
				msg=point,
			)
			self.assertEqual(
				frappe.db.sql(
					"select count(*) from `tabStock Entry` where remarks like %s",
					(f"%{TEMP_REMARK_MARKER}%",),
				)[0][0],
				0,
				msg=point,
			)
			frappe.flags.jc_repair_fail_at = None

	def test_post_apply_zero_mutation_and_foreign(self):
		before = self._baseline()
		mfg, _ = self._active()
		res = run_repair(
			JC,
			plan_input={
				"dispositions": [
					{
						"item_code": "13200544",
						"batch_no": "5648-13200544-PR-10741",
						"proposed_consumed": 0,
						"proposed_scrap": 0,
						"proposed_return": 0,
						"proposed_still_in_wip": 0,
					}
				],
				"merge_documents": [mfg],
				"stamp_mode": "HISTORICAL",
			},
			dry_run=True,
		)
		after = self._baseline()
		self.assertEqual(before, after)
		self.assertFalse(res.get("mutated"))
		self.assertIn(res.get("status"), ("BLOCKED", "DRY_RUN_PASS", "DRY_RUN_FAIL"))
		for n in FOREIGN_7:
			self.assertEqual(cint(frappe.db.get_value("Stock Entry", n, "docstatus")), 1)
		# Applied canonical + replacements remain submitted
		self.assertEqual(cint(frappe.db.get_value("Stock Entry", mfg, "docstatus")), 1)
		self.assertEqual(cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31724-1", "docstatus")), 2)
