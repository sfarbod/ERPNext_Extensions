# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR GL precision alignment before vanilla debit/credit validation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.irr_gl_precision_align import (
	align_irr_gl_map_to_currency_precision,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_scope import (
	get_riv_target_item_codes,
	is_sle_in_riv_blocking_scope,
)


def _entry(account, debit=0.0, credit=0.0, **extra):
	row = {
		"account": account,
		"debit": debit,
		"credit": credit,
		"debit_in_account_currency": debit,
		"credit_in_account_currency": credit,
		"debit_in_transaction_currency": debit,
		"credit_in_transaction_currency": credit,
		"cost_center": "CC",
		"company": "اسپاد فارمد دارو",
		"posting_date": "2026-08-04",
		"voucher_type": "Stock Entry",
		"voucher_no": "MAT-STE-2026-34391",
		"remarks": "test",
		"is_opening": "No",
	}
	row.update(extra)
	return row


class TestIRRGLPrecisionAlign(unittest.TestCase):
	def test_fractional_manufacture_map_aligns_to_balanced_integer(self):
		"""Reproduce MAT-STE-2026-34391 failure arithmetic.

		Built at precision 2:
		  WIP Cr 50,671,372.03
		  Adj Cr 0.48
		  Inv Dr 50,671,372.51
		Vanilla validate at IRR precision 0 would yield Diff=1.0 and throw.
		"""
		doc = SimpleNamespace(
			doctype="Stock Entry",
			name="MAT-STE-2026-34391",
			company="اسپاد فارمد دارو",
			purpose="Manufacture",
			posting_date="2026-08-04",
		)
		gl_map = [
			_entry("111701 - WIP", credit=50_671_372.03),
			_entry("621301 - Stock Adj", credit=0.48),
			_entry("111605 - Semi FG", debit=50_671_372.51),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - Stock Adj"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		):
			align_irr_gl_map_to_currency_precision(doc, gl_map)

		net = sum(flt(e["debit"]) - flt(e["credit"]) for e in gl_map)
		self.assertEqual(flt(net, 6), 0.0)
		inv = next(e for e in gl_map if e["account"].startswith("111605"))
		wip = next(e for e in gl_map if e["account"].startswith("111701"))
		adj = next(e for e in gl_map if e["account"].startswith("621301"))
		self.assertEqual(inv["debit"], 50_671_373.0)
		self.assertEqual(wip["credit"], 50_671_372.0)
		self.assertEqual(adj["credit"], 1.0)

	def test_already_balanced_integer_map_unchanged(self):
		doc = SimpleNamespace(
			doctype="Stock Entry",
			name="STE-OK",
			company="اسپاد فارمد دارو",
			posting_date="2026-08-04",
		)
		gl_map = [
			_entry("111701 - WIP", credit=100.0),
			_entry("111605 - Inv", debit=100.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			return_value="621301 - Stock Adj",
		):
			align_irr_gl_map_to_currency_precision(doc, gl_map)
		self.assertEqual(len(gl_map), 2)
		self.assertEqual(sum(flt(e["debit"]) - flt(e["credit"]) for e in gl_map), 0.0)

	def test_large_imbalance_not_silently_absorbed(self):
		doc = SimpleNamespace(
			doctype="Stock Entry",
			name="STE-BAD",
			company="اسپاد فارمد دارو",
			posting_date="2026-08-04",
		)
		gl_map = [
			_entry("111701 - WIP", credit=100.0),
			_entry("111605 - Inv", debit=105.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			return_value="621301 - Stock Adj",
		):
			align_irr_gl_map_to_currency_precision(doc, gl_map)
		# 5 IRR gap must remain for vanilla validation to fail closed.
		self.assertEqual(sum(flt(e["debit"]) - flt(e["credit"]) for e in gl_map), 5.0)


class TestScopeGuardStillIntact(unittest.TestCase):
	"""v5.2.26 scope-aware I1 contract must remain unchanged."""

	def test_unrelated_item_still_out_of_scope(self):
		engine = SimpleNamespace(
			repost_doc=SimpleNamespace(
				name="RIV-X",
				item_code="13100134",
				warehouse="WH-Q",
				items_to_be_repost=None,
			),
			args={"item_code": "20100064", "warehouse": "WH-FG", "items_to_be_repost": [
				{"item_code": "13100134"},
				{"item_code": "20100064"},
			]},
			company="اسپاد فارمد دارو",
		)
		self.assertEqual(get_riv_target_item_codes(engine), {"13100134"})
		self.assertFalse(
			is_sle_in_riv_blocking_scope(
				engine, {"item_code": "20100064", "warehouse": "WH-FG"}
			)
		)
		self.assertTrue(
			is_sle_in_riv_blocking_scope(
				engine, {"item_code": "13100134", "warehouse": "WH-OTHER"}
			)
		)


if __name__ == "__main__":
	unittest.main()
