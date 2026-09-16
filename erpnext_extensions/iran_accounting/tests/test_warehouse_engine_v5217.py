# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.17 unit tests for Warehouse Engine scope / validation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	READY_WAREHOUSE_REPLAY,
	SCOPE_WAREHOUSE_VALUATION,
	WAREHOUSE_REAL_SHORTAGE,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.validator import (
	validate_warehouse_simulation,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.dependency import (
	build_dependency_report,
)
from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
	SAFE_GROUP,
	classify_group,
)


class TestWarehouseValidator(unittest.TestCase):
	def _analysis(self, **over):
		base = {
			"required_replay_scope": SCOPE_WAREHOUSE_VALUATION,
			"warehouse_still_negative": False,
			"cyclic": False,
			"reason": "WAREHOUSE_MA_DEPENDENCY: 1 other-batch",
			"affected_vouchers": ["A", "B"],
			"moves": [{"document": "B", "new": "2026-01-01 00:00:01"}],
			"cross_warehouse_movements": [],
			"warehouse_qty": {"proposed_min": 0, "current_min": -10, "final_unchanged": True},
		}
		base.update(over)
		return base

	def _sim(self, **over):
		base = {
			"ok": True,
			"final_qty_unchanged": True,
			"negative_qty_vouchers": [],
			"negative_incoming_vouchers": [],
			"exploded_rate_vouchers": [],
			"idempotent": True,
			"expected_bin": {"actual_qty": 0, "stock_value": 0, "valuation_rate": 0},
			"expected_gl_impact": "selective_rebuild_if_valuation_changed",
			"downstream_affected_sle": [{"sle": "S1"}],
			"affected_batches": ["BATCH1"],
			"row_count": 5,
			"previous_voucher": "PREV",
			"opening_qty": 10,
		}
		base.update(over)
		return base

	def test_ready_when_all_gates_pass(self):
		out = validate_warehouse_simulation(self._analysis(), self._sim())
		self.assertEqual(out["planner_status"], READY_WAREHOUSE_REPLAY)
		self.assertTrue(out["eligible"])

	def test_real_shortage_when_proposed_min_negative(self):
		out = validate_warehouse_simulation(
			self._analysis(warehouse_still_negative=True, warehouse_qty={"proposed_min": -5}),
			self._sim(),
		)
		self.assertEqual(out["planner_status"], WAREHOUSE_REAL_SHORTAGE)
		self.assertFalse(out["eligible"])

	def test_dependency_report_includes_ma_edges(self):
		rep = build_dependency_report(
			{
				"item": "I",
				"warehouse": "W",
				"required_replay_scope": SCOPE_WAREHOUSE_VALUATION,
				"edges": [],
				"moving_average_dependencies": [{"voucher": "X", "fields": ["stock_value"], "batch": "B"}],
				"outbound_document": "OUT",
				"affected_vouchers": ["OUT", "X"],
				"other_batches_in_window": [],
				"other_work_orders_in_window": [],
				"cross_warehouse_movements": [],
			}
		)
		self.assertEqual(rep["by_type"].get("WAREHOUSE_MA_DEPENDENCY"), 1)

	def test_safe_group_rejects_duplicate_identity(self):
		roots = [
			{"voucher": "A", "item": "I1", "warehouse": "W1", "patient_zero": {"voucher_no": "A"}},
			{"voucher": "B", "item": "I1", "warehouse": "W1", "patient_zero": {"voucher_no": "B"}},
		]
		self.assertNotEqual(classify_group(roots), SAFE_GROUP)


class TestWarehouseScopeMapping(unittest.TestCase):
	def test_analyze_candidate_maps_warehouse_scope(self):
		from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.scope_analyzer import (
			analyze_candidate,
		)

		row = {
			"item": "30300020",
			"warehouse": "WH",
			"batch": "B1",
			"inbound_document": "IN",
			"outbound_document": "OUT",
			"moves": [],
		}
		fake_scope = {
			"smallest_safe_scope": "WAREHOUSE_VALUATION_SCOPED",
			"status": "WAREHOUSE_ESCALATION_REQUIRED",
			"escalation_required": True,
			"other_batch_rewrite_count": 1,
			"other_batch_changes": [{"voucher": "X", "batch": "B2", "fields": ["stock_value"], "sle": "S"}],
			"repair_order": ["IN", "OUT"],
			"edges": [],
			"batch_qty_ok": True,
			"warehouse_qty": {"proposed_min": 0, "final_unchanged": True},
			"warehouse_still_negative": False,
			"cyclic": False,
			"pair_end_value_equal": False,
			"reason": "MA",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.scope_analyzer.evaluate_minimal_scope",
			return_value=fake_scope,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.scope_analyzer._cross_warehouse_deps",
			return_value=[],
		):
			out = analyze_candidate(row)
		self.assertEqual(out["required_replay_scope"], SCOPE_WAREHOUSE_VALUATION)
		self.assertTrue(out["ma_forces_other_batches"])
		self.assertFalse(out["can_repair_at_batch_scope"])


if __name__ == "__main__":
	unittest.main()
