# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture consume + historical fractional batch TRACE authorization (v5.5.48)."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain import temporary_valuation_adjustment as tva


class TestManufactureFractionalBatchClassify(unittest.TestCase):
	def test_manufacture_consume_classifies_fractional_historical_batch(self):
		"""MAT-STE-2026-40972-1 pattern: Manufacture consume with fractional batch avg."""
		doc = frappe._dict(
			doctype="Stock Entry",
			name="STE-MFG-FRAC",
			purpose="Manufacture",
			company="Test Co",
			posting_date="2026-08-09",
			posting_time="12:00:00",
		)
		row = frappe._dict(
			name="sed-consume",
			idx=1,
			item_code="13200244",
			batch_no="1072-13200244-0409004-hp-168951202-3",
			s_warehouse="WH-WIP",
			t_warehouse=None,
			qty=1504,
			transfer_qty=1504,
			basic_rate=139357,
			basic_amount=209592928,
		)
		# Fractional historical pool (avg ≈ 139357.322)
		bal_qty = 5000.0
		bal_value = 696786611.70035
		with mock.patch.object(tva, "is_irr_company", return_value=True), mock.patch.object(
			tva, "get_company_currency", return_value="IRR"
		), mock.patch.object(
			tva, "resolve_stock_adjustment_account", return_value="621301 - SA"
		), mock.patch.object(
			tva,
			"resolve_fractional_batch_pool",
			return_value=(bal_qty, bal_value, bal_value / bal_qty, "net_sabe"),
		):
			adj = tva.classify_historical_fractional_batch_row(doc, row, bal_qty, bal_value)
		self.assertIsNotNone(adj)
		self.assertEqual(adj["accounting_category"], tva.CATEGORY_TRACE_ONLY)
		self.assertEqual(adj["reason_code"], tva.REASON_HISTORICAL_BATCH_RATE_MISMATCH)
		self.assertTrue((adj.get("payload") or {}).get("allow_fractional_sabe"))
		self.assertEqual(adj["posting_mode"], "TRACE")
		self.assertEqual(adj["debit_credit"], "None")

	def test_observed_native_fractional_sabe_includes_manufacture(self):
		doc = frappe._dict(
			doctype="Stock Entry",
			name="STE-MFG-OBS",
			purpose="Manufacture",
			company="Test Co",
		)
		fake_row = frappe._dict(
			sabe_name="sabe-1",
			batch_no="B1",
			qty=-10,
			incoming_rate=139357.32234007,
			stock_value_difference=-2095934.127,
			warehouse="WH",
			voucher_detail_no="sed-1",
			item_code="ITEM",
		)
		db = mock.MagicMock()
		db.sql.return_value = [fake_row]
		with mock.patch.object(tva, "is_irr_company", return_value=True), mock.patch.object(
			tva, "get_company_currency", return_value="IRR"
		), mock.patch.object(
			tva, "resolve_stock_adjustment_account", return_value="621301 - SA"
		), mock.patch.object(tva.frappe, "db", db):
			out = tva.classify_observed_native_fractional_sabe(doc)
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["accounting_category"], tva.CATEGORY_TRACE_ONLY)

	def test_indivisible_ma_still_skips_manufacture(self):
		"""Stock Adjustment Class A bridge remains excluded for Manufacture."""
		doc = frappe._dict(purpose="Manufacture", company="Test Co")
		row = frappe._dict(s_warehouse="WH", item_code="X", qty=10, transfer_qty=10)
		with mock.patch.object(tva, "is_irr_company", return_value=True):
			self.assertIsNone(tva.classify_indivisible_ma_row(doc, row, "pool", 10, 100.5))
