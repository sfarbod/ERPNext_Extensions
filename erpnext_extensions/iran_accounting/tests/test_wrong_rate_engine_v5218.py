# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Wrong Rate Engine Phase 2 (v5.2.18)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import pick_reconstruction
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine import (
	AMBIGUOUS_RATE,
	DOWNSTREAM_RATE_SYMPTOM,
	MANUAL_RATE,
	PATIENT_ZERO_RATE,
	RATE_AMBIGUOUS,
	RATE_MANUAL,
	READY_WRONG_RATE,
	WAITING_PATIENT_ZERO,
	WRONG_OUTGOING_RATE,
)
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
	classify_wrong_rate_row,
)
from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_READY_WRONG_RATE,
	READY_STATUSES,
	evaluate_row,
)


class TestWrongRateReconstructionSources(unittest.TestCase):
	def test_exact_previous_sle_corroborated(self):
		out = pick_reconstruction(
			{"previous_healthy_sle": 100.0, "batch_inward": 100.0},
		)
		self.assertEqual(out["confidence"], "EXACT")
		self.assertAlmostEqual(out["expected"], 100.0)

	def test_version_alone_is_likely(self):
		out = pick_reconstruction({"version": 55.0})
		self.assertEqual(out["confidence"], "LIKELY")
		self.assertEqual(out["source"], "version")

	def test_transfer_source_exact_alone(self):
		out = pick_reconstruction({"transfer_source": 12.5})
		self.assertEqual(out["confidence"], "EXACT")
		self.assertEqual(out["source"], "transfer_source")

	def test_batch_inward_exact_alone(self):
		out = pick_reconstruction({"batch_inward": 9.0})
		self.assertEqual(out["confidence"], "EXACT")
		self.assertEqual(out["source"], "batch_inward")

	def test_previous_healthy_sle_exact_alone(self):
		out = pick_reconstruction({"previous_healthy_sle": 88.0})
		self.assertEqual(out["confidence"], "EXACT")
		self.assertEqual(out["source"], "previous_healthy_sle")

	def test_manufacture_pool_still_likely(self):
		out = pick_reconstruction({"manufacture_pool": 12.0})
		self.assertEqual(out["confidence"], "LIKELY")

	def test_implied_svd_exact(self):
		out = pick_reconstruction({"implied_svd": 42.0})
		self.assertEqual(out["confidence"], "EXACT")
		self.assertEqual(out["source"], "implied_svd")

	def test_conflicting_sources_ambiguous(self):
		out = pick_reconstruction({"version": 10.0, "previous_healthy_sle": 99.0})
		self.assertEqual(out["confidence"], "AMBIGUOUS")

	def test_bin_key_stripped(self):
		out = pick_reconstruction({"bin": 999.0, "implied_svd": 50.0})
		self.assertNotIn("bin", out["sources_tried"])
		self.assertEqual(out["source"], "implied_svd")
		self.assertAlmostEqual(out["expected"], 50.0)

	def test_empty_sources_manual(self):
		out = pick_reconstruction({})
		self.assertEqual(out["confidence"], "MANUAL")
		self.assertEqual(out["source"], "manual")


