# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.23 — documented inbound dust is not an I4 consume leftover."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.i4_repair import is_precision_dust_inbound


class TestI4PrecisionDust(unittest.TestCase):
	def test_tiny_inbound_qty_times_rate_is_dust(self):
		self.assertTrue(
			is_precision_dust_inbound(
				{
					"actual_qty": 5.3e-05,
					"qty_after_transaction": 5.3e-05,
					"stock_value": 486.0,
					"incoming_rate": 9169811.0,
					"valuation_rate": 9169811.0,
				}
			)
		)

	def test_true_zero_qty_leftover_is_not_dust(self):
		self.assertFalse(
			is_precision_dust_inbound(
				{
					"actual_qty": -2038.0,
					"qty_after_transaction": 0.0,
					"stock_value": 2006334850.0,
					"incoming_rate": 0.0,
					"outgoing_rate": 358938.0,
					"valuation_rate": 358938.0,
				}
			)
		)
