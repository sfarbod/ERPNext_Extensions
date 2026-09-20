# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Phase 5 MANUAL reason + foreign-PZ waiting reclass."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import wrong_rate_bucket
from erpnext_extensions.iran_accounting.historical_stock.manual_reason import (
	MANUAL_DOWNSTREAM_SYMPTOM,
	MANUAL_TRANSFER_PROPAGATION,
	classify_wrong_manual_reason,
	summarize_manual_groups,
)


class TestManualReason(unittest.TestCase):
	def test_transfer_with_foreign_pz_is_downstream_lane(self):
		row = {
			"topic": "WRONG_RATE",
			"purpose": "Material Transfer",
			"voucher": "STE-DOWN",
			"patient_zero": {"voucher_no": "STE-ROOT"},
			"planner_status": "RATE_AMBIGUOUS",
			"confidence": "AMBIGUOUS",
		}
		classify_wrong_manual_reason(row)
		self.assertEqual(row["manual_reason"], MANUAL_TRANSFER_PROPAGATION)
		self.assertEqual(row["manual_lane"], "WAITING_UPSTREAM")

	def test_kpi_bucket_foreign_pz_counts_as_waiting(self):
		row = {
			"voucher": "STE-DOWN",
			"patient_zero": {"voucher_no": "STE-ROOT"},
			"planner_status": "RATE_AMBIGUOUS",
		}
		self.assertEqual(wrong_rate_bucket(row), "waiting")

	def test_manual_groups_root_count(self):
		rows = [
			classify_wrong_manual_reason(
				{
					"purpose": "Material Transfer",
					"voucher": f"V{i}",
					"item": "A",
					"warehouse": "W",
					"patient_zero": {"voucher_no": "ROOT"},
					"planner_status": "RATE_AMBIGUOUS",
				}
			)
			for i in range(5)
		]
		s = summarize_manual_groups(rows)
		self.assertEqual(s["unique_patient_zero"], 1)
		self.assertEqual(s["root_chains"], 1)
		self.assertEqual(s["unique_item_warehouse"], 1)


if __name__ == "__main__":
	unittest.main()
