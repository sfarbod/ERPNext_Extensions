# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Facility Balance report v5.7.49 column order and period reconciliations."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import add_days, flt, getdate, today

from erpnext_extensions.facility_management.facility_monetary import parse_facility_amount
from erpnext_extensions.facility_management.report.facility_balance.facility_balance import (
	_count_remaining_installments,
	execute,
	get_columns,
	get_data,
)


class TestFacilityBalanceReportV5749(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = frappe.db.sql(
			"""
			SELECT c.name FROM `tabCompany` c
			WHERE c.name NOT LIKE '\\_Test%%'
			  AND EXISTS (SELECT 1 FROM `tabAccount` a WHERE a.company=c.name AND a.is_group=0 LIMIT 1)
			ORDER BY c.creation ASC LIMIT 1
			"""
		)[0][0]

	def test_column_order_and_count(self):
		cols = get_columns()
		self.assertEqual(len(cols), 14)
		fieldnames = [c["fieldname"] for c in cols]
		self.assertEqual(
			fieldnames,
			[
				"facility",
				"facility_name",
				"principal_amount",
				"profit_amount",
				"opening_principal_balance",
				"opening_profit_balance",
				"period_paid_profit",
				"period_paid_principal",
				"remaining_principal",
				"remaining_profit",
				"remaining_total",
				"last_payment_date",
				"remaining_installments",
				"maturity_date",
			],
		)

	def test_remaining_total_invariant_on_live_data(self):
		rows = get_data({"company": self.company, "status": "Active"})
		self.assertTrue(rows)
		for row in rows[:50]:
			self.assertAlmostEqual(
				flt(row["remaining_total"]),
				flt(row["remaining_principal"]) + flt(row["remaining_profit"]),
				places=6,
				msg=row["facility"],
			)

	def test_period_reconciliation_identity(self):
		"""opening_balance - period_paid == remaining (principal & profit)."""
		# Pick a facility with repayments
		name = frappe.db.sql(
			"""
			SELECT f.name
			FROM `tabFacility` f
			WHERE f.company=%s AND f.status='Active'
			  AND EXISTS (
			  	SELECT 1 FROM `tabFacility Repayment` r
			  	WHERE r.facility=f.name AND r.docstatus=1
			  )
			ORDER BY f.modified DESC LIMIT 1
			""",
			(self.company,),
		)[0][0]
		to_date = getdate(today())
		from_date = add_days(to_date, -120)
		row = get_data(
			{"company": self.company, "facility": name, "from_date": from_date, "to_date": to_date}
		)[0]
		self.assertAlmostEqual(
			flt(row["opening_principal_balance"]) - flt(row["period_paid_principal"]),
			flt(row["remaining_principal"]),
			places=6,
		)
		self.assertAlmostEqual(
			flt(row["opening_profit_balance"]) - flt(row["period_paid_profit"]),
			flt(row["remaining_profit"]),
			places=6,
		)

	def test_draft_excluded_when_status_active(self):
		rows = get_data({"company": self.company, "status": "Active"})
		for row in rows:
			status = frappe.db.get_value("Facility", row["facility"], "status")
			self.assertEqual(status, "Active")

	def test_company_isolation(self):
		other = frappe.db.sql(
			"""
			SELECT name FROM `tabCompany`
			WHERE name != %s AND name NOT LIKE '\\_Test%%'
			LIMIT 1
			""",
			(self.company,),
		)
		if not other:
			self.skipTest("no second company")
		rows = get_data({"company": self.company})
		for row in rows:
			co = frappe.db.get_value("Facility", row["facility"], "company")
			self.assertEqual(co, self.company)

	def test_remaining_installment_counter(self):
		schedule = [
			{"principal_amount": 100, "profit_amount": 10},
			{"principal_amount": 100, "profit_amount": 10},
			{"principal_amount": 100, "profit_amount": 10},
		]
		# Full schedule, 200 principal paid → 1 remaining (third row)
		n = _count_remaining_installments(
			schedule,
			principal=Decimal("300"),
			opening_paid_principal=Decimal("0"),
			repaid_to_date_principal=Decimal("200"),
		)
		self.assertEqual(n, 1)
		# Opening-style: schedule is remaining only; opening paid 200 of 500
		n2 = _count_remaining_installments(
			schedule,
			principal=Decimal("500"),
			opening_paid_principal=Decimal("200"),
			repaid_to_date_principal=Decimal("200"),
		)
		# covered_before=200, paid_for_schedule=200 → first two paid, 1 remaining
		self.assertEqual(n2, 1)

	def test_execute_returns_columns_and_data(self):
		cols, data = execute({"company": self.company})
		self.assertEqual(len(cols), 14)
		self.assertIsInstance(data, list)

	def test_no_repayment_facility_period_paid_zero(self):
		name = frappe.db.sql(
			"""
			SELECT f.name
			FROM `tabFacility` f
			WHERE f.company=%s AND f.status='Active'
			  AND NOT EXISTS (
			  	SELECT 1 FROM `tabFacility Repayment` r
			  	WHERE r.facility=f.name AND r.docstatus=1
			  )
			LIMIT 1
			""",
			(self.company,),
		)
		if not name:
			self.skipTest("no zero-repayment Active facility")
		row = get_data({"company": self.company, "facility": name[0][0]})[0]
		self.assertEqual(flt(row["period_paid_principal"]), 0)
		self.assertEqual(flt(row["period_paid_profit"]), 0)
		self.assertIsNone(row["last_payment_date"])
