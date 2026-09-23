# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B — PO dry-run / apply preflight consistency tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.stock_posting_order.repair import (
	STATUS_BLOCKED,
	dry_run_selected,
	preflight_apply_eligibility,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D


class TestPODryApplyPreflightV530(unittest.TestCase):
	def _ready_row(self, **extra):
		base = {
			"outbound_document": "STE-OUT",
			"inbound_document": "STE-IN",
			"item": "ITEM-1",
			"warehouse": "WH-1",
			"batch": None,
			"planner_status": "READY_WAREHOUSE_REPLAY",
			"eligible": True,
			"confidence": "EXACT",
			"sql_updates": 3,
			"proposed_outbound_time": "2026-01-02 10:00:00",
			"current_outbound_time": "2026-01-01 10:00:00",
			"current_inbound_time": "2026-01-02 12:00:00",
			"proposed_inbound_time": "2026-01-02 12:00:00",
			"min_qty_before": -10,
			"min_qty_after": 0,
			"opening_qty": 0,
			"valuation_impact": "QUANTITY-ONLY",
			"dependency_signature": "sig",
		}
		base.update(extra)
		return base

	def test_preflight_blocks_still_negative_sim(self):
		row = self._ready_row()
		fake_sle = MagicMock(actual_qty=-10, voucher_no="STE-OUT")
		fake_sle.name = "SLE1"
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.planner.assert_ready",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair._row_moves",
				return_value=[
					{"document": "STE-OUT", "new": "2026-01-02 10:00:00", "old": "2026-01-01 10:00:00"}
				],
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair._identity_window",
				return_value=[fake_sle],
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair._identity_window_for_moves",
				return_value=[fake_sle],
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair.simulate_running",
				return_value={"min_qty": D("-1387"), "final_qty": D("-1387")},
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair._voucher_doctype",
				return_value="Stock Entry",
			),
		):
			pre = preflight_apply_eligibility(row)
		self.assertFalse(pre["ok"])
		self.assertIn("still negative", pre["reason"])

	def test_dry_run_surfaces_apply_blocker(self):
		row = self._ready_row()
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.planner.attach_plan_many",
				return_value=[dict(row)],
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair.preflight_apply_eligibility",
				return_value={
					"ok": False,
					"reason": "Replay failed: proposed order still negative (min -1387.0)",
					"sim_min_qty": D("-1387"),
					"gl_blockers": [],
				},
			),
		):
			from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

			self.assertIn("READY_WAREHOUSE_REPLAY", READY_STATUSES)
			res = dry_run_selected([row])
		self.assertEqual(len(res["applied"]), 0)
		self.assertEqual(len(res["blocked"]), 1)
		self.assertIn("still negative", res["blocked"][0]["error"])
		self.assertEqual(res["blocked"][0].get("status"), STATUS_BLOCKED)

	def test_dry_run_passes_when_preflight_ok(self):
		row = self._ready_row()
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.planner.attach_plan_many",
				return_value=[dict(row)],
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.repair.preflight_apply_eligibility",
				return_value={"ok": True, "reason": None, "sim_min_qty": D("0"), "gl_blockers": []},
			),
		):
			res = dry_run_selected([row])
		self.assertEqual(len(res["applied"]), 1)
		self.assertEqual(len(res["blocked"]), 0)


if __name__ == "__main__":
	unittest.main()
