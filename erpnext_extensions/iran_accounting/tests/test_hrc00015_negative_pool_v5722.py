# Copyright (c) 2026, ERPNext Extensions contributors
"""HRC-00015 — negative pool Decimal-floor + RIV fallthrough (v5.7.22)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch
import unittest

from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	allocate_physical_two_slice,
	_floor_div,
)
from erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle import (
	rewrite_outgoing_bundle_after_core,
)


class TestHrc00015NegativePoolV5722(unittest.TestCase):
	def test_exact_production_error_vector(self):
		plan = allocate_physical_two_slice(Decimal(2), Decimal("-3894157"), Decimal(1))
		self.assertEqual(plan.base_rate, -1947079)
		self.assertEqual(plan.out_value_abs, Decimal("1947079"))

	def test_broken_decimal_floordiv_would_select_wrong_R(self):
		pv, pq = Decimal("-3894157"), Decimal("2")
		self.assertEqual(int(pv // pq), -1947078)
		self.assertEqual(_floor_div(pv, pq), -1947079)

	def _sle(self):
		return {
			"voucher_type": "Delivery Note",
			"voucher_no": "DN-TEST",
			"actual_qty": -1,
			"serial_and_batch_bundle": "bundle-x",
			"company": "اسپاد فارمد دارو",
			"item_code": "13200112",
			"warehouse": "WH",
			"voucher_detail_no": "row1",
		}

	def test_live_submit_negative_pool_fail_closed(self):
		engine = MagicMock()
		engine.company = "اسپاد فارمد دارو"
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.get_all",
				return_value=[
					MagicMock(batch_no="B1", warehouse="WH", item_code="13200112", name="e1", qty=-1)
				],
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.read_batch_pool",
				return_value=(Decimal(2), Decimal("-3894157")),
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.flags",
				{},
			),
		):
			with self.assertRaises(AllocationError) as ctx:
				rewrite_outgoing_bundle_after_core(engine, self._sle(), MagicMock())
			self.assertIn("negative pool_value", str(ctx.exception))

	def test_riv_negative_pool_fallthrough(self):
		engine = MagicMock()
		engine.company = "اسپاد فارمد دارو"
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.get_all",
				return_value=[
					MagicMock(batch_no="B1", warehouse="WH", item_code="13200112", name="e1", qty=-1)
				],
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.read_batch_pool",
				return_value=(Decimal(2), Decimal("-3894157")),
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.flags",
				{"through_repost_item_valuation": True},
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.log_riv_short_pool_event",
				return_value="/tmp/audit.json",
			) as log,
		):
			plan = rewrite_outgoing_bundle_after_core(engine, self._sle(), MagicMock())
			self.assertIsNone(plan)
			log.assert_called_once()
