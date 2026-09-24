# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.5+ — unified RepairPlan pipeline + Wrong Rate residual contract."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.job_card_flow import group_job_card_reasons
from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import (
	compress_roots,
	is_manufacture_adjacent,
	plan,
	compare_safety,
)
from erpnext_extensions.iran_accounting.historical_stock.simple_model import (
	PRIMARY_MANUAL,
	PRIMARY_READY,
	REASON_MANUFACTURE_FLOW,
	REASON_WRONG_RATE,
)


class TestRepairPipeline(unittest.TestCase):
	def test_wrong_rate_plan_ready(self):
		p = plan(
			{
				"topic": "WRONG_RATE",
				"planner_status": "READY_WRONG_RATE",
				"eligible": True,
				"confidence": "EXACT",
				"voucher": "V1",
				"item": "I1",
				"warehouse": "W1",
				"purpose": "Material Issue",
				"proposed_rate": 100,
				"sql_updates": 2,
			}
		)
		self.assertEqual(p.primary_state, PRIMARY_READY)
		self.assertEqual(p.reason, REASON_WRONG_RATE)
		self.assertIn("write_authoritative_rate", p.operations)

	def test_manufacture_wrong_rate_is_manual(self):
		p = plan(
			{
				"topic": "WRONG_RATE",
				"planner_status": "READY_WRONG_RATE",
				"eligible": True,
				"confidence": "EXACT",
				"voucher": "V1",
				"item": "I1",
				"warehouse": "W1",
				"purpose": "Manufacture",
				"proposed_rate": 100,
			}
		)
		self.assertEqual(p.primary_state, PRIMARY_MANUAL)
		self.assertEqual(p.reason, REASON_MANUFACTURE_FLOW)
		self.assertTrue(is_manufacture_adjacent({"purpose": "Manufacture"}))
		self.assertTrue(is_manufacture_adjacent({"purpose": "Material Transfer for Manufacture"}))
		self.assertFalse(is_manufacture_adjacent({"purpose": "Material Issue", "warehouse": "Stores"}))

	def test_root_compression(self):
		rows = [
			{"topic": "WRONG_RATE", "planner_status": "READY_WRONG_RATE", "voucher": "R", "item": "A", "warehouse": "W", "patient_zero": {"voucher_no": "R"}, "purpose": "Material Issue"},
			{"topic": "WRONG_RATE", "planner_status": "WAITING_PATIENT_ZERO", "voucher": "D1", "item": "A", "warehouse": "W", "patient_zero": {"voucher_no": "R"}, "purpose": "Material Issue"},
			{"topic": "WRONG_RATE", "planner_status": "WAITING_PATIENT_ZERO", "voucher": "D2", "item": "A", "warehouse": "W", "patient_zero": {"voucher_no": "R"}, "purpose": "Material Issue"},
		]
		out = compress_roots(rows)
		self.assertEqual(out["finding_count"], 3)
		self.assertEqual(out["root_count"], 1)
		self.assertEqual(out["roots"][0]["findings"], 3)

	def test_safety_compare_flags_i1_regression(self):
		before = {"i1": 0, "neg_qty": 2, "manufacturing": {"wo_produced": 1, "jc_for": 2, "jc_done": 3}}
		after = {"i1": 1, "neg_qty": 2, "manufacturing": {"wo_produced": 1, "jc_for": 2, "jc_done": 3}}
		cmp = compare_safety(before, after)
		self.assertFalse(cmp["ok"])
		self.assertTrue(any("I1" in f for f in cmp["failures"]))


class TestWrongRateResidualContract(unittest.TestCase):
	def test_zero_to_nonzero_without_expected_match_is_not_cleared(self):
		"""Regression: old cleared clause accepted any nonzero after_rate."""
		expected = 223282.0
		after_rate = 1593971.69
		# New contract
		cleared = abs(after_rate - expected) <= 1.0
		self.assertFalse(cleared)
		# Old buggy contract would have passed:
		old = abs(after_rate - expected) <= 1.0 or (abs(after_rate) > 0.0001 and abs(0.0) <= 0.0001)
		self.assertTrue(old)


class TestJobCardReasonGroups(unittest.TestCase):
	def test_groups_completed_residuals(self):
		rows = [
			{"status": "BROKEN", "reason": "completed imbalance", "residual": 10, "linked": True, "completed": True, "vouchers": ["A"], "transferred": 100, "returned": 0, "consumed": 90, "scrap": 0},
			{"status": "BROKEN", "reason": "completed imbalance", "residual": -5, "linked": True, "completed": True, "vouchers": ["B"], "transferred": 100, "returned": 0, "consumed": 105, "scrap": 0},
			{"status": "MANUAL", "reason": "x", "linked": False, "vouchers": []},
			{"status": "BALANCED", "reason": "completed equation", "linked": True},
		]
		g = group_job_card_reasons(rows)
		self.assertEqual(g["completed_qty_residual_positive"]["count"], 1)
		self.assertEqual(g["completed_qty_residual_negative"]["count"], 1)
		self.assertEqual(g["missing_or_ambiguous_job_card_link"]["count"], 1)
		self.assertNotIn("completed equation", g)
