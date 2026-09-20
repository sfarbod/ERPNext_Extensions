# Copyright (c) 2026, ERPNext Extensions contributors
"""Blocker lane / KEY / USER vs TOOL_LIMIT regressions (v5.3.0)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.blockers import (
	LANE_TOOL_LIMIT,
	LANE_USER_ACTION,
	NEGATIVE_STOCK_REQUIRES_USER,
	build_blocker_key,
)


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
		self.assertTrue(u.startswith(LANE_USER_ACTION))
		self.assertTrue(t.startswith(LANE_TOOL_LIMIT))

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


if __name__ == "__main__":
	unittest.main()
