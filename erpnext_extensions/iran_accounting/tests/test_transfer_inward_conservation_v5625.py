# Copyright (c) 2026, ERPNext Extensions contributors

"""v5.6.25: Material Transfer destination must conserve source SLE amount."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	conserve_transfer_inward_from_outward_sle,
)


class TestTransferInwardConservationV5625(unittest.TestCase):
	def test_dest_uses_outward_amount_not_rate_times_qty(self):
		# Source consumed 5_380_066_700; integer rate×qty would invent +100.
		sle = SimpleNamespace(
			actual_qty=200.0,
			stock_value_difference=5_380_066_800.0,
			stock_value=5_380_066_800.0,
			incoming_rate=26_900_334.0,
			outgoing_rate=0.0,
			valuation_rate=26_900_334.0,
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-X",
			voucher_detail_no="detail-1",
			item_code="ITEM",
			company="E",
		)
		row = SimpleNamespace(
			s_warehouse="SRC",
			t_warehouse="DST",
			additional_cost=0,
			amount=5_380_066_800.0,
			set_basic_rate_manually=0,
			landed_cost_voucher_amount=0,
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync._outward_leg_stock_value_difference",
				return_value=-5_380_066_700.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle"
			) as align,
		):
			ok = conserve_transfer_inward_from_outward_sle(
				sle, row, "Material Transfer", "IRR"
			)
		self.assertTrue(ok)
		self.assertEqual(sle.stock_value_difference, 5_380_066_700.0)
		self.assertEqual(sle.stock_value, 5_380_066_700.0)
		align.assert_called_once()

	def test_skips_when_no_outward_leg(self):
		sle = SimpleNamespace(
			actual_qty=200.0,
			stock_value_difference=100.0,
			stock_value=100.0,
			incoming_rate=1.0,
			outgoing_rate=0.0,
			valuation_rate=1.0,
			voucher_type="Stock Entry",
			voucher_no="X",
			voucher_detail_no="d",
			item_code="I",
			company="E",
		)
		row = SimpleNamespace(
			s_warehouse="S",
			t_warehouse="T",
			additional_cost=0,
			amount=100.0,
			set_basic_rate_manually=0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync._outward_leg_stock_value_difference",
			return_value=None,
		):
			self.assertFalse(
				conserve_transfer_inward_from_outward_sle(sle, row, "Material Transfer", "IRR")
			)

	def test_skips_outgoing_leg(self):
		sle = SimpleNamespace(actual_qty=-200.0)
		row = SimpleNamespace(s_warehouse="S", t_warehouse="T", set_basic_rate_manually=0)
		self.assertFalse(
			conserve_transfer_inward_from_outward_sle(sle, row, "Material Transfer", "IRR")
		)


if __name__ == "__main__":
	unittest.main()
