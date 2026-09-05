# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer import api
from erpnext_extensions.iran_accounting.account_explorer.currency_opening import (
	get_currency_opening_balances,
	get_currency_period_balances,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import AccountExplorerQuerySpec_from_client
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
	enable_wave2c_unified_party,
	require_site,
)

CURRENCY_DUAL_MARKER = "AE-CURRENCY-DUAL"


def _cancel_dual_currency_jes(company: str) -> None:
	for name in frappe.get_all(
		"Journal Entry",
		filters={"company": company, "user_remark": ("like", f"{CURRENCY_DUAL_MARKER}%"), "docstatus": 1},
		pluck="name",
	):
		doc = frappe.get_doc("Journal Entry", name)
		doc.flags.ignore_permissions = True
		doc.cancel()
	frappe.db.commit()


def ensure_dual_currency_fixture(company: str, posting_date: str) -> dict:
	"""Create USD + EUR journal entries with known native and company amounts."""
	_cancel_dual_currency_jes(company)

	usd_account = "_Test Bank USD - _TC"
	eur_account = "_Test Bank EUR - _TC"
	# Prefer a non-stock cash/bank leaf — inventory fixtures may leave Stock accounts
	# as the first INR leaf from an unconstrained get_value().
	inr_account = (
		frappe.db.get_value(
			"Account",
			{"company": company, "name": "Cash - _TC", "is_group": 0},
			"name",
		)
		or frappe.db.get_value(
			"Account",
			{
				"company": company,
				"account_type": "Cash",
				"account_currency": frappe.get_cached_value("Company", company, "default_currency"),
				"is_group": 0,
			},
			"name",
		)
		or frappe.db.sql(
			"""
			select name from `tabAccount`
			where company=%s and is_group=0
			  and ifnull(account_type,'') not in ('Stock', 'Stock Received But Not Billed')
			  and account_currency=%s
			order by name
			limit 1
			""",
			(company, frappe.get_cached_value("Company", company, "default_currency")),
		)
	)
	if isinstance(inr_account, (list, tuple)):
		inr_account = inr_account[0][0] if inr_account else None
	inr_account = inr_account or "Cash - _TC"
	cost_center = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	if not all(
		[
			frappe.db.exists("Account", usd_account),
			frappe.db.exists("Account", eur_account),
			frappe.db.exists("Account", inr_account),
			cost_center,
		]
	):
		return {}

	company_currency = frappe.get_cached_value("Company", company, "default_currency")

	def submit_je(remark: str, foreign_account: str, foreign_amount: float, company_amount: float) -> str:
		exchange_rate = flt(company_amount) / flt(foreign_amount) if foreign_amount else 1
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = company
		je.posting_date = posting_date
		je.user_remark = remark
		je.multi_currency = 1
		je.append(
			"accounts",
			{
				"account": foreign_account,
				"debit_in_account_currency": foreign_amount,
				"debit": company_amount,
				"exchange_rate": exchange_rate,
				"cost_center": cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": inr_account,
				"credit_in_account_currency": company_amount,
				"credit": company_amount,
				"exchange_rate": 1,
				"cost_center": cost_center,
			},
		)
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		return je.name

	usd_native, usd_company = 10.0, 750.0
	eur_native, eur_company = 10.0, 900.0
	usd_je = submit_je(f"{CURRENCY_DUAL_MARKER}-USD", usd_account, usd_native, usd_company)
	eur_je = submit_je(f"{CURRENCY_DUAL_MARKER}-EUR", eur_account, eur_native, eur_company)
	frappe.db.commit()

	return {
		"company_currency": company_currency,
		"usd_account": usd_account,
		"eur_account": eur_account,
		"usd_native": usd_native,
		"usd_company": usd_company,
		"eur_native": eur_native,
		"eur_company": eur_company,
		"usd_je": usd_je,
		"eur_je": eur_je,
		"posting_date": posting_date,
	}


