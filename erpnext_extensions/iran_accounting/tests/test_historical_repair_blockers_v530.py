# Copyright (c) 2026, ERPNext Extensions contributors
"""Blocker key / lane / sync regressions (v5.3.0 Phase 5C)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.blockers import (
	BLOCKER_KEY_MAX_LEN,
	HISTORICAL_NEGATIVE_STOCK,
	LANE_TOOL_LIMIT,
	LANE_USER_ACTION,
	NEGATIVE_STOCK_REQUIRES_USER,
	build_blocker_key,
	canonical_blocker_identity,
	legacy_blocker_key,
)


PERSIAN_WH = "انبار Quarantine محصول نیمه ساخته اسپاد فارمد دارو مسیر طولانی تست"
LONG_BATCH = "BATCH-" + ("الفبای-طولانی-" * 8) + "END"
LONG_VOUCHER = "MAT-STE-2026-27825-EXTRA-LONG-SUFFIX-FOR-IDENTITY"


class TestBlockerKeyAndLane(unittest.TestCase):
	def test_user_and_tool_lanes_differ(self):
		u = build_blocker_key(
			issue_type=NEGATIVE_STOCK_REQUIRES_USER,
			item_code="X",
			warehouse="W",
			batch_no="B",
			voucher_no="V1",
			lane=LANE_USER_ACTION,
		)
		t = build_blocker_key(
			issue_type=NEGATIVE_STOCK_REQUIRES_USER,
			item_code="X",
			warehouse="W",
			batch_no="B",
			voucher_no="V1",
			lane=LANE_TOOL_LIMIT,
		)
		self.assertNotEqual(u, t)
		self.assertTrue(u.startswith("HRB:"))
		self.assertTrue(t.startswith("HRB:"))

	def test_root_dedupe_same_key(self):
		a = build_blocker_key(
			issue_type=NEGATIVE_STOCK_REQUIRES_USER,
			item_code="13100134",
			warehouse="WH",
			batch_no="",
			voucher_no="STE-1",
			lane=LANE_USER_ACTION,
		)
		b = build_blocker_key(
			issue_type=NEGATIVE_STOCK_REQUIRES_USER,
			item_code="13100134",
			warehouse="WH",
			batch_no="",
			voucher_no="STE-1",
			lane=LANE_USER_ACTION,
		)
		self.assertEqual(a, b)

	def test_long_persian_warehouse_under_limit(self):
		key = build_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="30300042",
			warehouse=PERSIAN_WH,
			batch_no=LONG_BATCH,
			voucher_no=LONG_VOUCHER,
			lane=LANE_USER_ACTION,
			company="اسپاد فارمد دارو",
		)
		self.assertLessEqual(len(key), BLOCKER_KEY_MAX_LEN)
		self.assertTrue(key.startswith("HRB:HNS:"))
		# Legacy pipe key would blow past 140.
		legacy = legacy_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="30300042",
			warehouse=PERSIAN_WH,
			batch_no=LONG_BATCH,
			voucher_no=LONG_VOUCHER,
			lane=LANE_USER_ACTION,
		)
		self.assertGreater(len(legacy), BLOCKER_KEY_MAX_LEN)

	def test_long_batch_and_voucher(self):
		key = build_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="ITEM",
			warehouse="W",
			batch_no=LONG_BATCH,
			voucher_no=LONG_VOUCHER,
			lane=LANE_USER_ACTION,
		)
		self.assertLessEqual(len(key), BLOCKER_KEY_MAX_LEN)

	def test_different_blockers_different_keys(self):
		a = build_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="A",
			warehouse="W1",
			voucher_no="V1",
			lane=LANE_USER_ACTION,
		)
		b = build_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="A",
			warehouse="W2",
			voucher_no="V1",
			lane=LANE_USER_ACTION,
		)
		self.assertNotEqual(a, b)

	def test_deterministic_across_calls(self):
		kwargs = dict(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="30300042",
			warehouse=PERSIAN_WH,
			batch_no="B1",
			voucher_no="MAT-STE-2026-27825",
			lane=LANE_USER_ACTION,
			company="اسپاد فارمد دارو",
			dependency_root="MAT-STE-2026-27825",
		)
		keys = [build_blocker_key(**kwargs) for _ in range(5)]
		self.assertEqual(len(set(keys)), 1)

	def test_whitespace_normalization(self):
		a = build_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code=" X ",
			warehouse="W  H",
			voucher_no="V1",
			lane=LANE_USER_ACTION,
		)
		b = build_blocker_key(
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="X",
			warehouse="W H",
			voucher_no="V1",
			lane=LANE_USER_ACTION,
		)
		self.assertEqual(a, b)

	def test_canonical_identity_stable(self):
		i1 = canonical_blocker_identity(
			lane=LANE_USER_ACTION,
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="I",
			warehouse="W",
			voucher_no="V",
		)
		i2 = canonical_blocker_identity(
			lane=LANE_USER_ACTION,
			issue_type=HISTORICAL_NEGATIVE_STOCK,
			item_code="I",
			warehouse="W",
			voucher_no="V",
		)
		self.assertEqual(i1, i2)
		self.assertIn("batch_or_sabb", i1)
		self.assertIn("root", i1)


if __name__ == "__main__":
	unittest.main()
