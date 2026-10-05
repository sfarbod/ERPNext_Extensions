# Copyright (c) 2026, ERPNext Extensions contributors
"""Focused Golden Rule / scrap pairing / disposition tests (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	scan_golden_rule,
	validate_dispositions,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.scrap_pairing import (
	golden_remainder,
	pair_scrap_on_manufacture,
)


class TestGoldenRuleV550(FrappeTestCase):
	def test_g01_g04_paired_scrap_remainder_zero(self):
		"""Consume includes scrap-source; scrap output must not double-count."""
		details = [
			{"item_code": "A", "batch_no": "B1", "qty": 100, "s_warehouse": "WIP", "t_warehouse": None},
			{"item_code": "A", "batch_no": "B1", "qty": 10, "s_warehouse": "WIP", "t_warehouse": None},
			{
				"item_code": "A",
				"batch_no": "B1",
				"qty": 10,
				"s_warehouse": None,
				"t_warehouse": "SCRAP",
				"custom_output_class": "COMPONENT_SCRAP",
				"secondary_item_type": "Scrap",
			},
		]
		pairing = pair_scrap_on_manufacture(details)
		self.assertTrue(pairing["ok"])
		self.assertEqual(flt(pairing["paired_scrap_qty"][("A", "B1")]), 10)
		rem = golden_remainder(issued=110, returned=0, consumed=110, component_scrap=10, paired_scrap=10)
		self.assertEqual(flt(rem), 0)

	def test_g05_unpaired_scrap_blocked(self):
		details = [
			{"item_code": "A", "batch_no": "B1", "qty": 5, "s_warehouse": "WIP", "t_warehouse": None},
			{
				"item_code": "A",
				"batch_no": "B1",
				"qty": 10,
				"s_warehouse": None,
				"t_warehouse": "SCRAP",
				"custom_output_class": "COMPONENT_SCRAP",
			},
		]
		pairing = pair_scrap_on_manufacture(details)
		self.assertFalse(pairing["ok"])
		self.assertEqual(len(pairing["unpaired_scrap"]), 1)

	def test_g06_g07_disposition_split_and_over_alloc(self):
		rows = [
			{
				"item_code": "X",
				"batch_no": "B",
				"remaining_wip": 1148,
				"issued": 1160,
				"returned": 12,
				"consumed": 0,
				"scrap": 0,
			}
		]
		ok, errors, norm = validate_dispositions(
			rows,
			[
				{
					"item_code": "X",
					"batch_no": "B",
					"proposed_consumed": 1100,
					"proposed_scrap": 48,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
		)
		self.assertTrue(ok)
		self.assertEqual(flt(norm[0]["proposed_consumed"]), 1100)
		ok2, errors2, _ = validate_dispositions(
			rows,
			[
				{
					"item_code": "X",
					"batch_no": "B",
					"proposed_consumed": 2000,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
		)
		self.assertFalse(ok2)
		self.assertTrue(errors2)

	def test_g02_po_job08760_missing_consumption(self):
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("PO-JOB08760 not on site")
		scan = scan_golden_rule("PO-JOB08760")
		row = next(r for r in scan["rows"] if r["item_code"] == "13200544")
		self.assertGreater(flt(row["remaining_wip"]), 1000)
		self.assertEqual(row["status"], "MISSING CONSUMPTION")
		self.assertEqual(row["suggested_action"], "CONSUMED")

	def test_scrap_pairing_fixes_false_negative_on_08760(self):
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("PO-JOB08760 not on site")
		scan = scan_golden_rule("PO-JOB08760")
		row = next(r for r in scan["rows"] if r["item_code"] == "13200023")
		# After pairing fix, remainder must not be -70
		self.assertGreaterEqual(flt(row["remaining_wip"]), -1e-6)