class TestAccountExplorerCurrencySummary(unittest.TestCase):
	def setUp(self):
		self.company = require_site(self)
		enable_wave2c_unified_party()
		frappe.set_user("Administrator")
		fy = current_fiscal_year(self.company)
		if not fy:
			self.skipTest("No fiscal year")
		self.fiscal_year, self.from_date, self.to_date = fy
		self.dual = ensure_dual_currency_fixture(self.company, self.from_date)
		if not self.dual:
			self.skipTest("USD/EUR test accounts unavailable")

	def tearDown(self):
		frappe.set_user("Administrator")
		_cancel_dual_currency_jes(self.company)

	def test_currency_axis_summary_structure(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "currency", "sort_field": "currency"},
		)
		result = api.get_currency_summary(payload)
		self.assertIn("rows", result)
		self.assertIn("totals", result)
		self.assertEqual(result.get("totals_currency"), self.dual["company_currency"])
		column_ids = [col["id"] for col in result.get("columns") or []]
		self.assertIn("company_period_debit", column_ids)
		self.assertIn("period_debit", column_ids)

	def test_currency_axis_blocked_when_disabled(self):
		settings = frappe.get_single("Iran Accounting Settings")
		settings.currency_analysis_enabled = 0
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "currency"},
		)
		with self.assertRaises(frappe.ValidationError):
			api.get_currency_summary(payload)
		settings.currency_analysis_enabled = 1
		settings.flags.ignore_permissions = True
		settings.save()
		frappe.db.commit()

	def test_currency_parity_against_gl(self):
		currency_row = frappe.db.sql(
			"""
			select distinct account_currency from `tabGL Entry`
			where company=%s and ifnull(account_currency,'')!='' and is_cancelled=0 limit 1
			""",
			self.company,
		)
		if not currency_row:
			self.skipTest("No account currency GL data")
		currency = currency_row[0][0]

		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "currency"},
			document={"currency": {"currency_type": "account_currency", "currency": currency}},
		)
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		opening = get_currency_opening_balances(spec).get(currency, (0, 0))
		period = get_currency_period_balances(spec).get(currency, (0, 0))
		result = api.get_currency_summary(payload)
		row = next((item for item in result.get("rows") or [] if item.get("currency") == currency), None)
		if not row:
			self.skipTest("Currency row not returned")
		self.assertAlmostEqual(row["period_debit"], period[0], places=2)
		self.assertAlmostEqual(row["period_credit"], period[1], places=2)
		self.assertAlmostEqual(row["opening_debit"], opening[0], places=2)
		self.assertAlmostEqual(row["opening_credit"], opening[1], places=2)

	def test_dual_currency_native_and_company_amounts(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "currency", "sort_field": "currency", "page_size": 100},
			document={"hide_zero_rows": 0},
		)
		result = api.get_currency_summary(payload)
		by_code = {row.get("currency"): row for row in result.get("rows") or []}

		usd = by_code.get("USD")
		eur = by_code.get("EUR")
		self.assertIsNotNone(usd, "USD row missing")
		self.assertIsNotNone(eur, "EUR row missing")

		self.assertAlmostEqual(flt(usd["period_debit"]), self.dual["usd_native"], places=2)
		self.assertAlmostEqual(flt(usd["company_period_debit"]), self.dual["usd_company"], places=2)
		self.assertAlmostEqual(flt(eur["period_debit"]), self.dual["eur_native"], places=2)
		self.assertAlmostEqual(flt(eur["company_period_debit"]), self.dual["eur_company"], places=2)

		# Totals are company currency only (must not sum 10 USD + 10 EUR as 20).
		totals = result.get("totals") or {}
		self.assertEqual(result.get("totals_currency"), self.dual["company_currency"])
		self.assertGreaterEqual(flt(totals.get("period_debit")), self.dual["usd_company"] + self.dual["eur_company"] - 0.01)

		# Mixed native sum would be tiny vs company totals.
		native_debit_sum = sum(flt(row.get("period_debit")) for row in result.get("rows") or [])
		self.assertNotAlmostEqual(flt(totals.get("period_debit")), native_debit_sum, places=2)

		cc = self.dual["company_currency"]
		labels = [col.get("label") for col in result.get("columns") or []]
		self.assertIn(f"Debit Amount ({cc})", labels)
		self.assertIn("Debit Amount (Currency)", labels)

	def test_currency_axis_grid_cells_have_no_currency_suffix_in_labels(self):
		"""Presentation contract: column headers carry currency; cell values are numeric fields."""
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "currency"},
		)
		result = api.get_currency_summary(payload)
		for row in result.get("rows") or []:
			for field in ("period_debit", "company_period_debit", "period_credit", "company_period_credit"):
				value = row.get(field)
				self.assertFalse(isinstance(value, str), f"{field} must be numeric, got {value!r}")
				self.assertIsInstance(flt(value), float)
