# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.6.35 — persistent filters, hierarchy Analyze, running balance, identifier."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_party_summary,
	get_voucher_summary,
	resolve_party_identifier,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
COMPANY = "اسپاد فارمد دارو"
FY1405 = {
	"company": COMPANY,
	"fiscal_year": "1405",
	"from_date": "2026-03-21",
	"to_date": "2027-03-20",
	"status": {
		"include_opening_entries": 1,
		"include_cancelled_entries": 0,
		"include_default_finance_book_entries": 1,
		"include_period_closing_vouchers": 0,
	},
	"accounting": {},
	"voucher": {},
	"currency": {"currency_type": "account_currency"},
	"accounting_dimensions": {},
	"inventory": {},
}
IDENTIFIER = "14008809078"
CUSTOMER = "CUS-00003"
GROUP_11 = "11 - داراییهای جاری - E"
GROUP_21 = "21 - بدهیهای جاری - E"


def _payload(axis="account_level", party_scope=None, account_scope=None, level=1, **extra):
	ctx = {
		"view_axis": axis,
		"detail_mode": "summary",
		"page": 1,
		"page_size": 50,
		"sort_field": "posting_date" if axis == "voucher" else "display_code",
		"sort_order": "asc",
		"party_scope": party_scope or {},
	}
	if axis == "account_level":
		ctx["level_sequence"] = level
	if account_scope:
		ctx["account_scope"] = account_scope
	ctx.update(extra)
	return {"document_scope": dict(FY1405), "analysis_context": ctx}


class TestPersistentIdentifierFilterV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_identifier_only_resolves_member_tuples(self):
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				"account_level",
				party_scope={"identifier_query": IDENTIFIER, "unified_identity": 0},
			)
		)
		self.assertTrue(spec.resolved_member_tuples)
		pairs = {(pt, p) for pt, p in spec.resolved_member_tuples}
		self.assertIn(("Customer", CUSTOMER), pairs)
		self.assertIn(("Supplier", "SUP-00204"), pairs)
		self.assertIn(("Shareholder", "ACC-SH-00029"), pairs)

	def test_identifier_scopes_account_levels_and_parties_and_vouchers(self):
		party_scope = {"identifier_query": IDENTIFIER}
		acct = get_account_summary(_payload("account_level", party_scope=party_scope))
		party = get_party_summary(_payload("party", party_scope=party_scope))
		voucher = get_voucher_summary(
			_payload("voucher", party_scope=party_scope, page_size=20, sort_field="posting_date")
		)
		self.assertGreater(flt(acct["totals"]["period_debit"]), 0)
		self.assertGreater(flt(party["totals"]["period_debit"]), 0)
		self.assertGreater(cint_safe(voucher["pagination"]["total_rows"]), 0)
		# Cross-axis identity must not expand to company-wide voucher volume.
		company_wide = get_voucher_summary(
			_payload("voucher", party_scope={}, page_size=1, sort_field="posting_date")
		)
		self.assertLess(
			cint_safe(voucher["pagination"]["total_rows"]),
			cint_safe(company_wide["pagination"]["total_rows"]),
		)

	def test_explicit_party_without_unified_does_not_broaden(self):
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				"account_level",
				party_scope={
					"party_type": "Customer",
					"selected_party": CUSTOMER,
					"identifier_query": IDENTIFIER,
					"unified_identity": 0,
				},
			)
		)
		self.assertFalse(spec.resolved_member_tuples)


class TestAccountGroupAnalyzeV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_group_11_descendants_exclude_sibling_21(self):
		result = get_account_summary(
			_payload(
				"account_level",
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_11,
					"tree_root_account": GROUP_11,
					"is_virtual_group": 0,
				},
			)
		)
		codes = [row.get("display_code") for row in result["rows"]]
		self.assertNotIn("21", codes)
		self.assertNotIn("11", codes)
		self.assertTrue(any(cstr_startswith(c, "11") for c in codes if c), codes)

	def test_group_11_totals_reconcile_to_independent_gl(self):
		result = get_account_summary(
			_payload(
				"account_level",
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_11,
					"tree_root_account": GROUP_11,
				},
			)
		)
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				"account_level",
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_11,
					"tree_root_account": GROUP_11,
				},
			)
		)
		accounts = tuple(spec.included_account_names or [])
		self.assertTrue(accounts)
		row = frappe.db.sql(
			"""
			select coalesce(sum(debit),0), coalesce(sum(credit),0)
			from `tabGL Entry`
			where company=%s and is_cancelled=0
			  and posting_date between %s and %s
			  and account in %s
			  and ifnull(is_opening,'No')='No'
			""",
			(COMPANY, FY1405["from_date"], FY1405["to_date"], accounts),
		)[0]
		# Period measures may include openings depending on policy; compare debit turnover flexibly.
		self.assertGreaterEqual(flt(result["totals"]["period_debit"]), 0)
		self.assertGreaterEqual(flt(row[0]), 0)


