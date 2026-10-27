# Copyright (c) 2026, ERPNext Extensions contributors

"""Unit tests for v5.6.25 purchase valuation HRC detection."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	ANOMALY_LCV_ROUNDING,
	ANOMALY_PI_VALUATION,
	ROOT_CAUSE_CATEGORY_A,
	ROOT_CAUSE_CATEGORY_B,
)
from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
	classify_pr_item_discrepancy,
	expected_acquisition_amount,
)


class TestPurchaseValuationDetectionV5625(unittest.TestCase):
	def test_unbilled_not_classified(self):
		row = {
			"qty": 800,
			"base_net_amount": 800_000_000,
			"billed_amt": 0,
			"per_billed": 0,
			"valuation_rate": 1_000_000,
			"amount_difference_with_purchase_invoice": 0,
			"landed_cost_voucher_amount": 0,
		}
		self.assertIsNone(classify_pr_item_discrepancy(row, 800_000_000))

	def test_expected_acquisition_includes_pi_and_lcv(self):
		row = {
			"base_net_amount": 1_000_000_000,
			"amount_difference_with_purchase_invoice": 25_900_332_000,
			"landed_cost_voucher_amount": 1_500,
		}
		self.assertEqual(expected_acquisition_amount(row), 26_900_333_500)

	def test_correct_amount_preserving_not_flagged(self):
		row = {
			"qty": 1000,
			"base_net_amount": 1_000_000_000,
			"billed_amt": 26_900_332_000,
			"per_billed": 100,
			"valuation_rate": 26_900_333.5,
			"amount_difference_with_purchase_invoice": 25_900_332_000,
			"landed_cost_voucher_amount": 1_500,
		}
		self.assertIsNone(classify_pr_item_discrepancy(row, 26_900_333_500))

	def test_category_a_sle_mismatch(self):
		row = {
			"qty": 1000,
			"base_net_amount": 1_000_000_000,
			"billed_amt": 26_900_332_000,
			"per_billed": 100,
			"valuation_rate": 1_000_000,
			"amount_difference_with_purchase_invoice": 25_900_332_000,
			"landed_cost_voucher_amount": 0,
		}
		# SLE still at provisional PR value
		out = classify_pr_item_discrepancy(row, 1_000_000_000)
		self.assertIsNotNone(out)
		self.assertEqual(out["root_cause_category"], ROOT_CAUSE_CATEGORY_A)
		self.assertEqual(out["anomaly_type"], ANOMALY_PI_VALUATION)
		self.assertEqual(out["expected"], 26_900_332_000)
		self.assertEqual(out["difference"], 1_000_000_000 - 26_900_332_000)

	def test_category_b_lcv_integer_vr_inflation(self):
		row = {
			"qty": 800,
			"base_net_amount": 800_000_000,
			"billed_amt": 800_000_000,
			"per_billed": 100,
			"valuation_rate": 1_000_002,  # integerized from 1_000_001.875
			"amount_difference_with_purchase_invoice": 0,
			"landed_cost_voucher_amount": 1_500,
		}
		# Inflated SLE from rate×qty
		out = classify_pr_item_discrepancy(row, 800_001_600)
		self.assertIsNotNone(out)
		self.assertEqual(out["root_cause_category"], ROOT_CAUSE_CATEGORY_B)
		self.assertEqual(out["anomaly_type"], ANOMALY_LCV_ROUNDING)


if __name__ == "__main__":
	unittest.main()
