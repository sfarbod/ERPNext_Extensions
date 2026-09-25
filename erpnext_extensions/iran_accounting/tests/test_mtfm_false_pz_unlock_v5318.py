# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.18 — MTfM EXACT unlock past false Patient Zero; warehouse-scoped PO."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.planner import (
	READY_STATUSES,
	PLAN_WAITING_PATIENT_ZERO,
	evaluate_row,
)
from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
	STATUS_CROSS_ITEM_CONFLICT,
	_is_mtfm_order_dependency,
	_proposal_payload,
)


class TestMtfmFalsePatientZeroUnlock(unittest.TestCase):
	def test_waiting_patient_zero_promotes_when_mtfm_exact(self):
		row = {
			"topic": "WRONG_RATE",
			"voucher": "MAT-STE-DOWN",
			"item": "ITEM1",
			"warehouse": "WH",
			"purpose": "Material Transfer for Manufacture",
			"confidence": "EXACT",
			"status": "RECONSTRUCTABLE",
			"eligible": False,
			"source_of_truth": "batch_inward_sabb_rate",
			"patient_zero": {"voucher_no": "MAT-STE-UP"},
			"blocked_because": "upstream_patient_zero",
			"sql_updates": 4,
			"expected": 100.0,
			"proposed_rate": 100.0,
			"current_rate": 0.0,
		}

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.repair_pipeline."
			"_material_transfer_for_manufacture_exact",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._write_counts",
			return_value={"se": 1, "sle": 2, "sabb": 1, "sbe": 0, "bin": 0, "sql": 4, "replay": 1, "rebuild": 0},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._rate_patient_cleared",
			return_value=False,
		):
			d = evaluate_row(row)

		self.assertIn(d["planner_status"], READY_STATUSES)
		self.assertTrue(d["eligible"])
		self.assertEqual(d.get("dependency"), "mtfm_exact_false_patient_zero_unlock")

	def test_waiting_stays_when_mtfm_exact_false(self):
		row = {
			"topic": "WRONG_RATE",
			"voucher": "MAT-STE-DOWN",
			"item": "ITEM1",
			"warehouse": "WH",
			"purpose": "Material Transfer for Manufacture",
			"confidence": "EXACT",
			"status": "RECONSTRUCTABLE",
			"eligible": False,
			"source_of_truth": "batch_inward_sabb_rate",
			"patient_zero": {"voucher_no": "MAT-STE-UP"},
			"blocked_because": "upstream_patient_zero",
			"sql_updates": 4,
			"expected": 100.0,
			"proposed_rate": 100.0,
			"current_rate": 0.0,
		}

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.repair_pipeline."
			"_material_transfer_for_manufacture_exact",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._rate_patient_cleared",
			return_value=False,
		):
			d = evaluate_row(row)

		self.assertEqual(d["planner_status"], PLAN_WAITING_PATIENT_ZERO)
		self.assertFalse(d["eligible"])


class TestMtfmOrderWarehouseFallback(unittest.TestCase):
	def test_sfg_then_consume_is_mtfm_order(self):
		self.assertTrue(
			_is_mtfm_order_dependency(
				"same_work_order+same_batch+sfg_then_consume",
				{"purpose": "Manufacture"},
				{"purpose": "Material Transfer for Manufacture"},
			)
		)

	def test_proposal_payload_preserves_times_on_cross_item(self):
		from datetime import datetime

		base = {"detection": "CROSS_TIME"}
		proposal = {
			"moves": [{"document": "OUT", "seconds": 1}],
			"seconds_shifted": 1,
			"proposed_outbound": datetime(2026, 4, 19, 18, 2, 16),
			"proposed_inbound": datetime(2026, 4, 19, 18, 2, 15),
			"docs_changed": 1,
			"current": {"min_qty": -10, "final_qty": 0},
			"proposed": {"min_qty": 0, "final_qty": 0},
		}
		out = _proposal_payload(
			base,
			proposal,
			status=STATUS_CROSS_ITEM_CONFLICT,
			confidence="EXACT",
			reason="sfg_then_consume",
			eligible=False,
		)
		self.assertEqual(out["status"], STATUS_CROSS_ITEM_CONFLICT)
		self.assertTrue(out["proposed_outbound"])
		self.assertTrue(out["proposed_inbound"])
		self.assertEqual(out["moves"][0]["document"], "OUT")


if __name__ == "__main__":
	unittest.main()
