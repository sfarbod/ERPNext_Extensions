# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.consignment_stock.material_loan.constants import (
	F_ISSUE_RATE,
	F_SETTLEMENT_AMOUNT,
)
from erpnext_extensions.consignment_stock.material_loan.recognition_service import (
	create_recognition_journal_entry,
)
from erpnext_extensions.consignment_stock.material_loan.settlement_service import (
	compute_settlement_amounts,
	create_settlement_journal_entry,
)
from erpnext_extensions.consignment_stock.tests.material_loan_helpers import (
	ensure_customer,
	ensure_material_loan_ready,
	ensure_material_loan_settings,
	ensure_material_loan_stock_entry_types,
	ensure_test_item,
	get_irr_company,
	make_material_loan_issue,
	make_material_loan_return,
	party_gl_balance,
	receive_stock,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import enable_perpetual_inventory


def _je_account_totals(je_name: str, account: str) -> tuple[float, float]:
	rows = frappe.get_all(
		"GL Entry",
		filters={
			"voucher_type": "Journal Entry",
			"voucher_no": je_name,
			"account": account,
			"is_cancelled": 0,
		},
		fields=["debit", "credit"],
	)
	return sum(flt(r.debit) for r in rows), sum(flt(r.credit) for r in rows)


def _force_return_actual_value(return_name: str, actual_a: float, settlement_r: float) -> None:
	"""Force A via total_incoming_value while freezing R on settlement amount fields."""
	frappe.db.set_value("Stock Entry", return_name, "total_incoming_value", actual_a)
	for row in frappe.get_all("Stock Entry Detail", filters={"parent": return_name}, pluck="name"):
		frappe.db.set_value("Stock Entry Detail", row, F_SETTLEMENT_AMOUNT, settlement_r)


def _apply_department_on_je_for_site_ad(je, stock_entry_name: str) -> None:
	"""Site Accounting Dimension may require Department on P&L JE lines (Diff account).

	Product JE builders only propagate cost_center today; tests fill Department from the
	source Stock Entry when present so Diff-account submit matches site AD rules.
	"""
	if not je.meta.has_field("department") and not frappe.get_meta("Journal Entry Account").has_field(
		"department"
	):
		return
	dept = None
	for row in frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": stock_entry_name},
		fields=["department"],
	):
		if row.department:
			dept = row.department
			break
	if not dept:
		dept = frappe.db.get_value(
			"Department",
			{"company": je.company},
			"name",
		)
	if not dept:
		return
	for row in je.accounts:
		if not row.get("department"):
			row.department = dept


