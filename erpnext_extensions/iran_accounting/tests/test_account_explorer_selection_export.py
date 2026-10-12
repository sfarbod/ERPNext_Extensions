# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.export import (
	_filter_rows_by_selected_keys,
	_normalize_selected_row_keys,
	collect_export_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


class TestAccountExplorerSelectionExport(FrappeTestCase):
	def setUp(self):
		self.company = frappe.db.get_value("Company", {"name": "اسپاد فارمد دارو"}, "name")
		if not self.company:
			self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("No Company")
		self.fy = frappe.db.get_value(
			"Fiscal Year",
			{"name": "1405"},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not self.fy:
			self.skipTest("Fiscal Year 1405 required")

	def _payload(self, axis="account_level", **analysis):
		return {
			"document_scope": {
				"company": self.company,
				"fiscal_year": self.fy.name,
				"from_date": str(self.fy.year_start_date),
				"to_date": str(self.fy.year_end_date),
				"hide_zero_rows": 1,
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
			"analysis_context": {
				"view_axis": axis,
				"detail_mode": "summary",
				"page": 1,
				"page_size": 50,
				"level_sequence": 1,
				**analysis,
			},
		}

	def test_normalize_and_filter_by_row_key(self):
		keys = _normalize_selected_row_keys(["item:A", "item:A", " item:B ", ""])
		self.assertEqual(keys, ["item:A", "item:B"])
		rows = [
			{"row_key": "item:A", "debit_balance": 1},
			{"row_key": "item:C", "debit_balance": 9},
			{"row_key": "item:B", "debit_balance": 2},
		]
		filtered = _filter_rows_by_selected_keys(rows, keys)
		self.assertEqual([r["row_key"] for r in filtered], ["item:A", "item:B"])

	def test_selected_export_uses_row_keys_not_indexes(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload("item"), require_dates=True)
		all_rows, _totals, total = collect_export_rows(spec, row_scope="all")
		if total < 2:
			self.skipTest("Need at least two item rows")
		# Pick the second and first keys in reverse order to prove identity order.
		keys = [all_rows[1]["row_key"], all_rows[0]["row_key"]]
		selected, selected_totals, selected_n = collect_export_rows(
			spec, row_scope="selected", selected_row_keys=keys
		)
		self.assertEqual(selected_n, 2)
		self.assertEqual([row["row_key"] for row in selected], keys)
		# Totals must reflect only the selected rows.
		self.assertEqual(
			flt(selected_totals.get("credit_balance")) + flt(selected_totals.get("debit_balance")),
			sum(flt(r.get("credit_balance")) + flt(r.get("debit_balance")) for r in selected),
		)

	def test_selected_export_requires_keys(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload("account_level"), require_dates=True)
		with self.assertRaises(Exception):
			collect_export_rows(spec, row_scope="selected", selected_row_keys=[])
