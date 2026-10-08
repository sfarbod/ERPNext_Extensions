# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.24 FIX A — leftover-MA post-RIV restore must honor receipt eligibility.

Normal RIV Completed must not replay Manufacture / MTfM / Paykar patient zeros.
Eligible historical receipt patient zeros must still stamp + downstream-replay.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
	filter_eligible_leftover_ma_patient_zeros,
	leftover_ma_receipt_eligible,
	on_repost_item_valuation_update,
	restore_leftover_ma_after_riv,
	warehouse_can_host_leftover_ma_receipt,
)

PAYKAR = "انبار پایکار خط تولید اسپاد فارمد"
STORES = "انبار approved مواد اولیه اسپاد"


class TestLeftoverMaEligibilityContract(unittest.TestCase):
	def test_mtfm_ineligible(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Transfer for Manufacture",
			warehouse=STORES,
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")

	def test_manufacture_ineligible(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Manufacture",
			warehouse=STORES,
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")

	def test_paykar_warehouse_ineligible_even_for_receipt(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse=PAYKAR,
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")
		ok_wh, reason_wh = warehouse_can_host_leftover_ma_receipt(PAYKAR)
		self.assertFalse(ok_wh)
		self.assertEqual(reason_wh, "MANUFACTURE_FLOW")

	def test_material_receipt_eligible_on_stores(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse=STORES,
		)
		self.assertTrue(ok)
		self.assertEqual(reason, "")
		ok_wh, _ = warehouse_can_host_leftover_ma_receipt(STORES)
		self.assertTrue(ok_wh)


class TestRestoreLeftoverMaAfterRivGate(unittest.TestCase):
	def test_paykar_warehouse_fast_return_no_find_no_replay(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.find_leftover_ma_patient_zeros"
			) as find,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.stamp_patient_zero_valuation_rate"
			) as stamp,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.replay_downstream_after_patient_zero"
			) as replay,
		):
			out = restore_leftover_ma_after_riv("13100023", PAYKAR)
			self.assertEqual(out["stamped"], 0)
			self.assertEqual(out["skipped"], "MANUFACTURE_FLOW")
			find.assert_not_called()
			stamp.assert_not_called()
			replay.assert_not_called()

	def test_mtfm_patient_zero_skipped_no_stamp_no_replay(self):
		row = SimpleNamespace(
			name="sle-mtfm",
			voucher_no="MAT-STE-MTFM-1",
			qty_after_transaction=10,
			stock_value=1000,
			valuation_rate=0,
			incoming_rate=0,
			stock_value_difference=0,
			actual_qty=10,
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.find_leftover_ma_patient_zeros",
				return_value=[row],
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.leftover_ma_blocked_by_manufacture_flow",
				return_value="MANUFACTURE_FLOW",
			) as blocked,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.stamp_patient_zero_valuation_rate"
			) as stamp,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.replay_downstream_after_patient_zero"
			) as replay,
		):
			out = restore_leftover_ma_after_riv("ITEM", STORES)
			self.assertEqual(out["stamped"], 0)
			self.assertEqual(out["vouchers"], [])
			self.assertEqual(len(out["skipped_rows"]), 1)
			self.assertEqual(out["skipped_rows"][0]["reason"], "MANUFACTURE_FLOW")
			blocked.assert_called_once_with("MAT-STE-MTFM-1", "ITEM", STORES)
			stamp.assert_not_called()
			replay.assert_not_called()

	def test_eligible_receipt_stamps_and_replays(self):
		row = SimpleNamespace(
			name="sle-receipt",
			voucher_no="MAT-STE-RECEIPT-1",
			qty_after_transaction=10,
			stock_value=1000,
			valuation_rate=0,
			incoming_rate=0,
			stock_value_difference=0,
			actual_qty=10,
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.find_leftover_ma_patient_zeros",
				return_value=[row],
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.leftover_ma_blocked_by_manufacture_flow",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.stamp_patient_zero_valuation_rate",
				return_value={"ok": True, "stamped": True, "valuation_rate": 100},
			) as stamp,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.replay_downstream_after_patient_zero",
				return_value={"ok": True, "replayed": True},
			) as replay,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.invalidate_stock_ledger_prepared_reports"
			) as invalidate,
		):
			out = restore_leftover_ma_after_riv("ITEM", STORES)
			self.assertEqual(out["stamped"], 1)
			self.assertEqual(out["vouchers"], ["MAT-STE-RECEIPT-1"])
			self.assertEqual(out["skipped_rows"], [])
			self.assertEqual(stamp.call_count, 2)  # stamp + re-stamp
			replay.assert_called_once_with("ITEM", STORES, "MAT-STE-RECEIPT-1")
			invalidate.assert_called_once_with("ITEM")

	def test_no_patient_zeros_no_expensive_traversal(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.find_leftover_ma_patient_zeros",
				return_value=[],
			) as find,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.stamp_patient_zero_valuation_rate"
			) as stamp,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.replay_downstream_after_patient_zero"
			) as replay,
		):
			out = restore_leftover_ma_after_riv("ITEM", STORES)
			self.assertEqual(out["stamped"], 0)
			find.assert_called_once()
			stamp.assert_not_called()
			replay.assert_not_called()

	def test_filter_helper_splits_eligible_and_skipped(self):
		rows = [
			SimpleNamespace(voucher_no="MR-1"),
			SimpleNamespace(voucher_no="MTFM-1"),
		]

		def _blocked(voucher, item, warehouse):
			return None if voucher == "MR-1" else "MANUFACTURE_FLOW"

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.leftover_ma_blocked_by_manufacture_flow",
			side_effect=_blocked,
		):
			out = filter_eligible_leftover_ma_patient_zeros("ITEM", STORES, rows=rows)
		self.assertEqual([r.voucher_no for r in out["eligible"]], ["MR-1"])
		self.assertEqual(out["skipped"][0]["voucher"], "MTFM-1")


class TestLeftoverMaRivHookReentry(unittest.TestCase):
	def test_reentry_flag_prevents_recursion(self):
		import frappe

		doc = SimpleNamespace(status="Completed", item_code="ITEM", warehouse=STORES)
		frappe.flags.leftover_ma_riv_hook = True
		try:
			with patch(
				"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.restore_leftover_ma_after_riv"
			) as restore:
				on_repost_item_valuation_update(doc)
				restore.assert_not_called()
		finally:
			frappe.flags.leftover_ma_riv_hook = False

	def test_completed_still_calls_restore(self):
		doc = SimpleNamespace(status="Completed", item_code="ITEM", warehouse=STORES)
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.leftover_ma.restore_leftover_ma_after_riv"
		) as restore:
			on_repost_item_valuation_update(doc)
			restore.assert_called_once_with("ITEM", STORES)
