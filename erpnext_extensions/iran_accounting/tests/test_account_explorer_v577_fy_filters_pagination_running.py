# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.7.7 — FY defaults helpers, column filters, pagination, running balance."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt, getdate

from erpnext_extensions.iran_accounting.account_explorer.api import get_voucher_summary
from erpnext_extensions.iran_accounting.account_explorer.column_filters import (
	apply_column_filters_to_rows,
	format_running_balance_display,
	normalize_column_filters,
	running_balance_sql_predicate,
)
from erpnext_extensions.iran_accounting.account_explorer.pagination import paginate_summary_rows
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import (
	AccountExplorerQuerySpec,
	AnalysisContext,
	DocumentScope,
	PaginationState,
)
from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
	_export_batch,
	_running_balance_fields,
	_split_running_net,
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
	"hide_zero_rows": 0,
}


def _voucher_payload(**ctx):
	analysis = {
		"view_axis": "voucher",
		"detail_mode": "summary",
		"page": 1,
		"page_size": 20,
		"sort_field": "posting_date",
		"sort_order": "asc",
		"party_scope": {},
		"account_scope": {},
	}
	analysis.update(ctx)
	return {"document_scope": dict(FY1405), "analysis_context": analysis}


class TestColumnFiltersV577(unittest.TestCase):
	def test_normalize_migrates_legacy_running_keys(self):
		out = normalize_column_filters(
			{"running_debit_balance": "10", "account_code": "1112", "__skip": "x", "": "y"}
		)
		self.assertEqual(out.get("running_balance"), "10")
		self.assertEqual(out.get("account_code"), "1112")
		self.assertNotIn("__skip", out)

	def test_multiple_column_filters_combine(self):
		rows = [
			{"display_code": "1112", "period_debit": 100, "display_title": "A"},
			{"display_code": "2110", "period_debit": 200, "display_title": "B"},
			{"display_code": "1112", "period_debit": 50, "display_title": "C"},
		]
		filtered = apply_column_filters_to_rows(
			rows, {"display_code": "1112", "period_debit": ">=60"}
		)
		self.assertEqual(len(filtered), 1)
		self.assertEqual(filtered[0]["display_title"], "A")

	def test_paginate_applies_filters_before_page_slice(self):
		rows = [{"display_code": f"C{i}", "period_debit": i, "period_credit": 0} for i in range(30)]
		for row in rows:
			row.update(
				{
					"opening_debit": 0,
					"opening_credit": 0,
					"closing_debit": 0,
					"closing_credit": 0,
					"net_balance": row["period_debit"],
					"debit_balance": row["period_debit"],
					"credit_balance": 0,
				}
			)
		spec = AccountExplorerQuerySpec(
			document_scope=DocumentScope(company=COMPANY, hide_zero_rows=False),
			analysis=AnalysisContext(
				pagination=PaginationState(page=1, page_size=5, sort_field="display_code"),
				column_filters={"display_code": "C1"},
			),
		)
		# C1 and C10-C19 match substring "C1"
		result = paginate_summary_rows(rows, spec)
		self.assertTrue(result["pagination"]["total_rows"] >= 5)
		self.assertEqual(len(result["rows"]), 5)
		self.assertTrue(all("C1" in r["display_code"] for r in result["rows"]))
		self.assertIn("total_pages", result["pagination"])