class TestMaterialLoanValuationDiff(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		ensure_material_loan_ready()
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		frappe.db.set_value("Company", cls.company, "enable_item_wise_inventory_account", 0)
		_, cls.accounts, cls.wh = ensure_material_loan_settings(cls.company)
		cls.types = ensure_material_loan_stock_entry_types()
		cls.item = ensure_test_item(cls.company, "ML-DIFF")
		cls.customer = ensure_customer(cls.company)
		receive_stock(company=cls.company, warehouse=cls.wh, item_code=cls.item, qty=2000, rate=10000)

	def _prepare_recognized_return(self, qty=100):
		issue = make_material_loan_issue(
			company=self.company,
			warehouse=self.wh,
			item_code=self.item,
			qty=qty,
			party_type="Customer",
			party=self.customer,
			stock_entry_type=self.types["issue"],
		)
		frappe.get_doc("Journal Entry", create_recognition_journal_entry(issue.name)).submit()
		rate = flt(frappe.db.get_value("Stock Entry Detail", issue.items[0].name, F_ISSUE_RATE))
		R = flt(qty * rate)
		ret = make_material_loan_return(
			company=self.company,
			warehouse=self.wh,
			item_code=self.item,
			qty=qty,
			party_type="Customer",
			party=self.customer,
			stock_entry_type=self.types["return"],
			issue_name=issue.name,
			issue_detail=issue.items[0].name,
		)
		return issue, ret, R, rate

	def test_d_equals_zero(self):
		issue, ret, R, rate = self._prepare_recognized_return(50)
		party_before = party_gl_balance(
			self.accounts["customer_receivable"], "Customer", self.customer, self.company
		)
		amounts = compute_settlement_amounts(frappe.get_doc("Stock Entry", ret.name))
		self.assertAlmostEqual(amounts["valuation_difference"], 0, places=2)
		je = frappe.get_doc("Journal Entry", create_settlement_journal_entry(ret.name))
		_apply_department_on_je_for_site_ad(je, ret.name)
		je.submit()
		temp_dr, temp_cr = _je_account_totals(je.name, self.accounts["temporary"])
		party_dr, party_cr = _je_account_totals(je.name, self.accounts["customer_receivable"])
		diff_dr, diff_cr = _je_account_totals(je.name, self.accounts["difference"])
		self.assertAlmostEqual(temp_dr, R, places=2)
		self.assertAlmostEqual(party_cr, R, places=2)
		self.assertAlmostEqual(diff_dr + diff_cr, 0, places=2)
		self.assertAlmostEqual(
			party_gl_balance(
				self.accounts["customer_receivable"], "Customer", self.customer, self.company
			),
			party_before - R,
			places=2,
		)
		self.assertEqual(flt(temp_dr) - flt(temp_cr) + flt(party_dr) - flt(party_cr) + flt(diff_dr) - flt(diff_cr), 0)

	def test_d_greater_than_zero(self):
		"""A = R + 200,000 → Dr Temp A / Cr Party R / Cr Diff D."""
		issue, ret, R, rate = self._prepare_recognized_return(100)
		A = flt(R + 200_000)
		D = flt(A - R)
		_force_return_actual_value(ret.name, A, R)
		party_before = party_gl_balance(
			self.accounts["customer_receivable"], "Customer", self.customer, self.company
		)
		amounts = compute_settlement_amounts(frappe.get_doc("Stock Entry", ret.name))
		self.assertAlmostEqual(amounts["actual_return_valuation_amount"], A, places=2)
		self.assertAlmostEqual(amounts["party_settlement_amount"], R, places=2)
		self.assertAlmostEqual(amounts["valuation_difference"], D, places=2)

		je = frappe.get_doc("Journal Entry", create_settlement_journal_entry(ret.name))
		_apply_department_on_je_for_site_ad(je, ret.name)
		temp_line = next(r for r in je.accounts if r.account == self.accounts["temporary"])
		party_line = next(r for r in je.accounts if r.party)
		diff_line = next(r for r in je.accounts if r.account == self.accounts["difference"])
		self.assertAlmostEqual(flt(temp_line.debit_in_account_currency), A, places=2)
		self.assertAlmostEqual(flt(party_line.credit_in_account_currency), R, places=2)
		self.assertAlmostEqual(flt(diff_line.credit_in_account_currency), D, places=2)
		je.submit()

		temp_dr, _ = _je_account_totals(je.name, self.accounts["temporary"])
		_, party_cr = _je_account_totals(je.name, self.accounts["customer_receivable"])
		_, diff_cr = _je_account_totals(je.name, self.accounts["difference"])
		self.assertAlmostEqual(temp_dr, A, places=2)
		self.assertAlmostEqual(party_cr, R, places=2)
		self.assertAlmostEqual(diff_cr, D, places=2)
		self.assertAlmostEqual(temp_dr, party_cr + diff_cr, places=2)
		self.assertAlmostEqual(diff_cr, 200_000, places=2)
		self.assertAlmostEqual(
			party_gl_balance(
				self.accounts["customer_receivable"], "Customer", self.customer, self.company
			),
			party_before - R,
			places=2,
		)

	def test_d_less_than_zero(self):
		"""A = R - 200,000 → Dr Temp A / Dr Diff |D| / Cr Party R."""
		issue, ret, R, rate = self._prepare_recognized_return(100)
		A = flt(R - 200_000)
		D = flt(A - R)
		_force_return_actual_value(ret.name, A, R)
		party_before = party_gl_balance(
			self.accounts["customer_receivable"], "Customer", self.customer, self.company
		)
		amounts = compute_settlement_amounts(frappe.get_doc("Stock Entry", ret.name))
		self.assertAlmostEqual(amounts["valuation_difference"], D, places=2)

		je = frappe.get_doc("Journal Entry", create_settlement_journal_entry(ret.name))
		_apply_department_on_je_for_site_ad(je, ret.name)
		temp_line = next(r for r in je.accounts if r.account == self.accounts["temporary"])
		party_line = next(r for r in je.accounts if r.party)
		diff_line = next(r for r in je.accounts if r.account == self.accounts["difference"])
		self.assertAlmostEqual(flt(temp_line.debit_in_account_currency), A, places=2)
		self.assertAlmostEqual(flt(diff_line.debit_in_account_currency), abs(D), places=2)
		self.assertAlmostEqual(flt(party_line.credit_in_account_currency), R, places=2)
		je.submit()

		temp_dr, _ = _je_account_totals(je.name, self.accounts["temporary"])
		_, party_cr = _je_account_totals(je.name, self.accounts["customer_receivable"])
		diff_dr, _ = _je_account_totals(je.name, self.accounts["difference"])
		self.assertAlmostEqual(temp_dr + diff_dr, party_cr, places=2)
		self.assertAlmostEqual(diff_dr, 200_000, places=2)
		self.assertAlmostEqual(
			party_gl_balance(
				self.accounts["customer_receivable"], "Customer", self.customer, self.company
			),
			party_before - R,
			places=2,
		)
