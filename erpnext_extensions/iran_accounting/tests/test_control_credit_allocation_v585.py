# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit / site tests for MFG Cost Control Credit Allocation (v5.8.5)."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.allocate_credit import (
	_distribute_pool,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.identify import (
	_classify_landed_line,
	_extract_pa_name,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.document_breakdown import (
	classify_account_control,
)


class TestControlCreditPureV585(unittest.TestCase):
	def test_classifier_prefixes(self):
		self.assertEqual(classify_account_control("211237 - کنترل سربار - E"), "overhead")
		self.assertEqual(classify_account_control("211238 - کنترل دستمزد - E"), "labor")

	def test_pa_extract(self):
		self.assertEqual(
			_extract_pa_name("کنترل سربار (Account Control 211237) | 1405-03 | PA de15e138b8 | qty 1"),
			"de15e138b8",
		)
		self.assertEqual(_extract_pa_name("Operating Cost for operation x"), "")

	def test_classify_operating_eligible(self):
		status, _reason, pa = _classify_landed_line(
			description="هزینه بسته بندی ماشین Operating Cost",
			has_operating_cost=1,
			operation_id="op-1",
			operating_component="machine",
			mca="",
		)
		self.assertEqual(status, "eligible")
		self.assertEqual(pa, "")

	def test_classify_missing_pa_unresolved(self):
		status, _reason, _pa = _classify_landed_line(
			description="Account Control 211237 | PA deadbeefdead",
			has_operating_cost=0,
			operation_id="",
			operating_component="",
			mca="",
		)
		self.assertEqual(status, "unresolved")

	def test_distribute_reuses_batch_share_residual(self):
		batches = [
			frappe._dict(batch_no="B1", normalized_qty=70, applied_share=0.7, calculated_share=0.7),
			frappe._dict(batch_no="B2", normalized_qty=30, applied_share=0.3, calculated_share=0.3),
		]
		out = _distribute_pool(Decimal("1000"), batches, currency="IRR")
		by_batch = {b.batch_no: (amt, res) for b, amt, res in out}
		self.assertEqual(by_batch["B1"][0] + by_batch["B2"][0], Decimal("1000"))
		self.assertTrue(by_batch["B1"][0] >= by_batch["B2"][0])

	def test_zero_pool(self):
		batches = [
			frappe._dict(batch_no="B1", normalized_qty=10, applied_share=1, calculated_share=1),
		]
		out = _distribute_pool(Decimal("0"), batches, currency="IRR")
		self.assertEqual(out[0][1], Decimal("0"))

	def test_unallocated_formula(self):
		self.assertEqual(Decimal("1000") - Decimal("400"), Decimal("600"))


class TestControlCreditSiteV585(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("Requires connected Frappe site")

	def test_doctype_and_breakdown_fields(self):
		self.assertTrue(frappe.db.exists("DocType", "MFG Cost Control Credit Allocation"))
		self.assertTrue(frappe.db.exists("DocType", "MFG Cost Control Credit Source"))
		meta = frappe.get_meta("MFG Cost Product Allocation Document Breakdown")
		for f in (
			"allocated_labor_share",
			"allocated_overhead_share",
			"unallocated_labor",
			"unallocated_overhead",
		):
			self.assertIsNotNone(meta.get_field(f), f)

	def test_resolve_accounts(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.accounts import (
			resolve_control_accounts,
		)

		company = "اسپاد فارمد دارو"
		if not frappe.db.exists("Company", company):
			self.skipTest("Company missing")
		pair = resolve_control_accounts(company)
		self.assertEqual(classify_account_control(pair.overhead_account), "overhead")
		self.assertEqual(classify_account_control(pair.labor_account), "labor")

	def test_identify_khordad(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.identify import (
			identify_control_credits,
		)

		company = "اسپاد فارمد دارو"
		if not frappe.db.exists("Company", company):
			self.skipTest("Company missing")
		result = identify_control_credits(company=company, persian_year=1405, persian_month=3)
		self.assertGreaterEqual(result.overhead_period_credit, Decimal("351300"))
		self.assertGreaterEqual(result.overhead_eligible, Decimal("0"))

	def test_calculate_idempotent_farvardin(self):
		company = "اسپاد فارمد دارو"
		if not frappe.db.exists("Monthly FG Cost Calculation", {"persian_period": "1405-01"}):
			self.skipTest("No Farvardin monthly calc")
		for name in frappe.get_all(
			"MFG Cost Control Credit Allocation",
			filters={"company": company, "persian_year": 1405, "persian_month": 1},
			pluck="name",
		):
			doc = frappe.get_doc("MFG Cost Control Credit Allocation", name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc(
				"MFG Cost Control Credit Allocation", name, force=1, ignore_permissions=True
			)

		doc = frappe.get_doc(
			{
				"doctype": "MFG Cost Control Credit Allocation",
				"company": company,
				"persian_year": 1405,
				"persian_month": 1,
			}
		).insert(ignore_permissions=True)
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.engine import (
			calculate_control_credit_allocation,
		)

		r1 = calculate_control_credit_allocation(doc.name, commit=False)
		r2 = calculate_control_credit_allocation(doc.name, commit=False)
		self.assertEqual(r1["labor_eligible_credit"], r2["labor_eligible_credit"])
		self.assertEqual(r1["overhead_eligible_credit"], r2["overhead_eligible_credit"])
		self.assertEqual(flt(r1["labor_eligible_credit"]), 0)
		self.assertEqual(flt(r1["overhead_eligible_credit"]), 0)
		frappe.delete_doc(
			"MFG Cost Control Credit Allocation", doc.name, force=1, ignore_permissions=True
		)
		frappe.db.rollback()
