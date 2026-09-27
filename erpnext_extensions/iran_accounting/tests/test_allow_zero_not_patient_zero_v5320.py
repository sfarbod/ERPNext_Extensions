# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.20 — allow_zero Material Receipt is not Patient Zero."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.patient_zero import (
	find_patient_zero,
	transition_is_invalid,
)


class TestAllowZeroNotPatientZeroV5320(unittest.TestCase):
	def test_zero_inbound_with_allow_zero_is_valid(self):
		row = {
			"name": "SLE1",
			"voucher_no": "MAT-STE-ZERO",
			"item_code": "ITEM-Z",
			"warehouse": "WH",
			"actual_qty": 17,
			"incoming_rate": 0,
			"outgoing_rate": 0,
			"stock_value_difference": 0,
			"stock_value": 0,
			"valuation_rate": 0,
			"qty_after_transaction": 17,
			"posting_datetime": "2026-06-15 10:00:00",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.patient_zero._stock_entry_allows_zero_valuation",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.patient_zero.sle_poison_reason",
			return_value=None,
		):
			self.assertIsNone(transition_is_invalid(None, row))
			self.assertIsNone(find_patient_zero([row]))

	def test_zero_lot_continuation_after_allow_zero_receipt_is_valid(self):
		prev = {
			"voucher_no": "MAT-STE-ZERO",
			"stock_value": 0,
			"valuation_rate": 0,
			"incoming_rate": 0,
			"qty_after_transaction": 17,
		}
		row = {
			"name": "SLE2",
			"voucher_no": "MAT-STE-REPACK",
			"item_code": "ITEM-Z",
			"warehouse": "WH",
			"actual_qty": 17,
			"incoming_rate": 0,
			"outgoing_rate": 0,
			"stock_value_difference": 0,
			"stock_value": 0,
			"valuation_rate": 0,
			"qty_after_transaction": 17,
			"posting_datetime": "2026-06-15 11:00:00",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.patient_zero._stock_entry_allows_zero_valuation",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.patient_zero.sle_poison_reason",
			return_value=None,
		):
			self.assertIsNone(transition_is_invalid(prev, row))

	def test_zero_inbound_without_allow_zero_is_patient_zero(self):
		row = {
			"name": "SLE1",
			"voucher_no": "MAT-STE-BAD",
			"item_code": "ITEM-Z",
			"warehouse": "WH",
			"actual_qty": 17,
			"incoming_rate": 0,
			"outgoing_rate": 0,
			"stock_value_difference": 0,
			"stock_value": 0,
			"valuation_rate": 0,
			"qty_after_transaction": 17,
			"posting_datetime": "2026-06-15 10:00:00",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.patient_zero._stock_entry_allows_zero_valuation",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.patient_zero.sle_poison_reason",
			return_value=None,
		):
			self.assertEqual(transition_is_invalid(None, row), "zero_incoming_with_qty")
			pz = find_patient_zero([row])
			self.assertIsNotNone(pz)
			self.assertEqual(pz["voucher_no"], "MAT-STE-BAD")
			self.assertEqual(pz["reason"], "zero_incoming_with_qty")


if __name__ == "__main__":
	unittest.main()