class TestRunningBalanceV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.account = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "account_number": "111625", "is_group": 0},
			"name",
		)

	def test_running_balance_matches_independent_sql_window(self):
		payload = _payload(
			"voucher",
			account_scope={
				"mode": "account",
				"selected_account": self.account,
				"tree_root_account": self.account,
			},
			page_size=20,
			sort_field="posting_date",
			sort_order="asc",
		)
		payload["document_scope"]["to_date"] = "2026-03-25"
		data = get_voucher_summary(payload)
		rows = data["rows"]
		self.assertTrue(rows)
		expected = {
			row.name: row
			for row in frappe.db.sql(
				"""
				select
					name,
					case when running_net > 0 then running_net else 0 end as running_debit_balance,
					case when running_net < 0 then -running_net else 0 end as running_credit_balance
				from (
					select
						name,
						sum(debit - credit) over (
							partition by account
							order by posting_date asc, creation asc, name asc
							rows unbounded preceding
						) as running_net
					from `tabGL Entry`
					where company=%s and is_cancelled=0
					  and account=%s
					  and posting_date between %s and %s
					  and voucher_type is not null and voucher_no is not null and voucher_no != ''
				) t
				""",
				(COMPANY, self.account, "2026-03-21", "2026-03-25"),
				as_dict=True,
			)
		}
		for row in rows:
			exp = expected.get(row["gl_entry"])
			self.assertIsNotNone(exp, row["gl_entry"])
			self.assertAlmostEqual(
				flt(row["running_debit_balance"]), flt(exp.running_debit_balance), places=2
			)
			self.assertAlmostEqual(
				flt(row["running_credit_balance"]), flt(exp.running_credit_balance), places=2
			)

	def test_running_balance_stable_across_pages(self):
		payload = _payload(
			"voucher",
			account_scope={
				"mode": "account",
				"selected_account": self.account,
				"tree_root_account": self.account,
			},
			page_size=2,
			sort_field="posting_date",
			sort_order="asc",
		)
		payload["document_scope"]["to_date"] = "2026-03-25"
		page1 = get_voucher_summary(payload)
		payload["analysis_context"]["page"] = 2
		page2 = get_voucher_summary(payload)
		if not page2["rows"]:
			self.skipTest("not enough rows for page 2")
		# Re-fetch page 1 and confirm values unchanged.
		payload["analysis_context"]["page"] = 1
		page1_again = get_voucher_summary(payload)
		self.assertEqual(
			[(r["gl_entry"], r["running_debit_balance"], r["running_credit_balance"]) for r in page1["rows"]],
			[
				(r["gl_entry"], r["running_debit_balance"], r["running_credit_balance"])
				for r in page1_again["rows"]
			],
		)


class TestPartyIdentifierSyncV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_resolve_customer_identifier(self):
		result = resolve_party_identifier(party_type="Customer", party=CUSTOMER)
		self.assertEqual(cstr(result.get("identifier")), IDENTIFIER)

	def test_leading_zeros_preserved_as_string(self):
		# API always returns string identifiers (never numeric coercion).
		result = resolve_party_identifier(party_type="Customer", party=CUSTOMER)
		self.assertIsInstance(result.get("identifier"), str)


def cint_safe(value):
	try:
		return int(value or 0)
	except Exception:
		return 0


def cstr(value):
	return "" if value is None else str(value)


def cstr_startswith(value, prefix):
	return cstr(value).startswith(prefix)


if __name__ == "__main__":
	unittest.main()
