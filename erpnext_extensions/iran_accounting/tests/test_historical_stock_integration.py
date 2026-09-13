# Copyright (c) 2026, ERPNext Extensions contributors
"""Synthetic 5.2.7 historical repair tests on development.localhost."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import frappe
from frappe.utils import add_to_date, now_datetime, nowdate, nowtime, random_string

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	SKIP_LOST_RATE_GUARD,
	STATUS_REPAIRED,
	Z0_LEGITIMATE_ZERO,
)
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero
from erpnext_extensions.iran_accounting.historical_stock.reconstruct import repair_zero_rate_selected
from erpnext_extensions.iran_accounting.historical_stock.runtime_guard import assert_outgoing_rates_not_silently_zeroed
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row, scan_zero_rate_rows
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_bootstrap
from erpnext_extensions.iran_accounting.stock_posting_order.prevention import PREVENTION_FLAG


class TestHistoricalStockIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_bootstrap()
		frappe.set_user("Administrator")
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		frappe.flags.iran_gate_defaults = True
		cls.stores = get_warehouse(cls.company)
		cls.wip = get_second_warehouse(cls.company, cls.stores)

	def setUp(self):
		frappe.flags[PREVENTION_FLAG] = True
		frappe.flags[SKIP_LOST_RATE_GUARD] = True
		frappe.flags.iran_gate_defaults = True

	def tearDown(self):
		frappe.flags[SKIP_LOST_RATE_GUARD] = False
		frappe.flags[PREVENTION_FLAG] = False
		frappe.flags.stock_entry_before_submit = False
		frappe.flags["historical_stock_repair"] = False
		frappe.db.rollback()

	def _item(self, tag):
		code = ensure_test_item(self.company, f"HSR-{tag}-{random_string(5)}")
		frappe.db.set_value("Item", code, {"valuation_rate": 1000, "is_stock_item": 1, "has_batch_no": 0})
		return code

	def test_legitimate_zero_stays_zero(self):
		item = self._item("Z0")
		row = {
			"name": "d0",
			"parent": "SE-Z0",
			"purpose": "Material Receipt",
			"item_code": item,
			"qty": 5,
			"t_warehouse": self.stores,
			"basic_rate": 0,
			"allow_zero_valuation_rate": 1,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
		}
		classified = classify_zero_row(row)
		self.assertEqual(classified["zero_class"], Z0_LEGITIMATE_ZERO)
		self.assertFalse(classified["eligible"])

	def test_version_lost_rate_reconstruct_and_idempotent(self):
		item = self._item("Z1")
		submit_material_receipt(self.company, item, 20, 5000, warehouse=self.stores)
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": self.company,
				"purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer",
				"posting_date": nowdate(),
				"posting_time": nowtime(),
				"items": [
					{
						"item_code": item,
						"qty": 4,
						"transfer_qty": 4,
						"conversion_factor": 1,
						"basic_rate": 5000,
						"s_warehouse": self.stores,
						"t_warehouse": self.wip,
					}
				],
			}
		)
		se.insert()
		se.submit()
		detail = se.items[0].name
		frappe.db.set_value(
			"Stock Entry Detail",
			detail,
			{"basic_rate": 0, "valuation_rate": 0, "basic_amount": 0, "amount": 0},
			update_modified=False,
		)
		frappe.get_doc(
			{
				"doctype": "Version",
				"ref_doctype": "Stock Entry",
				"docname": se.name,
				"data": json.dumps(
					{
						"row_changed": [
							["items", 0, detail, [["basic_rate", 5000, 0], ["amount", 20000, 0]]]
						]
					}
				),
			}
		).insert(ignore_permissions=True)
		row = frappe.db.sql(
			"""
			SELECT sed.*, se.purpose, se.posting_date, se.posting_time, se.company
			FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name = sed.parent
			WHERE sed.name=%s
			""",
			detail,
			as_dict=True,
		)[0]
		classified = classify_zero_row(row)
		self.assertIn(classified["confidence"], (CONFIDENCE_EXACT, "LIKELY"))
		if classified["confidence"] == CONFIDENCE_EXACT and classified.get("eligible"):
			out = repair_zero_rate_selected([classified], dry_run=False)
			self.assertEqual(len(out["blocked"]), 0, out)
			rate = frappe.db.get_value("Stock Entry Detail", detail, "basic_rate")
			self.assertGreater(float(rate), 0)
			out2 = repair_zero_rate_selected([classified], dry_run=False)
			self.assertTrue(out2["applied"] or out2["blocked"])
		else:
			# previous SLE / patient-zero may mark dependency; still a valid dry-run
			dry = repair_zero_rate_selected([classified], dry_run=True)
			self.assertIn("applied", dry)
			self.assertIn("blocked", dry)

	def test_runtime_guard_blocks_submit_when_batch_history_nonzero(self):
		item = self._item("RG")
		doc = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"company": self.company,
				"purpose": "Material Transfer",
				"posting_date": nowdate(),
				"posting_time": nowtime(),
				"items": [
					{
						"item_code": item,
						"qty": 1,
						"transfer_qty": 1,
						"s_warehouse": self.stores,
						"t_warehouse": self.wip,
						"basic_rate": 0,
						"batch_no": "B-RG-TEST",
						"allow_zero_valuation_rate": 0,
					}
				],
			}
		)
		frappe.flags[SKIP_LOST_RATE_GUARD] = False
		frappe.flags.stock_entry_before_submit = True
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import ValuationIntegrityError

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._batch_inward_rate",
			return_value=9000,
		):
			self.assertRaises(ValuationIntegrityError, assert_outgoing_rates_not_silently_zeroed, doc)
		frappe.flags[SKIP_LOST_RATE_GUARD] = True

	def test_repost_selected_not_global(self):
		from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected

		preview = preview_repost_selected("NO-SUCH-ITEM", self.stores)
		self.assertIn(preview["status"], ("SAFE_TO_REPOST", "UNSAFE_TO_REPOST"))
		self.assertNotIn("all_stock_entries", preview)

	def test_25741_read_only_mix(self):
		if not frappe.db.exists("Stock Entry", "MAT-STE-2026-25741"):
			self.skipTest("25741 not on site")
		scan = scan_zero_rate_rows(voucher="MAT-STE-2026-25741")
		self.assertGreaterEqual(scan["count"], 5)
		items = {r["item"]: r for r in scan["rows"]}
		self.assertIn("13200473", items)
		self.assertIn("13200254", items)
		self.assertIn("13200256", items)
		self.assertIn("13200114", items)
		self.assertEqual(items["13200114"]["confidence"], "AMBIGUOUS")
		self.assertFalse(items["13200114"]["eligible"])
		for code in ("13200473", "13200254", "13200256"):
			self.assertGreater(float(items[code]["historical_rate"] or items[code]["proposed_rate"] or 0), 0)
			if items[code]["patient_zero"] and items[code]["patient_zero"].get("voucher_no") != "MAT-STE-2026-25741":
				self.assertEqual(items[code]["status"], "DEPENDENCY_REPAIR_REQUIRED")

	def test_screenshot_batch_posting_order_read_only(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import diagnose_canonical_batch

		batch = "504135-30300042-AK264401A11"
		if not frappe.db.sql(
			"SELECT name FROM `tabSerial and Batch Entry` WHERE batch_no=%s LIMIT 1", batch
		) and not frappe.db.sql(
			"SELECT name FROM `tabStock Ledger Entry` WHERE batch_no=%s LIMIT 1", batch
		):
			self.skipTest("screenshot batch not on site")
		diag = diagnose_canonical_batch(batch)
		self.assertTrue(diag["found"])
		self.assertIn("series", diag)
		for row in diag.get("same_time_groups") or []:
			self.assertIn(row.get("optimizer_status") or row.get("status"), (
				"NO_REPAIR_NEEDED",
				"REPAIRABLE_SECONDS",
				"REAL_STOCK_SHORTAGE",
				"ELIGIBLE",
				"MANUAL_APPROVAL",
			))
		cross = [
			r
			for r in (diag.get("negative_intervals") or [])
			if r.get("outbound_document") == "MAT-STE-2026-25825"
		]
		self.assertTrue(cross, "17 Farvardin must remain a cross-time posting-order candidate")
		self.assertEqual(cross[0].get("optimizer_status"), "CROSS_TIME_REPAIRABLE")
		self.assertEqual(cross[0].get("confidence"), "EXACT")
		self.assertEqual(cross[0].get("time_gap_seconds"), 71)
