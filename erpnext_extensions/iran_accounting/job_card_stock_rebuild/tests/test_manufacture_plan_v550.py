# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture plan / merge / block tests (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
	discover_downstream,
)


class TestManufacturePlanV550(FrappeTestCase):
	def test_m01_po_job08760_canonical_includes_13200544(self):
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("missing canary")
		plan = build_manufacture_plan(
			"PO-JOB08760",
			dispositions=[
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 1148,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
		)
		rows = plan["canonical_manufacture"]["rows"]
		found = [r for r in rows if r["item_code"] == "13200544" and r["type"] == "CONSUME"]
		self.assertTrue(found)
		self.assertAlmostEqual(found[0]["qty"], 1148)
		self.assertEqual(plan["canonical_manufacture"]["historical_stamp"], "5.3.34")
		# Returns/logistics not merged into manufacture rows as CONSUME of FG logistics
		self.assertTrue(any(d["role"] == "TEMP CANCEL / RECREATE" for d in plan["documents"]))

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

	def test_m03_po_job09617_stamp_conflict_or_finance(self):
		if not frappe.db.exists("Job Card", "PO-JOB09617"):
			self.skipTest("missing canary")
		plan = build_manufacture_plan("PO-JOB09617", dispositions=[])
		# Mixed stamps historically → finance/blocker unless HISTORICAL with single stamp
		self.assertTrue(plan.get("scan", {}).get("multiple_manufacture") or len(plan.get("merge_documents") or []) >= 2)