class TestRunningBalanceV577(unittest.TestCase):
	def test_display_dr_cr_zero(self):
		self.assertTrue(format_running_balance_display(100).endswith("Dr"))
		self.assertTrue(format_running_balance_display(-20).endswith("Cr"))
		self.assertEqual(format_running_balance_display(0), "0")

	def test_alternating_debit_credit_sequence(self):
		# Independent expected: prev + debit - credit
		moves = [(100, 0), (0, 30), (0, 90), (50, 0)]
		running = 0.0
		expected = []
		for debit, credit in moves:
			running = running + debit - credit
			expected.append(running)
		self.assertEqual(expected, [100, 70, -20, 30])
		self.assertEqual(_running_balance_fields(100)["running_balance"], 100)
		self.assertIn("Dr", _running_balance_fields(100)["running_balance_display"])
		self.assertIn("Cr", _running_balance_fields(-20)["running_balance_display"])
		# Legacy split still available for migration tests.
		self.assertEqual(_split_running_net(70), (70.0, 0.0))
		self.assertEqual(_split_running_net(-20), (0.0, 20.0))

	def test_voucher_grid_exposes_single_running_balance(self):
		frappe.set_user("Administrator")
		# Narrow scope for speed — first leaf with activity if available.
		account = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "account_number": "111625", "is_group": 0},
			"name",
		)
		if not account:
			self.skipTest("sample account missing")
		payload = _voucher_payload(
			page_size=10,
			account_scope={
				"mode": "account",
				"selected_account": account,
				"tree_root_account": account,
			},
		)
		payload["document_scope"]["to_date"] = "2026-03-25"
		data = get_voucher_summary(payload)
		self.assertTrue(data["rows"])
		row = data["rows"][0]
		self.assertIn("running_balance", row)
		self.assertIn("running_balance_display", row)
		self.assertNotIn("running_debit_balance", row)
		self.assertNotIn("running_credit_balance", row)
		# Independent chronological window (not visual display order).
		expected = {
			r.name: flt(r.running_net)
			for r in frappe.db.sql(
				"""
				select name, sum(debit - credit) over (
					partition by account
					order by posting_date asc, creation asc, name asc
					rows unbounded preceding
				) as running_net
				from `tabGL Entry`
				where company=%s and is_cancelled=0 and account=%s
				  and posting_date between %s and %s
				  and voucher_type is not null and voucher_no is not null and voucher_no != ''
				""",
				(COMPANY, account, "2026-03-21", "2026-03-25"),
				as_dict=True,
			)
		}
		for item in data["rows"]:
			self.assertAlmostEqual(
				flt(item["running_balance"]), expected[item["gl_entry"]], places=2
			)

	def test_running_balance_page_continuity(self):
		frappe.set_user("Administrator")
		account = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "account_number": "111625", "is_group": 0},
			"name",
		)
		if not account:
			self.skipTest("sample account missing")
		base = _voucher_payload(
			page_size=2,
			sort_field="posting_date",
			sort_order="asc",
			account_scope={
				"mode": "account",
				"selected_account": account,
				"tree_root_account": account,
			},
		)
		base["document_scope"]["to_date"] = "2026-03-25"
		page1 = get_voucher_summary(base)
		if cint_safe(page1["pagination"]["total_rows"]) < 3:
			self.skipTest("not enough voucher rows")
		base["analysis_context"]["page"] = 2
		page2 = get_voucher_summary(base)
		self.assertTrue(page2["rows"])
		# Continuity is chronological (creation/name), not display-row accumulation.
		expected = {
			r.name: flt(r.running_net)
			for r in frappe.db.sql(
				"""
				select name, sum(debit - credit) over (
					partition by account
					order by posting_date asc, creation asc, name asc
					rows unbounded preceding
				) as running_net
				from `tabGL Entry`
				where company=%s and is_cancelled=0 and account=%s
				  and posting_date between %s and %s
				  and voucher_type is not null and voucher_no is not null and voucher_no != ''
				""",
				(COMPANY, account, "2026-03-21", "2026-03-25"),
				as_dict=True,
			)
		}
		for item in page1["rows"] + page2["rows"]:
			self.assertAlmostEqual(
				flt(item["running_balance"]), expected[item["gl_entry"]], places=2
			)

	def test_column_filter_voucher_account_code(self):
		frappe.set_user("Administrator")
		payload = _voucher_payload(page_size=5, column_filters={"account_code": "111"})
		payload["document_scope"]["to_date"] = "2026-03-25"
		data = get_voucher_summary(payload)
		for row in data["rows"]:
			self.assertIn("111", cstr(row.get("account_code")))

	def test_grid_export_running_balance_parity(self):
		frappe.set_user("Administrator")
		account = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "account_number": "111625", "is_group": 0},
			"name",
		)
		if not account:
			self.skipTest("sample account missing")
		payload = _voucher_payload(
			page_size=15,
			sort_field="posting_date",
			sort_order="asc",
			account_scope={
				"mode": "account",
				"selected_account": account,
				"tree_root_account": account,
			},
		)
		payload["document_scope"]["to_date"] = "2026-03-25"
		grid = get_voucher_summary(payload)
		spec = AccountExplorerQuerySpec_from_client(payload)
		exported = _export_batch(spec, [], batch_size=15)
		self.assertTrue(grid["rows"])
		self.assertTrue(exported)
		by_gl = {row["gl_entry"]: row for row in exported}
		for row in grid["rows"]:
			exp = by_gl.get(row["gl_entry"])
			self.assertIsNotNone(exp, row["gl_entry"])
			self.assertAlmostEqual(flt(row["running_balance"]), flt(exp["running_balance"]), places=2)
			self.assertEqual(row["running_balance_display"], exp["running_balance_display"])

	def test_running_balance_sql_predicate(self):
		self.assertIn(">=", running_balance_sql_predicate(">=10") or "")
		self.assertIn("<=", running_balance_sql_predicate("0..100") or "")


