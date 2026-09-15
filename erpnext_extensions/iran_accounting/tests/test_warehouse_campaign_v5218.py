# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.18 unit tests — Warehouse dependency graph + campaign optimizer."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	READY_WAREHOUSE_CAMPAIGN,
	SCOPE_WAREHOUSE_VALUATION,
	WAREHOUSE_REAL_SHORTAGE,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.graph import (
	EDGE_WAREHOUSE_MA,
	build_identity_pair_graph,
	build_warehouse_universe_graph,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
	merge_proposed_times,
	optimize_identity_campaign,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.validator import (
	validate_warehouse_simulation,
)


class TestWarehouseDependencyGraph(unittest.TestCase):
	def test_multi_pair_requires_joint_campaign(self):
		pairs = [
			{
				"inbound_document": "IN1",
				"outbound_document": "OUT1",
				"batch": "B1",
				"current_outbound_time": "2026-04-12 10:00:00",
			},
			{
				"inbound_document": "IN2",
				"outbound_document": "OUT2",
				"batch": "B2",
				"current_outbound_time": "2026-04-12 11:00:00",
			},
		]
		g = build_identity_pair_graph(pairs, item="I", warehouse="W")
		self.assertTrue(g["requires_joint_campaign"])
		self.assertEqual(g["n_pairs"], 2)
		self.assertEqual(len(g["multi_root_campaigns"]), 1)
		self.assertGreaterEqual(g["by_edge_type"].get(EDGE_WAREHOUSE_MA, 0), 1)
		self.assertEqual(g["dependency_depth"], 1)

	def test_shared_batch_is_bridge(self):
		pairs = [
			{"inbound_document": "IN1", "outbound_document": "OUT1", "batch": "SHARED"},
			{"inbound_document": "IN2", "outbound_document": "OUT2", "batch": "SHARED"},
		]
		g = build_identity_pair_graph(pairs, item="I", warehouse="W")
		self.assertTrue(any(s.get("kind") == "batch" for s in g["shared_roots"]))
		self.assertTrue(any(b.get("type") == "BATCH" for b in g["bridge_nodes"]))

	def test_universe_groups_by_identity(self):
		rows = [
			{"item": "A", "warehouse": "W1", "inbound_document": "I1", "outbound_document": "O1"},
			{"item": "A", "warehouse": "W1", "inbound_document": "I2", "outbound_document": "O2"},
			{"item": "B", "warehouse": "W2", "inbound_document": "I3", "outbound_document": "O3"},
		]
		u = build_warehouse_universe_graph(rows)
		self.assertEqual(u["n_identities"], 2)
		self.assertEqual(u["n_multi_root"], 1)
		self.assertEqual(u["n_independent"], 1)


class TestWarehouseCampaignOptimizer(unittest.TestCase):
	def test_merge_synthesizes_midnight_without_moves(self):
		pairs = [
			{
				"optimizer_status": "MIDNIGHT_REVIEW",
				"inbound_document": "IN",
				"outbound_document": "OUT",
				"current_inbound_time": "2026-04-15 18:03:41",
				"current_outbound_time": "2026-04-14 18:03:50",
				"proposed_inbound_time": "2026-04-15 18:03:41",
				"moves": [],
			}
		]
		proposed, moves, from_dt, notes = merge_proposed_times(pairs)
		self.assertIn("OUT", proposed)
		self.assertTrue(proposed["OUT"] > proposed.get("IN", proposed["OUT"]))
		self.assertTrue(any(m.get("document") == "OUT" for m in moves))
		self.assertIsNotNone(from_dt)

	def test_optimize_promotes_ready_campaign_when_sim_and_multi_clear(self):
		pairs = [
			{
				"item": "I",
				"warehouse": "W",
				"optimizer_status": "CROSS_TIME_REPAIRABLE",
				"inbound_document": "IN1",
				"outbound_document": "OUT1",
				"current_inbound_time": "2026-04-12 18:13:11",
				"current_outbound_time": "2026-04-12 18:06:20",
				"proposed_inbound_time": "2026-04-12 18:13:11",
				"moves": [{"document": "OUT1", "old": "2026-04-12 18:06:20", "new": "2026-04-12 18:13:12"}],
			},
			{
				"item": "I",
				"warehouse": "W",
				"optimizer_status": "CROSS_TIME_REPAIRABLE",
				"inbound_document": "IN2",
				"outbound_document": "OUT2",
				"current_inbound_time": "2026-04-12 18:13:16",
				"current_outbound_time": "2026-04-12 18:05:05",
				"proposed_inbound_time": "2026-04-12 18:13:16",
				"moves": [{"document": "OUT2", "old": "2026-04-12 18:05:05", "new": "2026-04-12 18:13:17"}],
			},
		]
		sim_ok = {
			"ok": True,
			"final_qty_unchanged": True,
			"final_qty_current": 0,
			"final_qty_proposed": 0,
			"negative_qty_vouchers": [],
			"negative_incoming_vouchers": [],
			"exploded_rate_vouchers": [],
			"idempotent": True,
			"expected_bin": {"actual_qty": 0, "stock_value": 0, "valuation_rate": 0},
			"expected_gl_impact": "selective_rebuild_if_valuation_changed",
			"downstream_affected_sle": [],
			"affected_batches": [],
			"row_count": 10,
			"previous_voucher": "PREV",
			"opening_qty": 0,
		}
		multi_ok = {
			"all_clear": True,
			"n_identities": 2,
			"n_failed": 0,
			"identities": [],
			"reason": None,
			"blocker_status": None,
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer.simulate_warehouse_replay",
			return_value=sim_ok,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer.expand_cross_identity_times",
			return_value={
				"ok": True,
				"rounds": 1,
				"proposed_times": {
					"OUT1": datetime(2026, 4, 12, 18, 13, 12),
					"OUT2": datetime(2026, 4, 12, 18, 13, 17),
					"IN1": datetime(2026, 4, 12, 18, 13, 11),
					"IN2": datetime(2026, 4, 12, 18, 13, 16),
				},
				"notes": [],
				"round_logs": [],
				"verification": multi_ok,
			},
		):
			out = optimize_identity_campaign(pairs, item="I", warehouse="W")
		self.assertEqual(out["planner_status"], READY_WAREHOUSE_CAMPAIGN)
		self.assertTrue(out["eligible"])
		self.assertEqual(out["group_class"], "SAFE_GROUP")
		self.assertEqual(out["n_pairs"], 2)

	def test_validator_still_flags_real_shortage(self):
		out = validate_warehouse_simulation(
			{
				"required_replay_scope": SCOPE_WAREHOUSE_VALUATION,
				"warehouse_still_negative": True,
				"cyclic": False,
				"warehouse_qty": {"proposed_min": -5},
				"affected_vouchers": [],
				"moves": [],
				"cross_warehouse_movements": [],
			},
			{
				"ok": True,
				"final_qty_unchanged": True,
				"negative_qty_vouchers": ["X"],
				"negative_incoming_vouchers": [],
				"exploded_rate_vouchers": [],
				"idempotent": True,
				"expected_bin": {"actual_qty": 0},
				"expected_gl_impact": "x",
				"downstream_affected_sle": [],
				"opening_qty": 0,
				"previous_voucher": "P",
			},
		)
		self.assertEqual(out["planner_status"], WAREHOUSE_REAL_SHORTAGE)
		self.assertFalse(out["eligible"])


if __name__ == "__main__":
	unittest.main()
