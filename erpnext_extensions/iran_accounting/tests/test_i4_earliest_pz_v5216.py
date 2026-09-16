# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.16 I4 earliest-leftover patient zero unit tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import I4_READY, I4_WAITING
from erpnext_extensions.iran_accounting.historical_stock.i4_repair import classify_i4_row


class TestI4EarliestLeftoverPZ(unittest.TestCase):
	def test_earliest_leftover_promotes_ready_when_sim_clears(self):
		sle = SimpleNamespace(
			name="SLE1",
			voucher_no="STE-A",
			item_code="I1",
			warehouse="W1",
			actual_qty=0,
			qty_after_transaction=0,
			stock_value=100,
			stock_value_difference=0,
			valuation_rate=0,
			incoming_rate=0,
			outgoing_rate=0,
			posting_datetime="2026-01-01 10:00:00",
			batch_no=None,
			serial_and_batch_bundle=None,
		)
		prev = SimpleNamespace(
			name="SLE0",
			voucher_no="STE-0",
			qty_after_transaction=10,
			stock_value=500,
			valuation_rate=50,
			incoming_rate=50,
			outgoing_rate=0,
			actual_qty=10,
			stock_value_difference=500,
		)

		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.find_patient_zero_identity",
				return_value={"voucher_no": "STE-OTHER", "reason": "nonzero_to_zero_incoming", "sle_name": "X"},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair._earliest_i4_leftover",
				return_value=sle,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.frappe.db.get_value",
				return_value=sle,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair._fetch_previous",
				return_value=prev,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.sle_poison_reason",
				side_effect=lambda r: None if r is prev else "qty_after_zero_nonzero_value",
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.preview_i4_replay",
				return_value={"rows": 1, "sql_updates": 2},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair._simulation_clears_patient_zero",
				return_value=True,
			),
		):
			out = classify_i4_row("I1", "W1", sle_name="SLE1")
		self.assertEqual(out["i4_status"], I4_READY)
		self.assertTrue(out["eligible"])
		self.assertEqual(out["patient_zero"]["voucher_no"], "STE-A")

	def test_downstream_leftover_waits_on_earliest_i4(self):
		downstream = SimpleNamespace(
			name="SLE2",
			voucher_no="STE-B",
			item_code="I1",
			warehouse="W1",
			actual_qty=0,
			qty_after_transaction=0,
			stock_value=50,
			stock_value_difference=0,
			valuation_rate=0,
			incoming_rate=0,
			outgoing_rate=0,
			posting_datetime="2026-01-02 10:00:00",
			batch_no=None,
			serial_and_batch_bundle=None,
		)
		earliest = SimpleNamespace(
			name="SLE1",
			voucher_no="STE-A",
			item_code="I1",
			warehouse="W1",
			actual_qty=0,
			qty_after_transaction=0,
			stock_value=100,
			stock_value_difference=0,
			valuation_rate=0,
			incoming_rate=0,
			outgoing_rate=0,
			posting_datetime="2026-01-01 10:00:00",
			batch_no=None,
			serial_and_batch_bundle=None,
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.find_patient_zero_identity",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair._earliest_i4_leftover",
				return_value=earliest,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.frappe.db.get_value",
				return_value=downstream,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair._fetch_previous",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.sle_poison_reason",
				return_value="qty_after_zero_nonzero_value",
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.preview_i4_replay",
				return_value={"rows": 2, "sql_updates": 3},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair._simulation_clears_patient_zero",
				return_value=True,
			),
		):
			out = classify_i4_row("I1", "W1", sle_name="SLE2")
		self.assertEqual(out["i4_status"], I4_WAITING)
		self.assertEqual(out["patient_zero"]["voucher_no"], "STE-A")


if __name__ == "__main__":
	unittest.main()