class TestFiscalYearDatesV577(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_fiscal_year_1405_bounds(self):
		row = frappe.db.get_value(
			"Fiscal Year", "1405", ["year_start_date", "year_end_date"], as_dict=True
		)
		self.assertIsNotNone(row)
		self.assertEqual(getdate(row.year_start_date), getdate("2026-03-21"))
		self.assertEqual(getdate(row.year_end_date), getdate("2027-03-20"))
		# Default To Date must be FY closing — not today.
		self.assertNotEqual(getdate(row.year_end_date), getdate())

	def test_query_spec_preserves_explicit_dates(self):
		payload = _voucher_payload()
		payload["document_scope"]["from_date"] = "2026-04-01"
		payload["document_scope"]["to_date"] = "2026-04-30"
		spec = AccountExplorerQuerySpec_from_client(payload)
		self.assertEqual(getdate(spec.from_date), getdate("2026-04-01"))
		self.assertEqual(getdate(spec.to_date), getdate("2026-04-30"))


class TestPaginationMetaV577(unittest.TestCase):
	def test_pagination_exposes_total_pages(self):
		frappe.set_user("Administrator")
		payload = _voucher_payload(page_size=5)
		payload["document_scope"]["to_date"] = "2026-03-25"
		data = get_voucher_summary(payload)
		pag = data["pagination"]
		self.assertIn("total_pages", pag)
		self.assertIn("has_previous", pag)
		self.assertEqual(pag["page"], 1)
		if pag["total_rows"] > 5:
			self.assertTrue(pag["has_next"])
			self.assertGreaterEqual(pag["total_pages"], 2)


class TestVoucherColumnOrderV577(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_voucher_api_columns_debit_credit_running_remarks(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import VOUCHER_COLUMNS

		ids = [col["id"] for col in VOUCHER_COLUMNS]
		self.assertNotIn("running_debit_balance", ids)
		self.assertNotIn("running_credit_balance", ids)
		debit_i = ids.index("debit")
		credit_i = ids.index("credit")
		running_i = ids.index("running_balance")
		remarks_i = ids.index("remarks")
		self.assertEqual(credit_i, debit_i + 1)
		self.assertEqual(running_i, credit_i + 1)
		self.assertEqual(remarks_i, running_i + 1)

	def test_voucher_export_columns_balance_before_remarks(self):
		from erpnext_extensions.iran_accounting.account_explorer.export import (
			get_export_columns,
		)

		spec = AccountExplorerQuerySpec_from_client(_voucher_payload())
		cols = get_export_columns(spec)
		ids = [col["fieldname"] for col in cols]
		debit_i = ids.index("debit")
		credit_i = ids.index("credit")
		running_i = ids.index("running_balance_display")
		remarks_i = ids.index("remarks")
		self.assertEqual(credit_i, debit_i + 1)
		self.assertEqual(running_i, credit_i + 1)
		self.assertEqual(remarks_i, running_i + 1)

	def test_running_balance_unchanged_by_column_metadata(self):
		"""Column reordering must not alter running_balance values."""
		account = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "account_number": "111625", "is_group": 0},
			"name",
		)
		if not account:
			self.skipTest("sample account missing")
		payload = _voucher_payload(
			page_size=10,
			account_scope={
				"mode": "account",
				"selected_account": account,
				"tree_root_account": account,
			},
		)
		payload["document_scope"]["to_date"] = "2026-03-25"
		data = get_voucher_summary(payload)
		ids = [col["id"] for col in data["columns"]]
		self.assertEqual(ids[ids.index("debit") : ids.index("remarks") + 1], [
			"debit",
			"credit",
			"running_balance",
			"remarks",
		])
		expected = {
			r.name: flt(r.running_net)
			for r in frappe.db.sql(
				"""
				select name, sum(debit - credit) over (
					partition by account
					order by posting_date asc, creation asc, name asc
					rows unbounded preceding
				) as running_net
				from `tabGL Entry`
				where company=%s and is_cancelled=0 and account=%s
				  and posting_date between %s and %s
				  and voucher_type is not null and voucher_no is not null and voucher_no != ''
				""",
				(COMPANY, account, "2026-03-21", "2026-03-25"),
				as_dict=True,
			)
		}
		for row in data["rows"]:
			self.assertAlmostEqual(flt(row["running_balance"]), expected[row["gl_entry"]], places=2)


class TestPartyNameColumnFilterV577(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_party_name_column_filter_uses_enriched_title(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_party_summary

		payload = {
			"document_scope": dict(FY1405),
			"analysis_context": {
				"view_axis": "party",
				"detail_mode": "summary",
				"page": 1,
				"page_size": 50,
				"sort_field": "display_title",
				"sort_order": "asc",
				"party_scope": {},
				"account_scope": {},
				"column_filters": {"display_title": "نانو"},
			},
		}
		payload["document_scope"]["to_date"] = "2026-03-25"
		data = get_party_summary(payload)
		self.assertGreaterEqual(cint_safe(data["pagination"]["total_rows"]), 1)
		for row in data["rows"]:
			self.assertIn("نانو", cstr(row.get("display_title")))

	def test_party_name_alias_normalizes_to_display_title(self):
		out = normalize_column_filters({"party_name": "نانو"})
		self.assertEqual(out.get("display_title"), "نانو")
		self.assertNotIn("party_name", out)


def cint_safe(value):
	try:
		return int(value or 0)
	except Exception:
		return 0


def cstr(value):
	return "" if value is None else str(value)
