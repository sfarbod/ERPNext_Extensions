# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.item_summary import build_item_summary
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


COMPANY = "اسپاد فارمد دارو"
ITEM = "13100057"
CORRUPT_SLE = "MAT-SLE-2026-259388"


class TestAccountExplorerItem13100057(FrappeTestCase):
	"""Parity for Item 13100057 — positive qty, negative SLE valuation."""

	def setUp(self):
		if not frappe.db.exists("Company", COMPANY):
			self.skipTest("Espad company unavailable")
		if not frappe.db.exists("Item", ITEM):
			self.skipTest("Item 13100057 unavailable")
		self.fy = frappe.db.get_value(
			"Fiscal Year",
			{"name": "1405"},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not self.fy:
			self.skipTest("FY1405 required")

	def test_ae_matches_independent_sle_value_buckets(self):
		sle = frappe.db.sql(
			"""
			select
				coalesce(sum(case when posting_date < %s and ifnull(is_cancelled,0)=0
					then stock_value_difference else 0 end), 0) as opening_value,
				coalesce(sum(case when posting_date between %s and %s and ifnull(is_cancelled,0)=0
					and stock_value_difference > 0 then stock_value_difference else 0 end), 0) as inward_value,
				coalesce(sum(case when posting_date between %s and %s and ifnull(is_cancelled,0)=0
					and stock_value_difference < 0 then -stock_value_difference else 0 end), 0) as outward_value,
				coalesce(sum(case when posting_date between %s and %s and ifnull(is_cancelled,0)=0
					and actual_qty > 0 then actual_qty else 0 end), 0) as in_qty,
				coalesce(sum(case when posting_date between %s and %s and ifnull(is_cancelled,0)=0
					and actual_qty < 0 then -actual_qty else 0 end), 0) as out_qty
			from `tabStock Ledger Entry`
			where company=%s and item_code=%s
			""",
			(
				self.fy.year_start_date,
				self.fy.year_start_date,
				self.fy.year_end_date,
				self.fy.year_start_date,
				self.fy.year_end_date,
				self.fy.year_start_date,
				self.fy.year_end_date,
				self.fy.year_start_date,
				self.fy.year_end_date,
				COMPANY,
				ITEM,
			),
			as_dict=True,
		)[0]

		spec = AccountExplorerQuerySpec_from_client(
			{
				"document_scope": {
					"company": COMPANY,
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
					"inventory": {"item": [ITEM]},
				},
				"analysis_context": {
					"view_axis": "item",
					"detail_mode": "summary",
					"page": 1,
					"page_size": 20,
				},
			},
			require_dates=True,
		)
		row = next(
			(r for r in (build_item_summary(spec).get("rows") or []) if r.get("item_code") == ITEM),
			None,
		)
		self.assertIsNotNone(row)
		# Opening rolled into inward by presentation contract.
		expected_inward = flt(sle.opening_value) + flt(sle.inward_value)
		self.assertEqual(flt(row.get("inward_value")), expected_inward)
		self.assertEqual(flt(row.get("outward_value")), flt(sle.outward_value))
		self.assertEqual(flt(row.get("balance_qty")), flt(sle.in_qty) - flt(sle.out_qty))
		signed = expected_inward - flt(sle.outward_value)
		self.assertGreater(flt(row.get("balance_qty")), 0)
		self.assertLess(signed, 0)
		self.assertEqual(flt(row.get("credit_balance")), abs(signed))
		self.assertEqual(flt(row.get("debit_balance")), 0)

	def test_corrupt_issue_sle_still_present(self):
		"""Document the known valuation outlier; do not auto-repair."""
		if not frappe.db.exists("Stock Ledger Entry", CORRUPT_SLE):
			self.skipTest("Corrupt SLE fixture not on this site")
		row = frappe.db.get_value(
			"Stock Ledger Entry",
			CORRUPT_SLE,
			["actual_qty", "stock_value_difference", "valuation_rate", "voucher_no", "is_cancelled"],
			as_dict=True,
		)
		self.assertEqual(cint_safe(row.is_cancelled), 0)
		self.assertEqual(flt(row.actual_qty), -1)
		self.assertLess(flt(row.stock_value_difference), -1e12)


def cint_safe(value):
	from frappe.utils import cint

	return cint(value)
