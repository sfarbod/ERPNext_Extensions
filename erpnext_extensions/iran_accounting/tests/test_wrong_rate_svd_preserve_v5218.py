# Copyright (c) 2026 — unit test SVD preserve on wrong-rate write
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestWrongRateSVDPreserve(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply.sync_sabb_from_sle", create=True)
	@patch("erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply.write_sle_transaction_rates", create=True)
	@patch("erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply.frappe")
	def test_write_restores_zeroed_svd(self, frappe, *_mocks):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
			_write_sle_expected,
		)
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
			sync_sabb_from_sle,
			write_sle_transaction_rates,
		)

		# Patch imports used inside function
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild.write_sle_transaction_rates"
		) as w, patch(
			"erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild.sync_sabb_from_sle"
		) as s:
			frappe.db.get_value.return_value = MagicMock(
				actual_qty=-1.0,
				stock_value_difference=0.0,
				incoming_rate=0.0,
				outgoing_rate=0.0,
			)
			_write_sle_expected({"sle": "SLE1", "voucher": "V1", "item": "I1"}, 37250000.0)
			args = frappe.db.set_value.call_args
			self.assertEqual(args[0][0], "Stock Ledger Entry")
			self.assertEqual(args[0][1], "SLE1")
			payload = args[0][2]
			self.assertEqual(payload["outgoing_rate"], 37250000.0)
			self.assertEqual(payload["stock_value_difference"], -37250000.0)


if __name__ == "__main__":
	unittest.main()
