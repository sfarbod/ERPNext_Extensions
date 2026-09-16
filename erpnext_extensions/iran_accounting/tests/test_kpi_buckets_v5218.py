# Copyright (c) 2026, ERPNext Extensions contributors
"""Wrong Rate / KPI bucket membership invariants (v5.2.18)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import (
	assert_wrong_rate_invariants,
	count_wrong_rate_buckets,
	filter_rows_by_kpi_bucket,
	wrong_rate_bucket,
	row_matches_kpi_bucket,
)


def _row(**kw):
	base = {
		"topic": "WRONG_RATE",
		"repair_required": True,
		"sql_updates": 1,
		"rate_source": "reconstructed",
		"confidence": "EXACT",
	}
	base.update(kw)
	return base


class TestWrongRateKpiBuckets(unittest.TestCase):
	def test_complete_never_in_active_cards(self):
		row = _row(
			planner_status="RATE_REPAIR_COMPLETE",
			repair_required=False,
			sql_updates=0,
			rate_source="already-valued",
			required_action="repair wrong rate X first (manual — not auto)",
		)
		self.assertEqual(wrong_rate_bucket(row), "complete")
		self.assertFalse(row_matches_kpi_bucket(row, "wrong_rate_ready"))
		self.assertFalse(row_matches_kpi_bucket(row, "wrong_rate_waiting"))
		self.assertFalse(row_matches_kpi_bucket(row, "wrong_rate_manual"))
		self.assertFalse(row_matches_kpi_bucket(row, "wrong_rate_active"))
		self.assertTrue(row_matches_kpi_bucket(row, "wrong_rate_complete"))
		self.assertEqual(assert_wrong_rate_invariants(row), [])

	def test_ready_waiting_manual_ambiguous(self):
		ready = _row(planner_status="READY_WRONG_RATE", sql_updates=2, repair_required=True)
		waiting = _row(planner_status="WAITING_PATIENT_ZERO", sql_updates=0, repair_required=True)
		manual = _row(planner_status="RATE_MANUAL", sql_updates=0, repair_required=True)
		ambiguous = _row(planner_status="RATE_AMBIGUOUS", sql_updates=0, repair_required=True)
		self.assertEqual(wrong_rate_bucket(ready), "ready")
		self.assertEqual(wrong_rate_bucket(waiting), "waiting")
		self.assertEqual(wrong_rate_bucket(manual), "manual")
		self.assertEqual(wrong_rate_bucket(ambiguous), "manual")
		self.assertTrue(row_matches_kpi_bucket(ready, "wrong_rate_ready"))
		self.assertTrue(row_matches_kpi_bucket(waiting, "wrong_rate_waiting"))
		self.assertTrue(row_matches_kpi_bucket(manual, "wrong_rate_manual"))
		self.assertTrue(row_matches_kpi_bucket(ambiguous, "wrong_rate_manual"))

	def test_filter_manual_excludes_complete(self):
		rows = [
			_row(planner_status="RATE_MANUAL"),
			_row(
				planner_status="RATE_REPAIR_COMPLETE",
				repair_required=False,
				sql_updates=0,
				rate_source="already-valued",
			),
			_row(planner_status="READY_WRONG_RATE", sql_updates=1),
		]
		manual = filter_rows_by_kpi_bucket(rows, "wrong_rate_manual")
		self.assertEqual(len(manual), 1)
		self.assertEqual(manual[0]["planner_status"], "RATE_MANUAL")
		counts = count_wrong_rate_buckets(rows)
		self.assertEqual(counts["manual"], 1)
		self.assertEqual(counts["complete"], 1)
		self.assertEqual(counts["ready"], 1)
		self.assertEqual(counts["active"], 2)  # ready + manual

	def test_substring_manual_in_required_action_does_not_classify_complete(self):
		"""Reproduce UI bug: client search 'MANUAL' matched COMPLETE via required_action text."""
		row = _row(
			planner_status="RATE_REPAIR_COMPLETE",
			repair_required=False,
			sql_updates=0,
			rate_source="already-valued",
			required_action="repair wrong rate MAT-STE-1 first (manual — not auto)",
		)
		blob = str(row).lower()
		self.assertIn("manual", blob)  # would fool substring search
		self.assertEqual(wrong_rate_bucket(row), "complete")
		self.assertFalse(row_matches_kpi_bucket(row, "wrong_rate_manual"))


if __name__ == "__main__":
	unittest.main()
