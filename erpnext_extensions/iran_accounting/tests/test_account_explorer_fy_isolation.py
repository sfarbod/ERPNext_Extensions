# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.opening_balance import (
	get_account_wise_measures,
)
from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
	build_account_level_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
	measures_query_hash,
	query_hash,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


COMPANY = "اسپاد فارمد دارو"


def _fy_payload(fiscal_year: str, from_date: str, to_date: str, **analysis):
	return {
		"document_scope": {
			"company": COMPANY,
			"fiscal_year": fiscal_year,
			"from_date": from_date,
			"to_date": to_date,
			"hide_zero_rows": 0,
			"status": {
				"include_opening_entries": 1,
				"include_cancelled_entries": 0,
				"include_default_finance_book_entries": 1,
				"include_period_closing_vouchers": 0,
			},
		},
		"analysis_context": {
			"view_axis": "account_level",
			"detail_mode": "summary",
			"level_sequence": 1,
			"page": 1,
			"page_size": 50,
			**analysis,
		},
	}


class TestAccountExplorerFYIsolation(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Company", COMPANY):
			self.skipTest("Espad company not available")
		self.fy1404 = frappe.db.get_value(
			"Fiscal Year",
			{"name": "1404"},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		self.fy1405 = frappe.db.get_value(
			"Fiscal Year",
			{"name": "1405"},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not self.fy1404 or not self.fy1405:
			self.skipTest("Fiscal years 1404/1405 required")

	def test_empty_fy1404_has_no_ghost_opening_from_future(self):
		"""No GL on/before FY1404 end ⇒ Account Levels measures must be zero.

		Future-year is_opening=Yes vouchers must not leak into earlier years.
		"""
		period_gl = frappe.db.sql(
			"""
			select count(*) from `tabGL Entry`
			where company=%s and ifnull(is_cancelled,0)=0
			  and posting_date between %s and %s
			""",
			(COMPANY, self.fy1404.year_start_date, self.fy1404.year_end_date),
		)[0][0]
		before_gl = frappe.db.sql(
			"""
			select count(*) from `tabGL Entry`
			where company=%s and ifnull(is_cancelled,0)=0
			  and posting_date < %s
			""",
			(COMPANY, self.fy1404.year_start_date),
		)[0][0]
		if period_gl or before_gl:
			self.skipTest("FY1404 is not an empty historical year on this site")

		spec = AccountExplorerQuerySpec_from_client(
			_fy_payload("1404", str(self.fy1404.year_start_date), str(self.fy1404.year_end_date)),
			require_dates=True,
		)
		measures = get_account_wise_measures(spec)
		nonzero = {
			account: row
			for account, row in measures.items()
			if any(
				flt(row.get(field))
				for field in (
					"opening_debit",
					"opening_credit",
					"period_debit",
					"period_credit",
					"debit_balance",
					"credit_balance",
				)
			)
		}
		self.assertEqual(nonzero, {}, msg="FY1404 must not show future opening balances")

		summary = build_account_level_summary(spec)
		totals = summary.get("totals") or {}
		for field in (
			"opening_debit",
			"opening_credit",
			"period_debit",
			"period_credit",
			"debit_balance",
			"credit_balance",
		):
			self.assertEqual(flt(totals.get(field)), 0.0, msg=f"footer {field}")

	def test_fiscal_year_fingerprint_isolation(self):
		spec_1404 = AccountExplorerQuerySpec_from_client(
			_fy_payload("1404", str(self.fy1404.year_start_date), str(self.fy1404.year_end_date)),
			require_dates=True,
		)
		spec_1405 = AccountExplorerQuerySpec_from_client(
			_fy_payload("1405", str(self.fy1405.year_start_date), str(self.fy1405.year_end_date)),
			require_dates=True,
		)
		self.assertNotEqual(query_hash(spec_1404), query_hash(spec_1405))
		self.assertNotEqual(measures_query_hash(spec_1404), measures_query_hash(spec_1405))

	def test_fy1405_baseline_account_11100409(self):
		from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
			get_enabled_levels,
		)

		level = next((row for row in get_enabled_levels() if int(row.code_length) == 8), None)
		if not level:
			self.skipTest("No 8-digit Account Explorer level")
		payload = _fy_payload(
			"1405",
			str(self.fy1405.year_start_date),
			str(self.fy1405.year_end_date),
			level_sequence=int(level.sequence),
		)
		payload["document_scope"]["hide_zero_rows"] = 1
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		summary = build_account_level_summary(spec)
		match = next(
			(row for row in (summary.get("rows") or []) if str(row.get("display_code")) == "11100409"),
			None,
		)
		self.assertIsNotNone(match)
		self.assertEqual(flt(match.get("debit_balance")), 994353195185.0)