class TestWrongRateClassifier(unittest.TestCase):
	def test_ready_outgoing_implied_svd(self):
		row = classify_wrong_rate_row(
			{
				"topic": "WRONG_RATE",
				"flags": ["WRONG_OUTGOING_RATE"],
				"mismatch_class": "WRONG_OUTGOING_RATE",
				"confidence": "EXACT",
				"eligible": True,
				"planner_status": "READY_WRONG_RATE",
				"status": "RECONSTRUCTABLE",
				"current": 0,
				"expected": 120.0,
				"source": "implied_svd",
				"voucher": "MAT-STE-1",
				"surface": "SLE",
			}
		)
		self.assertEqual(row["rate_status"], READY_WRONG_RATE)
		self.assertEqual(row["rate_bucket"], WRONG_OUTGOING_RATE)
		self.assertEqual(row["expected_source"], "implied_svd")
		self.assertAlmostEqual(row["rate_difference"], 120.0)

	def test_bin_fallback_forced_manual(self):
		row = classify_wrong_rate_row(
			{
				"confidence": "EXACT",
				"eligible": True,
				"planner_status": "READY",
				"source": "bin",
				"current": 1,
				"expected": 2,
				"voucher": "X",
			}
		)
		self.assertEqual(row["rate_status"], RATE_MANUAL)
		self.assertEqual(row["rate_bucket"], MANUAL_RATE)
		self.assertFalse(row["eligible"])

	def test_patient_zero_waiting_downstream(self):
		row = classify_wrong_rate_row(
			{
				"flags": ["WRONG_OUTGOING_RATE"],
				"confidence": "EXACT",
				"eligible": False,
				"planner_status": "WAITING_PATIENT_ZERO",
				"voucher": "DOWNSTREAM",
				"patient_zero": {"voucher_no": "PATIENT_ZERO"},
				"current": 0,
				"expected": 10,
				"source": "implied_svd",
			}
		)
		self.assertEqual(row["rate_status"], WAITING_PATIENT_ZERO)
		self.assertEqual(row["rate_bucket"], DOWNSTREAM_RATE_SYMPTOM)

	def test_ambiguous_bucket(self):
		row = classify_wrong_rate_row({"confidence": "AMBIGUOUS", "planner_status": "AMBIGUOUS"})
		self.assertEqual(row["rate_status"], RATE_AMBIGUOUS)
		self.assertEqual(row["rate_bucket"], AMBIGUOUS_RATE)

	def test_patient_zero_ready_bucket(self):
		row = classify_wrong_rate_row(
			{
				"flags": ["WRONG_VS_RECONSTRUCTED_SOURCE"],
				"mismatch_class": "WRONG_VS_RECONSTRUCTED_SOURCE",
				"confidence": "EXACT",
				"eligible": True,
				"planner_status": "READY_WRONG_RATE",
				"status": "RECONSTRUCTABLE",
				"voucher": "PZ",
				"patient_zero": {"voucher_no": "PZ"},
				"current": 1,
				"expected": 2,
				"source": "previous_healthy_sle",
			}
		)
		self.assertEqual(row["rate_bucket"], PATIENT_ZERO_RATE)


class TestWrongRatePlannerStates(unittest.TestCase):
	def test_ready_wrong_rate_in_ready_statuses(self):
		self.assertIn(PLAN_READY_WRONG_RATE, READY_STATUSES)

	def test_evaluate_emits_ready_wrong_rate(self):
		from unittest.mock import patch

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._write_counts",
			return_value={"se": 1, "sle": 1, "sabb": 0, "sbe": 0, "bin": 0, "sql": 2},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._rate_poison_hit",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._rate_poison_reason",
			return_value=None,
		):
			decision = evaluate_row(
				{
					"topic": "WRONG_RATE",
					"confidence": "EXACT",
					"status": "RECONSTRUCTABLE",
					"eligible": True,
					"voucher": "MAT-STE-TEST",
					"item": "ITEM",
					"warehouse": "WH",
					"flags": ["WRONG_OUTGOING_RATE"],
					"proposed_rate": 10,
					"surface": "SLE",
				}
			)
		self.assertEqual(decision["planner_status"], PLAN_READY_WRONG_RATE)
		self.assertTrue(decision["eligible"])

	def test_no_generic_blocked_for_wrong_rate_ineligible(self):
		decision = evaluate_row(
			{
				"topic": "WRONG_RATE",
				"confidence": "EXACT",
				"status": "SOMETHING_ELSE",
				"eligible": False,
				"voucher": "X",
				"flags": ["WRONG_OUTGOING_RATE"],
			}
		)
		self.assertNotEqual(decision["planner_status"], "BLOCKED")


