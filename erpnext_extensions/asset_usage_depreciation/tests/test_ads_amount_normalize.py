# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Unit tests for ADS whole-number normalization / life-final balancing (v5.3.29)."""

from __future__ import annotations

import unittest

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import (
	assert_schedule_whole_and_balanced,
	is_whole_irr_amount,
	normalize_full_schedule_amounts,
	normalize_unposted_tail,
)


class TestAdsAmountNormalize(unittest.TestCase):
	def test_to_depr_amount_half_up(self):
		self.assertEqual(to_depr_amount(13413698.63), 13413699)
		self.assertEqual(to_depr_amount(34652054.79), 34652055)
		self.assertEqual(to_depr_amount(21238356.16), 21238356)

	def test_full_schedule_positive_residual_final_is_whole(self):
		# Independent rounding of early rows overshoots vs last raw → final absorbs negative residual
		raw = [13413698.63, 34652054.79, 34652054.79, 21238356.16]
		total = 13413699 + 34652055 + 34652055 + 21238356  # intentional: recompute via algo
		rows = [{"schedule_date": f"d{i}", "depreciation_amount": a} for i, a in enumerate(raw)]
		# Use sum of raw as depreciable (matches ERPNext schedule that already balances in float)
		depreciable = to_depr_amount(sum(raw))
		# Actually authoritative total is exact int of sum when schedule already sums cleanly
		depreciable = int(sum(raw)) if abs(sum(raw) - int(sum(raw))) < 1e-6 else to_depr_amount(sum(raw))
		# For 3760-like: sum raw == 1224000000 exactly in DB; here use rounded sum path
		depreciable = 104000000  # toy total
		# Rebuild toy so sum works
		raw = [10000000.4, 20000000.4, 30000000.4, 44000000.0]
		depreciable = 104000001  # whole total
		rows = [{"schedule_date": f"d{i}", "depreciation_amount": a} for i, a in enumerate(raw)]
		normalize_full_schedule_amounts(rows, depreciable_total=depreciable)
		for r in rows:
			self.assertTrue(is_whole_irr_amount(r["depreciation_amount"]))
			self.assertEqual(r["depreciation_amount"], to_depr_amount(r["depreciation_amount"]))
		self.assertEqual(sum(r["depreciation_amount"] for r in rows), depreciable)
		# final = total - sum(rounded first n-1)
		self.assertEqual(
			rows[-1]["depreciation_amount"],
			depreciable - sum(r["depreciation_amount"] for r in rows[:-1]),
		)

	def test_full_schedule_negative_residual_final_whole(self):
		# Early rows round up so final is reduced but stays whole
		raw = [10.6, 10.6, 10.6, 10.2]
		depreciable = 42
		rows = [{"schedule_date": f"d{i}", "depreciation_amount": a} for i, a in enumerate(raw)]
		normalize_full_schedule_amounts(rows, depreciable_total=depreciable)
		self.assertEqual([r["depreciation_amount"] for r in rows], [11, 11, 11, 9])
		self.assertEqual(sum(r["depreciation_amount"] for r in rows), 42)
		self.assertTrue(all(is_whole_irr_amount(r["depreciation_amount"]) for r in rows))

	def test_full_schedule_positive_residual(self):
		raw = [10.4, 10.4, 10.4, 10.8]
		depreciable = 42
		rows = [{"schedule_date": f"d{i}", "depreciation_amount": a} for i, a in enumerate(raw)]
		normalize_full_schedule_amounts(rows, depreciable_total=depreciable)
		self.assertEqual([r["depreciation_amount"] for r in rows], [10, 10, 10, 12])
		self.assertEqual(sum(r["depreciation_amount"] for r in rows), 42)

	def test_assert_rejects_fractional(self):
		rows = [
			{"schedule_date": "a", "depreciation_amount": 10, "accumulated_depreciation_amount": 10},
			{"schedule_date": "b", "depreciation_amount": 10.5, "accumulated_depreciation_amount": 20.5},
		]
		with self.assertRaises(Exception):
			assert_schedule_whole_and_balanced(rows, 20)

	def test_unposted_tail_preserves_posted(self):
		rows = [
			{
				"schedule_date": "a",
				"depreciation_amount": 100,
				"journal_entry": "JE-1",
				"accumulated_depreciation_amount": 100,
			},
			{"schedule_date": "b", "depreciation_amount": 10.6, "journal_entry": None},
			{"schedule_date": "c", "depreciation_amount": 10.6, "journal_entry": None},
		]
		normalize_unposted_tail(rows, remaining_depreciable=21, opening_accumulated=100)
		self.assertEqual(rows[0]["depreciation_amount"], 100)
		self.assertEqual(rows[0]["journal_entry"], "JE-1")
		self.assertEqual(rows[1]["depreciation_amount"], 11)
		self.assertEqual(rows[2]["depreciation_amount"], 10)
		self.assertTrue(is_whole_irr_amount(rows[2]["depreciation_amount"]))


if __name__ == "__main__":
	unittest.main()
