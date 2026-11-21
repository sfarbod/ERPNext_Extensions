# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Tests for Control Credit pool / batch-product reporting (v5.8.5 UI follow-up)."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.reporting import (
	fetch_control_credit_batch_product_details,
	fetch_control_credit_pools,
)


class TestControlCreditReportingPure(unittest.TestCase):
	def test_pool_status_helper_via_rows(self):
		# Smoke: module importable
		self.assertTrue(callable(fetch_control_credit_pools))
		self.assertTrue(callable(fetch_control_credit_batch_product_details))


class TestControlCreditReportingSite(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("Requires connected Frappe site")

	def test_pool_report_labor_overhead_separation(self):
		rows = fetch_control_credit_pools({"persian_period": "1405-03"})
		types = {r["control_type"] for r in rows}
		self.assertEqual(types, {"Labor", "Overhead"})
		oh = next(r for r in rows if r["control_type"] == "Overhead")
		self.assertEqual(flt(oh["eligible_credit"]), 351288)
		self.assertEqual(flt(oh["allocated_amount"]), 351288)
		self.assertEqual(flt(oh["remaining_amount"]), 0)
		self.assertEqual(oh["pool_status"], "Fully Allocated")
		lab = next(r for r in rows if r["control_type"] == "Labor")
		self.assertEqual(flt(lab["eligible_credit"]), 0)

	def test_pool_filter_control_type(self):
		rows = fetch_control_credit_pools({"persian_period": "1405-03", "control_type": "Overhead"})
		self.assertTrue(rows)
		self.assertTrue(all(r["control_type"] == "Overhead" for r in rows))

	def test_pool_company_filter(self):
		rows = fetch_control_credit_pools(
			{"company": "اسپاد فارمد دارو", "persian_year": 1405, "persian_month": 1}
		)
		self.assertTrue(rows)
		self.assertTrue(all(r["company"] == "اسپاد فارمد دارو" for r in rows))
		self.assertTrue(all(flt(r["eligible_credit"]) == 0 for r in rows))

	def test_batch_product_details_khordad(self):
		rows = fetch_control_credit_batch_product_details(
			{"persian_period": "1405-03", "control_type": "Overhead"}
		)
		self.assertTrue(rows)
		total = sum(flt(r["allocated_amount"]) for r in rows)
		self.assertEqual(total, 351288)
		self.assertTrue(all(r["credit_allocation"] for r in rows))
		self.assertTrue(all(r["product_allocation"] for r in rows))
		self.assertTrue(all(r.get("applied_batch_share") is not None for r in rows))

	def test_batch_product_filter_by_product_allocation(self):
		sample = fetch_control_credit_batch_product_details(
			{"persian_period": "1405-03", "control_type": "Overhead"}
		)
		if not sample:
			self.skipTest("No product rows")
		pa = sample[0]["product_allocation"]
		rows = fetch_control_credit_batch_product_details(
			{"product_allocation": pa, "control_type": "Overhead"}
		)
		self.assertTrue(rows)
		self.assertTrue(all(r["product_allocation"] == pa for r in rows))

	def test_script_reports_exist(self):
		self.assertTrue(frappe.db.exists("Report", "MFG Cost Control Credit Pool"))
		self.assertTrue(
			frappe.db.exists("Report", "MFG Cost Control Credit Batch Product Allocation")
		)

	def test_workspace_has_credit_links(self):
		ws = frappe.get_doc("Workspace", "Costing")
		labels = [l.label for l in ws.links if l.type == "Link"]
		self.assertTrue(
			any("Control Credit" in (lbl or "") for lbl in labels),
			labels,
		)
