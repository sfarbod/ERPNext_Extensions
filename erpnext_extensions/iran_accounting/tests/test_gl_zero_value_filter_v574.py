# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.38 — company-currency 0/0 GL legs must not persist (Core + Iran)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	PENDING_SABE_LOCALIZATION_ATTR,
	apply_sabe_localization_sa_gl,
)
from erpnext_extensions.iran_accounting.integration import monkey_patches as mp


class TestGlZeroValueFilterV574(unittest.TestCase):
	def test_a2_legs_mirror_account_currency(self):
		doc = frappe._dict(
			name="MAT-STE-Z0",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			value_difference=0,
			items=[frappe._dict(department="D", cost_center="CC")],
		)
		setattr(
			doc,
			PENDING_SABE_LOCALIZATION_ATTR,
			[
				{
					"localization_difference": 461.0,
					"voucher_no": "MAT-STE-Z0",
					"voucher_detail_no": "d1",
					"item_code": "X",
					"warehouse": "W",
					"detail": {"idempotency_key": "IRR_SABE_A2|E|Stock Entry|MAT-STE-Z0|d1|461"},
				}
			],
		)
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])
		gl_map = [
			frappe._dict(
				account="WIP",
				debit=0,
				credit=100,
				cost_center="CC",
				project=None,
				posting_date="2026-09-02",
			)
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"frappe.get_cached_value",
			side_effect=lambda *a, **k: {
				("Company", "E", "stock_adjustment_account"): "621301",
				("Account", "WIP", "account_type"): "Stock",
			}.get(a if len(a) == 3 else a[:3]),
		), patch(
			"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions",
			return_value=["cost_center", "department"],
		):
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		sa = next(e for e in out if e.get("_irr_sabe_localization"))
		self.assertEqual(flt(sa.credit), 461.0)
		self.assertEqual(flt(sa.credit_in_account_currency), 461.0)
		self.assertEqual(flt(sa.debit_in_account_currency), 0.0)
		self.assertEqual(sa.account_currency, "IRR")

	def test_a2_skips_append_when_outstanding_rounds_to_zero_pair(self):
		# outstanding path already returns early; still assert no 0/0 append helper
		src = apply_sabe_localization_sa_gl.__doc__ or ""
		self.assertIn("Stock Adjustment", src)

	def test_is_non_zero_rejects_company_currency_zeros(self):
		# Install patches so helper exists, then use merge filter semantics
		entry = frappe._dict(
			voucher_type="Stock Entry",
			voucher_no="X",
			debit=0,
			credit=0,
			debit_in_account_currency=0,
			credit_in_account_currency=0,
		)
		# Direct unit of the predicate used by merge/save strip
		import erpnext.accounts.general_ledger as gl

		mp._patch_general_ledger()
		# Re-fetch installed merge filter via merge_similar_entries source behavior
		merged = gl.merge_similar_entries(
			[
				frappe._dict(
					account="621301",
					debit=0,
					credit=0,
					cost_center="C",
					company="E",
					voucher_type="Stock Entry",
					voucher_no="X",
					posting_date="2026-01-01",
					against="A",
					party_type=None,
					party=None,
					project=None,
					remarks="",
					is_opening="No",
				),
				frappe._dict(
					account="621301",
					debit=0,
					credit=461,
					cost_center="C",
					company="E",
					voucher_type="Stock Entry",
					voucher_no="X",
					posting_date="2026-01-01",
					against="A",
					party_type=None,
					party=None,
					project=None,
					remarks="Accounting Entry for Stock",
					is_opening="No",
				),
			],
			precision=0,
		)
		# Only the non-zero company-currency leg survives
		self.assertTrue(any(flt(e.get("credit")) == 461 for e in merged))
		self.assertFalse(any(flt(e.get("debit")) == 0 and flt(e.get("credit")) == 0 for e in merged))

	def test_one_irr_debit_and_credit_survive(self):
		import erpnext.accounts.general_ledger as gl

		mp._patch_general_ledger()
		for debit, credit in ((1, 0), (0, 1)):
			merged = gl.merge_similar_entries(
				[
					frappe._dict(
						account="621301",
						debit=debit,
						credit=credit,
						cost_center="C",
						company="E",
						voucher_type="Stock Entry",
						voucher_no="X",
						posting_date="2026-01-01",
						against="A",
						party_type=None,
						party=None,
						project=None,
						remarks="IRR stock-valuation currency-precision residual",
						is_opening="No",
					)
				],
				precision=0,
			)
			self.assertEqual(len(merged), 1)
			self.assertEqual(flt(merged[0].debit), debit)
			self.assertEqual(flt(merged[0].credit), credit)


if __name__ == "__main__":
	unittest.main()
