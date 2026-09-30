# Copyright (c) 2026, ERPNext Extensions contributors
"""Iran native historical repair — Product Reject / scrap / stage (v5.3.39+)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.manufacture_native_historical import (
	ALREADY_HEALTHY,
	EXACT_REPAIRABLE,
	STRATEGY,
	analyze_iran_native_historical,
	stamp_wrong_rate_row_from_iran_native,
)


class _Row(SimpleNamespace):
	def get(self, k, default=None):
		return getattr(self, k, default)


class TestManufactureNativeHistorical(unittest.TestCase):
	def test_strategy_constant(self):
		self.assertEqual(STRATEGY, "IRAN_NATIVE_HISTORICAL")

	def test_stamp_already_healthy(self):
		row = stamp_wrong_rate_row_from_iran_native(
			{"voucher": "X", "item": "I"},
			evidence={
				"classification": ALREADY_HEALTHY,
				"families": ["PRODUCT_REJECT"],
				"delta_n": 0,
			},
		)
		self.assertEqual(row["status"], "NO_ACTION_REQUIRED")
		self.assertFalse(row["eligible"])
		self.assertEqual(row["source_of_truth"], "iran_native_already_healthy")
		self.assertIsNone(row.get("manual_lane"))

	def test_stamp_exact_repairable_no_proposed_svd_rate(self):
		row = stamp_wrong_rate_row_from_iran_native(
			{"voucher": "X", "item": "I"},
			evidence={
				"classification": EXACT_REPAIRABLE,
				"families": ["VR_SCRAP", "COMPONENT_SCRAP"],
				"delta_n": 2,
				"deltas": [{"item_code": "A", "before_basic_rate": 0, "after_basic_rate": 10}],
			},
		)
		self.assertEqual(row["status"], "RECONSTRUCTABLE")
		self.assertTrue(row["eligible"])
		self.assertEqual(row["repair_strategy"], STRATEGY)
		self.assertEqual(flt0(row.get("proposed_rate")), 0.0)
		self.assertEqual(row["source_of_truth"], "iran_native_historical")

	def test_explicit_valuation_rate_not_cleared_without_bridge(self):
		"""Finance-excluded explicit VR must not be wiped by historical helpers blindly."""
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			clear_core_vr_auto_default_for_zero_byproduct,
		)

		doc = SimpleNamespace(company="X", purpose="Manufacture", items=[])
		row = _Row(
			item_code="BP",
			qty=1,
			basic_rate=1000,
			valuation_type="Valuation Rate",
			secondary_item_type="By-Product",
			allow_zero_valuation_rate=0,
		)
		# Non-zero explicit rate — clear helper must refuse.
		cleared = clear_core_vr_auto_default_for_zero_byproduct(doc, row)
		self.assertFalse(cleared)
		self.assertEqual(row.valuation_type, "Valuation Rate")
		self.assertEqual(row.basic_rate, 1000)


def flt0(v):
	try:
		return float(v or 0)
	except (TypeError, ValueError):
		return 0.0


if __name__ == "__main__":
	unittest.main()
