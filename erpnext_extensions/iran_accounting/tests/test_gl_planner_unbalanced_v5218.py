# Copyright (c) 2026, ERPNext Extensions contributors
"""Planner must demote GL READY when SE GL map is not postable at currency precision."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import G1_ECONOMICALLY_WRONG, G2_MISSING
from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_MANUAL,
	PLAN_READY,
	attach_plan,
)


class TestGLPlannerUnbalancedMap(unittest.TestCase):
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_expected_map_state",
		return_value={
			"nonempty": True,
			"balanced": True,
			"postable": False,
			"diff": 1.0,
			"precision": 0,
			"allowance": 0.5,
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_has_poison",
		return_value=False,
	)
	def test_g2_currency_precision_unpostable_is_manual(self, _poison, _state):
		row = {
			"topic": "GL",
			"gl_class": G2_MISSING,
			"voucher": "MAT-STE-UNBALANCED",
			"eligible": True,
		}
		planned = attach_plan(row)
		self.assertEqual(planned["planner_status"], PLAN_MANUAL)
		self.assertFalse(planned["eligible"])
		self.assertIn("not postable", (planned.get("reason") or "").lower())

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_expected_map_state",
		return_value={
			"nonempty": True,
			"balanced": True,
			"postable": False,
			"diff": 1.0,
			"precision": 0,
			"allowance": 0.5,
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_has_poison",
		return_value=False,
	)
	def test_g1_currency_precision_unpostable_is_manual(self, _poison, _state):
		row = {
			"topic": "GL",
			"gl_class": G1_ECONOMICALLY_WRONG,
			"voucher": "MAT-STE-G1-FRAC",
			"eligible": True,
		}
		planned = attach_plan(row)
		self.assertEqual(planned["planner_status"], PLAN_MANUAL)
		self.assertFalse(planned["eligible"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_expected_map_state",
		return_value={
			"nonempty": True,
			"balanced": True,
			"postable": True,
			"diff": 0.0,
			"precision": 0,
			"allowance": 0.5,
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_has_poison",
		return_value=False,
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._gl_row_count",
		return_value=0,
	)
	def test_g2_postable_expected_map_stays_ready(self, _count, _poison, _state):
		row = {
			"topic": "GL",
			"gl_class": G2_MISSING,
			"voucher": "MAT-STE-BALANCED",
			"eligible": True,
		}
		planned = attach_plan(row)
		self.assertEqual(planned["planner_status"], PLAN_READY)
		self.assertTrue(planned["eligible"])
		self.assertGreater(planned["sql_updates"], 0)


if __name__ == "__main__":
	unittest.main()
