# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1: CASE D — Manufacture SED overshoot must not leave qty>0 with sv<0.

Item 30100023 / MAT-STE-2026-24815:
  prior transfer incoming 608890 × 2060 = 1_254_313_400
  manufacture SED        608892 × 2060 = 1_254_317_520
  overshoot = 4120 → qty_after=32, stock_value=-4120 (NEGATIVE_STOCK_VALUE)
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt


class TestCaseD30100023NegativeStockValue(unittest.TestCase):
	def test_document_authority_reconstructs_overshoot_to_zero_remaining(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			stock_value=1254313400.0,  # post-vanilla / pre-stamp identity
			stock_value_difference=0.0,
			qty_after_transaction=32.0,
			actual_qty=-2060.0,
			outgoing_rate=608892.0,
			incoming_rate=0.0,
			valuation_rate=608892.0,
			voucher_detail_no="lc9ii40c2g",
			company="اسپاد فارمد دارو",
		)
		# Simulate vanilla already holding previous layer in stock_value and
		# zero svd so value_before = 1254313400.
		sle.stock_value = 1254313400.0
		sle.stock_value_difference = 0.0
		row = frappe._dict(
			additional_cost=0,
			landed_cost_voucher_amount=0,
			basic_rate=608892,
			valuation_rate=608892,
			amount=1254317520,
		)
		with mock.patch.object(ses, "align_sed_from_native_outgoing_sle") as align:
			ses._apply_document_authority_movement(
				sle,
				movement=-1254317520.0,  # SED overshoot
				rate=608892.0,
				ccy="IRR",
				purpose="Manufacture",
				row=row,
			)
		self.assertEqual(flt(sle.stock_value), 0.0)
		self.assertEqual(flt(sle.qty_after_transaction), 32.0)
		self.assertEqual(flt(sle.stock_value_difference), -1254313400.0)
		self.assertLess(flt(sle.stock_value_difference), 0)
		self.assertTrue(align.called)

	def test_reconstruct_repairs_already_poisoned_sle(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			# Already poisoned like MAT-SLE-2026-161119 after bad RIV
			stock_value=-4120.0,
			stock_value_difference=-1254317520.0,
			qty_after_transaction=32.0,
			actual_qty=-2060.0,
			outgoing_rate=608892.0,
			valuation_rate=0.0,
			voucher_detail_no=None,
		)
		changed = ses.reconstruct_nonnegative_remaining_layer(sle, ccy="IRR", row=None)
		self.assertTrue(changed)
		self.assertEqual(flt(sle.stock_value), 0.0)
		self.assertEqual(flt(sle.stock_value_difference), -1254313400.0)
		self.assertEqual(flt(sle.outgoing_rate), 608890.0)

	def test_reconstruct_noop_when_already_nonnegative(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			stock_value=0.0,
			stock_value_difference=-777555084.0,
			qty_after_transaction=32.0,
			actual_qty=-1277.0,
			outgoing_rate=608892.0,
			valuation_rate=608892.0,
		)
		self.assertFalse(ses.reconstruct_nonnegative_remaining_layer(sle, ccy="IRR"))

	def test_rate_delta_explains_4120_shortfall(self):
		"""Document the exact arithmetic of CASE D."""
		incoming_rate = 608890.0
		outgoing_rate = 608892.0
		qty = 2060.0
		shortfall = (outgoing_rate - incoming_rate) * qty
		self.assertEqual(shortfall, 4120.0)
		value_before = incoming_rate * qty  # leftover was 0-valued
		sed_movement = -(outgoing_rate * qty)
		self.assertEqual(value_before + sed_movement, -4120.0)


if __name__ == "__main__":
	unittest.main()
