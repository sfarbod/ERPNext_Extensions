# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture plan / merge / block tests (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
	discover_downstream,
)


class TestManufacturePlanV550(FrappeTestCase):
	def test_m01_po_job08760_post_apply_balanced(self):
		"""After controlled Apply: Golden Rule OK; no further repair needed."""
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("missing canary")
		scan = scan_golden_rule("PO-JOB08760")
		row = next(r for r in scan["rows"] if r["item_code"] == "13200544")
		self.assertEqual(row["status"], "OK")
		self.assertAlmostEqual(flt(row["consumed"]), 1148)
		self.assertAlmostEqual(flt(row["remaining_wip"]), 0)
		self.assertEqual(len(scan["manufactures"]), 1)
		self.assertEqual(scan["historical_stamp"], "5.3.34")

		mfg = scan["manufactures"][0].name
		# Active Manufacture SED contains 13200544 × 1148
		qty = flt(
			frappe.db.sql(
				"""
				select sum(qty) from `tabStock Entry Detail`
				where parent=%s and item_code='13200544' and ifnull(t_warehouse,'')=''
				""",
				mfg,
			)[0][0]
		)
		self.assertAlmostEqual(qty, 1148)

		plan = build_manufacture_plan(
			"PO-JOB08760",
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
		self.assertFalse(plan["apply_allowed"])
		self.assertTrue(any("Nothing to repair" in b for b in plan["blockers"]))

	def test_m06_po_job08830_dn_blocked(self):
		if not frappe.db.exists("Job Card", "PO-JOB08830"):
			self.skipTest("missing canary")
		mfgs = frappe.db.sql(
			"select name from `tabStock Entry` where job_card=%s and purpose='Manufacture' and docstatus=1",
			"PO-JOB08830",
			pluck="name",
		)
		down = discover_downstream("PO-JOB08830", mfgs)
		self.assertTrue(down["blocked"])
		plan = build_manufacture_plan("PO-JOB08830", dispositions=[])
		self.assertTrue(any("Delivery Note" in b or "MERGE_BLOCKED" in b for b in plan["blockers"]))
