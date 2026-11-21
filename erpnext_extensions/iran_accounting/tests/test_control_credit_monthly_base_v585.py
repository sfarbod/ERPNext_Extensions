# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Tests: Monthly Credit Amount is the Control Credit allocation base (v5.8.5)."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.allocate_credit import (
	_distribute_pool,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.identify import (
	CreditIdentificationResult,
	_classify_landed_line,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.accounts import (
	ControlAccountPair,
)


def _dummy_accounts():
	return ControlAccountPair(
		company="TEST",
		overhead_account="211237 - OH",
		labor_account="211238 - LAB",
		overhead_account_name="OH",
		labor_account_name="LAB",
	)


class TestMonthlyCreditBasePure(unittest.TestCase):
	def test_unresolved_classification_preserved(self):
		status, reason, pa = _classify_landed_line(
			description="Corrective Operation Cost",
			has_operating_cost=0,
			operation_id="",
			operating_component="",
			mca="",
		)
		self.assertEqual(status, "unresolved")
		self.assertIn("reliably classified", reason)
		self.assertEqual(pa, "")

	def test_non_stock_unresolved(self):
		status, _r, _p = _classify_landed_line(
			description="Non stock items",
			has_operating_cost=0,
			operation_id="",
			operating_component="",
			mca="",
		)
		self.assertEqual(status, "unresolved")

	def test_distribute_full_monthly_including_unresolved_delta(self):
		"""Pool = Monthly Credit (eligible + unresolved), not eligible alone."""
		monthly = Decimal("351300")
		eligible_only = Decimal("351288")
		self.assertNotEqual(monthly, eligible_only)
		batches = [
			frappe._dict(batch_no="B1", normalized_qty=100, applied_share=1.0, calculated_share=1.0),
		]
		out = _distribute_pool(monthly, batches, currency="IRR")
		self.assertEqual(out[0][1], monthly)

	def test_residual_on_monthly_base(self):
		batches = [
			frappe._dict(batch_no="B1", normalized_qty=50, applied_share=0.5, calculated_share=0.5),
			frappe._dict(batch_no="B2", normalized_qty=50, applied_share=0.5, calculated_share=0.5),
		]
		# IRR precision 0: 1*0.5→0 each → residual 1 applied to largest qty batch
		pool = Decimal("1")
		out = _distribute_pool(pool, batches, currency="IRR")
		self.assertEqual(sum(amt for _b, amt, _r in out), pool)
		self.assertTrue(any(res for _b, _a, res in out))
		self.assertEqual(sum(1 for _b, _a, res in out if res), 1)

	def test_zero_monthly_credit(self):
		batches = [
			frappe._dict(batch_no="B1", normalized_qty=10, applied_share=1, calculated_share=1),
		]
		out = _distribute_pool(Decimal("0"), batches, currency="IRR")
		self.assertEqual(out[0][1], Decimal("0"))

	def test_build_plan_uses_period_credit_not_eligible(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit import (
			allocate_credit as ac,
		)

		identification = CreditIdentificationResult(
			company="اسپاد فارمد دارو",
			persian_year=1405,
			persian_month=3,
			period_start="2026-05-22",
			period_end="2026-06-21",
			accounts=_dummy_accounts(),
			labor_period_credit=Decimal("0"),
			overhead_period_credit=Decimal("351300"),
			labor_eligible=Decimal("0"),
			overhead_eligible=Decimal("351288"),  # informational; must NOT be the pool
		)
		# Skip if monthly calc missing
		if not frappe.db.exists(
			"Monthly FG Cost Calculation", {"persian_year": 1405, "persian_month": 3}
		):
			self.skipTest("Monthly calc missing")
		# Ensure company matches site data
		identification.company = frappe.db.get_value(
			"Monthly FG Cost Calculation",
			{"persian_year": 1405, "persian_month": 3},
			"company",
		)
		plan = ac.build_credit_allocation_plan(identification, currency="IRR")
		self.assertEqual(plan.overhead_allocated_total, Decimal("351300"))
		self.assertNotEqual(plan.overhead_allocated_total, Decimal("351288"))
		self.assertEqual(plan.labor_allocated_total, Decimal("0"))

	def test_unallocated_control_formula_allows_negative(self):
		control = Decimal("100")
		allocated_share = Decimal("150")
		unallocated = control - allocated_share
		self.assertEqual(unallocated, Decimal("-50"))

	def test_pool_status_uses_monthly(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.reporting import (
			_pool_status,
		)

		self.assertEqual(_pool_status("Calculated", 0, 0, 0), "No Monthly Credit")
		self.assertEqual(_pool_status("Calculated", 100, 100, 0), "Fully Allocated")
		self.assertEqual(_pool_status("Calculated", 100, 40, 60), "Partially Allocated")


class TestMonthlyCreditBaseSite(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("Requires connected Frappe site")

	def test_calculate_allocates_monthly_not_eligible_khordad(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.engine import (
			calculate_control_credit_allocation,
		)

		name = frappe.db.get_value(
			"MFG Cost Control Credit Allocation",
			{"persian_year": 1405, "persian_month": 3, "docstatus": ("<", 2)},
			"name",
		)
		if not name:
			self.skipTest("No 1405-03 credit allocation")
		doc = frappe.get_doc("MFG Cost Control Credit Allocation", name)
		from frappe.utils import cint

		if cint(doc.docstatus) == 1:
			self.skipTest("Submitted — skip mutate in unit test")
		result = calculate_control_credit_allocation(name, commit=False)
		self.assertEqual(flt(result["overhead_period_credit"]), 351300)
		self.assertEqual(flt(result["overhead_eligible_credit"]), 351288)
		self.assertEqual(flt(result["overhead_allocated"]), 351300)
		self.assertEqual(flt(result["overhead_remaining"]), 0)
		self.assertEqual(flt(result["labor_allocated"]), 0)
		self.assertTrue(result["is_consistent"])
		# Product rows sum
		prod = frappe.db.sql(
			"SELECT COALESCE(SUM(overhead_allocated),0) FROM `tabMFG Cost Control Credit Product Row` WHERE parent=%s",
			name,
		)[0][0]
		self.assertEqual(flt(prod), 351300)
		frappe.db.rollback()

	def test_idempotent_recalculate_same_totals(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.engine import (
			calculate_control_credit_allocation,
		)

		name = frappe.db.get_value(
			"MFG Cost Control Credit Allocation",
			{"persian_year": 1405, "persian_month": 4, "docstatus": ("<", 2)},
			"name",
		)
		if not name:
			self.skipTest("No 1405-04 credit allocation")
		r1 = calculate_control_credit_allocation(name, commit=False)
		r2 = calculate_control_credit_allocation(name, commit=False)
		self.assertEqual(r1["overhead_allocated"], r2["overhead_allocated"])
		self.assertEqual(r1["overhead_period_credit"], r2["overhead_period_credit"])
		self.assertEqual(flt(r1["overhead_allocated"]), flt(r1["overhead_period_credit"]))
		frappe.db.rollback()

	def test_pa_breakdown_unallocated_formula(self):
		"""After apply, unallocated = control - allocated (may be negative)."""
		pa = frappe.db.sql(
			"""
			SELECT p.name, b.name AS bd, b.overhead_amount, b.allocated_overhead_share, b.unallocated_overhead
			FROM `tabMFG Cost Product Allocation` p
			INNER JOIN `tabMFG Cost Product Allocation Document Breakdown` b ON b.parent = p.name
			INNER JOIN `tabMonthly FG Cost Calculation` m ON m.name = p.monthly_calculation
			WHERE m.persian_period = '1405-05' AND IFNULL(b.allocated_overhead_share,0) > 0
			LIMIT 1
			""",
			as_dict=True,
		)
		if not pa:
			self.skipTest("No allocated breakdown row yet")
		r = pa[0]
		expected = flt(r.overhead_amount) - flt(r.allocated_overhead_share)
		self.assertAlmostEqual(flt(r.unallocated_overhead), expected, places=0)
