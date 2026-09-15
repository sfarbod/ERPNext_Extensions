# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.18 unit tests — global shared-voucher warehouse solver."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver import (
	READY_GLOBAL_WAREHOUSE_SOLVER,
	GLOBAL_WAREHOUSE_OSCILLATION,
	build_shared_voucher_components,
	solve_component,
	_is_micro_oscillation,
)


class TestSharedVoucherComponents(unittest.TestCase):
	def test_pairs_sharing_outbound_merge(self):
		pairs = [
			{
				"item": "A",
				"warehouse": "W",
				"inbound_document": "IN1",
				"outbound_document": "SHARED_OUT",
				"moves": [],
			},
			{
				"item": "B",
				"warehouse": "W",
				"inbound_document": "IN2",
				"outbound_document": "SHARED_OUT",
				"moves": [],
			},
			{
				"item": "C",
				"warehouse": "W2",
				"inbound_document": "IN3",
				"outbound_document": "OUT3",
				"moves": [],
			},
		]
		comps = build_shared_voucher_components(pairs)
		self.assertEqual(len(comps), 2)
		sizes = sorted(c["n_pairs"] for c in comps)
		self.assertEqual(sizes, [1, 2])


class TestGlobalSolveComponent(unittest.TestCase):
	def test_ready_when_multi_clear(self):
		pairs = [
			{
				"item": "I1",
				"warehouse": "W",
				"inbound_document": "IN",
				"outbound_document": "OUT",
				"optimizer_status": "CROSS_ITEM_CONFLICT",
				"current_inbound_time": "2026-08-23 13:00:07",
				"current_outbound_time": "2026-08-23 13:00:06",
				"proposed_inbound_time": "2026-08-23 13:00:07",
				"moves": [],
			}
		]
		multi_ok = {
			"all_clear": True,
			"n_identities": 3,
			"n_failed": 0,
			"identities": [
				{"item": "I1", "warehouse": "W", "clears": True, "row_count": 5, "introduced": []}
			],
			"reason": None,
		}

		def fake_live(vn):
			return {
				"IN": datetime(2026, 8, 23, 13, 0, 7),
				"OUT": datetime(2026, 8, 23, 13, 0, 6),
			}.get(vn)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver.verify_touched_identities_from_times",
			return_value=multi_ok,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver._live_time",
			side_effect=fake_live,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver._multi_item_vouchers",
			return_value=["OUT"],
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver._drop_noop_moves",
			side_effect=lambda proposed, moves: (proposed, moves, []),
		):
			out = solve_component(
				{
					"pairs": pairs,
					"n_identities": 1,
					"n_vouchers": 2,
					"vouchers": ["IN", "OUT"],
					"identities": [{"item": "I1", "warehouse": "W"}],
					"multi_item_vouchers": ["OUT"],
				}
			)
		self.assertEqual(out["planner_status"], READY_GLOBAL_WAREHOUSE_SOLVER)
		self.assertTrue(out["eligible"])
		self.assertIn("OUT", out.get("proposed_times") or {})

	def test_cycle_detected_for_opposite_transfer(self):
		from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver import (
			_constraint_cycle,
		)

		self.assertEqual(_constraint_cycle([("A", "B"), ("B", "A")]), ("A", "B"))
		self.assertIsNone(_constraint_cycle([("A", "B"), ("B", "C")]))

	def test_micro_oscillation_detected(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver._live_time",
			return_value=datetime(2026, 8, 23, 13, 0, 6),
		):
			self.assertTrue(
				_is_micro_oscillation({"OUT": datetime(2026, 8, 23, 13, 0, 8)})
			)
			self.assertFalse(
				_is_micro_oscillation({"OUT": datetime(2026, 8, 23, 13, 1, 0)})
			)


if __name__ == "__main__":
	unittest.main()
