# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Phase 3 Assisted Recovery."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	ASSISTED_READY,
	NO_EVIDENCE,
	OPERATOR_DECISION,
	REAL_STOCK_SHORTAGE,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.confidence import (
	score_rate_candidates,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.ambiguity import (
	resolve_rate_ambiguity,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.impact_rank import (
	estimate_unlock_impact,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.wizard import (
	build_decision_card,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.riv_assist import (
	refine_riv_root_cause,
)


class TestConfidenceScoring(unittest.TestCase):
	def test_unique_cluster_is_ready_candidate(self):
		out = score_rate_candidates({"previous_healthy_sle": 100.0, "batch_inward": 100.0})
		self.assertEqual(out["outcome"], "READY_CANDIDATE")
		self.assertEqual(out["confidence"], 1.0)

	def test_high_confidence_assisted(self):
		# Strong sources agree; weak version disagrees → noise cluster dropped → unique READY
		out = score_rate_candidates(
			{
				"previous_healthy_sle": 1000.0,
				"batch_inward": 1000.0,
				"purchase_receipt": 1000.0,
				"version": 1.0,
			},
			threshold=0.95,
		)
		self.assertIn(out["outcome"], ("READY_CANDIDATE", ASSISTED_READY))
		self.assertGreaterEqual(out["confidence"], 0.95)
		self.assertAlmostEqual(out["recommended"]["rate"], 1000.0)

	def test_empty_sources_no_evidence(self):
		out = score_rate_candidates({})
		self.assertEqual(out["outcome"], NO_EVIDENCE)

	def test_balanced_disagreement_operator(self):
		out = score_rate_candidates(
			{"version": 10.0, "manufacture_pool": 99.0},
			threshold=0.95,
		)
		self.assertEqual(out["outcome"], OPERATOR_DECISION)


class TestAmbiguityResolver(unittest.TestCase):
	def test_purchase_matches_batch_promotes_ready(self):
		evidence = {
			"rate_sources": {"version": 5.0, "purchase_receipt": 50.0},
			"purchase_receipts": [{"rate": 50.0}],
			"batch_inward": {"rate": 50.0},
			"previous_sle": {},
		}
		out = resolve_rate_ambiguity({"voucher": "X"}, evidence)
		self.assertTrue(out["resolved"])
		self.assertEqual(out["promote_to"], "READY")
		self.assertAlmostEqual(out["expected"], 50.0)


class TestImpactRank(unittest.TestCase):
	def test_fanout_scores_pz_highest(self):
		root = {"voucher": "PZ1", "item": "A", "warehouse": "W", "topic": "WRONG_RATE"}
		universe = [
			{"voucher": "D1", "planner_status": "WAITING_PATIENT_ZERO", "patient_zero": "PZ1", "item": "A", "warehouse": "W"},
			{"voucher": "D2", "planner_status": "WAITING_PATIENT_ZERO", "patient_zero": "PZ1", "item": "A", "warehouse": "W"},
			{"voucher": "D3", "planner_status": "READY", "patient_zero": "PZ1"},
		]
		imp = estimate_unlock_impact(root, universe)
		self.assertEqual(imp["waiting_same_pz"], 2)
		self.assertGreaterEqual(imp["score"], 20)


class TestWizard(unittest.TestCase):
	def test_card_has_required_fields(self):
		card = build_decision_card(
			{"voucher": "V1", "item": "I", "topic": "WRONG_RATE", "planner_status": "RATE_AMBIGUOUS", "current": 0},
			evidence={"rate_sources": {"version": 1.0}},
			resolution={"promote_to": ASSISTED_READY, "expected": 10.0, "source": "version", "confidence": 0.97, "reason": "test", "scored": {"options": [{"rate": 10.0, "confidence": 0.97, "sources": ["version"], "top_source": "version"}]}},
			impact={"unlocked_estimate": 5, "score": 50},
		)
		for key in (
			"what_is_wrong",
			"why_automatic_repair_stopped",
			"possible_choices",
			"suggested_choice",
			"confidence",
			"estimated_downstream_repairs_unlocked",
		):
			self.assertIn(key, card)
		self.assertGreaterEqual(len(card["possible_choices"]), 1)


class TestRivAssist(unittest.TestCase):
	def test_negative_maps_waiting_negative(self):
		out = refine_riv_root_cause({"riv_status": "NEGATIVE_STOCK", "error_head": "negative stock"})
		self.assertEqual(out["waiting_bucket"], "WAITING_NEGATIVE")
		self.assertFalse(out["safe_to_retry"])

	def test_unknown_not_retried(self):
		out = refine_riv_root_cause({"riv_status": "UNKNOWN"})
		self.assertEqual(out["waiting_bucket"], "UNKNOWN")
		self.assertFalse(out["auto_retry"])


if __name__ == "__main__":
	unittest.main()