class TestPatientZeroUnlock(unittest.TestCase):
	def test_already_valued_is_rate_repair_complete(self):
		decision = evaluate_row(
			{
				"topic": "WRONG_RATE",
				"confidence": "EXACT",
				"status": "RATE_REBUILD_COMPLETE",
				"source": "already_valued",
				"source_of_truth": "already_valued",
				"eligible": False,
				"voucher": "PZ-1",
				"current": 100.0,
				"expected": 100.0,
				"current_rate": 100.0,
				"proposed_rate": 100.0,
				"flags": ["WRONG_AMOUNT"],
			}
		)
		self.assertEqual(decision["planner_status"], "RATE_REPAIR_COMPLETE")

	def test_waiting_cleared_when_pz_already_valued(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

		result = stamp_scan_result(
			{
				"rows": [
					{
						"topic": "WRONG_RATE",
						"confidence": "EXACT",
						"status": "RATE_REBUILD_COMPLETE",
						"source": "already_valued",
						"source_of_truth": "already_valued",
						"eligible": False,
						"voucher": "PZ-1",
						"current": 100.0,
						"expected": 100.0,
						"current_rate": 100.0,
						"proposed_rate": 100.0,
						"flags": ["WRONG_AMOUNT"],
						"item": "I",
						"warehouse": "W",
					},
					{
						"topic": "WRONG_RATE",
						"confidence": "EXACT",
						"status": "DEPENDENCY_REPAIR_REQUIRED",
						"eligible": False,
						"voucher": "DEP-1",
						"patient_zero": "PZ-1",
						"current": 0.0,
						"expected": 50.0,
						"current_rate": 0.0,
						"proposed_rate": 50.0,
						"flags": ["ZERO_BASIC_RATE"],
						"item": "I",
						"warehouse": "W",
						"surface": "SE",
					},
				]
			}
		)
		by_v = {r["voucher"]: r for r in result["rows"]}
		self.assertEqual(by_v["PZ-1"]["planner_status"], "RATE_REPAIR_COMPLETE")
		self.assertEqual(by_v["DEP-1"]["planner_status"], PLAN_READY_WRONG_RATE)
		self.assertTrue(by_v["DEP-1"]["eligible"])

	def test_circular_earliest_wins(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

		result = stamp_scan_result(
			{
				"rows": [
					{
						"topic": "WRONG_RATE",
						"confidence": "EXACT",
						"status": "DEPENDENCY_REPAIR_REQUIRED",
						"eligible": False,
						"voucher": "A-EARLY",
						"patient_zero": "B-LATE",
						"posting_date": "2026-01-01",
						"posting_time": "10:00:00",
						"current": 0.0,
						"expected": 10.0,
						"proposed_rate": 10.0,
						"current_rate": 0.0,
						"flags": ["ZERO_BASIC_RATE"],
						"item": "I",
						"warehouse": "W",
						"surface": "SE",
					},
					{
						"topic": "WRONG_RATE",
						"confidence": "EXACT",
						"status": "DEPENDENCY_REPAIR_REQUIRED",
						"eligible": False,
						"voucher": "B-LATE",
						"patient_zero": "A-EARLY",
						"posting_date": "2026-01-02",
						"posting_time": "10:00:00",
						"current": 0.0,
						"expected": 20.0,
						"proposed_rate": 20.0,
						"current_rate": 0.0,
						"flags": ["ZERO_BASIC_RATE"],
						"item": "I",
						"warehouse": "W",
						"surface": "SE",
					},
				]
			}
		)
		by_v = {r["voucher"]: r for r in result["rows"]}
		self.assertEqual(by_v["A-EARLY"]["planner_status"], PLAN_READY_WRONG_RATE)
		self.assertEqual(by_v["B-LATE"]["planner_status"], "WAITING_PATIENT_ZERO")


if __name__ == "__main__":
	unittest.main()
