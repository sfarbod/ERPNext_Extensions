# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — clear WAITING when external patient-zero already has healthy rate."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import STATUS_DEPENDENCY_REPAIR_REQUIRED
from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_READY_WRONG_RATE,
	PLAN_WAITING_PATIENT_ZERO,
	_external_patient_rate_healthy,
	_rate_patient_cleared,
	evaluate_row,
)

PLANNER = "erpnext_extensions.iran_accounting.historical_stock.planner"


class TestExternalPatientZeroClear(unittest.TestCase):
	def test_reco_matching_rate_clears(self):
		row = {
			"item": "16100066",
			"warehouse": "WH-A",
			"expected": 5072466.0,
			"proposed_rate": 5072466.0,
		}
		cache = {}
		sle_rows = [
			{
				"actual_qty": 0.0,
				"incoming_rate": 5072466.0,
				"valuation_rate": 5072466.0,
				"stock_value_difference": 100.0,
			}
		]
		with patch(f"{PLANNER}._voucher_type_of", return_value="Stock Reconciliation"), patch(
			f"{PLANNER}._sle_rates_for_voucher_item", return_value=sle_rows
		):
			self.assertTrue(_external_patient_rate_healthy("MAT-RECO-1", cache, row=row))
			self.assertTrue(_rate_patient_cleared("MAT-RECO-1", cache, row=row))

	def test_pre_zero_rate_does_not_clear(self):
		row = {
			"item": "15010336",
			"warehouse": "WH-A",
			"expected": 7225000.0,
		}
		cache = {}
		with patch(f"{PLANNER}._voucher_type_of", return_value="Purchase Receipt"), patch(
			f"{PLANNER}._sle_rates_for_voucher_item",
			return_value=[
				{
					"actual_qty": 4.0,
					"incoming_rate": 0.0,
					"valuation_rate": 0.0,
					"stock_value_difference": 0.0,
				}
			],
		):
			self.assertFalse(_external_patient_rate_healthy("MAT-PRE-1", cache, row=row))
			self.assertFalse(_rate_patient_cleared("MAT-PRE-1", cache, row=row))

	def test_stock_entry_patient_not_cleared_via_external(self):
		row = {"item": "X", "warehouse": "WH", "expected": 10.0}
		cache = {}
		with patch(f"{PLANNER}._voucher_type_of", return_value="Stock Entry"):
			self.assertFalse(_rate_patient_cleared("MAT-STE-1", cache, row=row))

	def test_evaluate_row_promotes_after_healthy_reco_pz(self):
		row = {
			"topic": "WRONG_RATE",
			"voucher": "MAT-STE-DEP",
			"item": "16100066",
			"warehouse": "WH-A",
			"confidence": "EXACT",
			"status": STATUS_DEPENDENCY_REPAIR_REQUIRED,
			"eligible": True,
			"expected": 5072466.0,
			"proposed_rate": 5072466.0,
			"current": 0.0,
			"current_rate": 0.0,
			"source": "previous_healthy_sle",
			"source_of_truth": "previous_healthy_sle",
			"patient_zero": {"voucher_no": "MAT-RECO-HEALTHY"},
			"sql_updates": 4,
		}
		sle_rows = [
			{
				"actual_qty": 0.0,
				"incoming_rate": 5072466.0,
				"valuation_rate": 5072466.0,
				"stock_value_difference": 1.0,
			}
		]
		with patch(f"{PLANNER}._voucher_type_of", return_value="Stock Reconciliation"), patch(
			f"{PLANNER}._sle_rates_for_voucher_item", return_value=sle_rows
		), patch(
			f"{PLANNER}._write_counts",
			return_value={"sql": 4, "sle": 1, "se": 1, "sabb": 1, "sbe": 1, "bin": 0},
		), patch(f"{PLANNER}._rate_poison_hit", return_value=None), patch(
			f"{PLANNER}._rate_poison_reason", return_value=None
		):
			out = evaluate_row(row, cache={})
		self.assertEqual(out.get("planner_status"), PLAN_READY_WRONG_RATE)
		self.assertTrue(out.get("eligible"))
		self.assertNotEqual(out.get("planner_status"), PLAN_WAITING_PATIENT_ZERO)


if __name__ == "__main__":
	unittest.main()
