# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.8: CASE E — Manufacture SED overclaim at consume-to-zero must not break continuity.

MAT-STE-2026-36178-1 / item 30100298 / SLE MAT-SLE-2026-294684 failed HRC with:
  VALUATION_CONTINUITY expected≈−35.9T actual=0

Root cause:
  Core emptied stock_value=0 while keeping SED rate×qty SVD (−35.9T).
  ``value_before = sv − svd`` invented +35.9T; ghost≈0; NOOP left discontinuity.
  True previous SLE (same voucher prior row) had stock_value=0.
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt


class TestCaseE30100298ValuationContinuity(unittest.TestCase):
	def test_resolve_prefers_true_prev_when_core_emptied(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			name="MAT-SLE-2026-294684",
			item_code="30100298",
			warehouse="انبار پایکار خط تولید اسپاد فارمد",
			actual_qty=-24.048583,
			qty_after_transaction=0.0,
			stock_value=0.0,
			stock_value_difference=-35929217136873.0,
		)
		with mock.patch.object(ses, "_lookup_previous_stock_value", return_value=0.0):
			prev = ses._resolve_consume_to_zero_previous_stock_value(sle)
		self.assertEqual(flt(prev), 0.0)

	def test_classify_caps_sed_overclaim_when_layer_within_ceiling(self):
		from erpnext_extensions.iran_accounting.domain.historical_valuation_residual import (
			ABSORB_INTO_MOVEMENT,
			MANUFACTURE_AUTO_SA_MAX,
			classify_zero_qty_ghost,
		)

		sle = frappe._dict(
			name="MAT-SLE-2026-294684",
			item_code="30100298",
			warehouse="WH",
			company="اسپاد فارمد",
			actual_qty=-24.048583,
			qty_after_transaction=0.0,
			stock_value=0.0,
			stock_value_difference=0.0,
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"is_final_zero_qty_consume_state",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"prior_type_a_on_identity",
			return_value=None,
		):
			cl = classify_zero_qty_ghost(
				sle=sle,
				purpose="Manufacture",
				value_before=0.0,
				movement=-35929217136873.0,
				company="اسپاد فارمد",
			)
		self.assertEqual(cl["disposition"], ABSORB_INTO_MOVEMENT)
		self.assertEqual(cl["detail"], "manufacture_cap_sed_overclaim_to_available_layer")
		self.assertGreater(abs(flt(cl["ghost_amount"])), MANUFACTURE_AUTO_SA_MAX)

	def test_document_authority_consume_to_zero_clears_only_available_layer(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
			mark_sle_running_balance_processed,
		)

		sle = frappe._dict(
			name="MAT-SLE-2026-294684",
			item_code="30100298",
			warehouse="انبار پایکار خط تولید اسپاد فارمد",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-36178-1",
			voucher_detail_no="ap8qcq5nn5",
			company="اسپاد فارمد",
			actual_qty=-24.048583,
			qty_after_transaction=0.0,
			# Core emptied with poisoned SVD
			stock_value=0.0,
			stock_value_difference=-35929217136873.0,
			outgoing_rate=1494026368908.0,
			valuation_rate=1494026368908.0,
			incoming_rate=0.0,
			valuation_method="Moving Average",
		)
		mark_sle_running_balance_processed(sle)
		row = frappe._dict(
			set_basic_rate_manually=0,
			allow_zero_valuation_rate=0,
			basic_rate=1494026368908.0,
			amount=35929217136873.0,
			additional_cost=0,
			landed_cost_voucher_amount=0,
		)
		with mock.patch.object(ses, "_lookup_previous_stock_value", return_value=0.0), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle"
		) as align:
			ses._apply_document_authority_movement(
				sle,
				movement=-35929217136873.0,
				rate=1494026368908.0,
				ccy="IRR",
				purpose="Manufacture",
				row=row,
			)

		# Continuity: prev(0) + svd(0) = sv(0). Must not keep −35.9T SVD.
		self.assertEqual(flt(sle.stock_value), 0.0)
		self.assertEqual(flt(sle.stock_value_difference), 0.0)
		self.assertLessEqual(abs(flt(sle.outgoing_rate)), 0.5)
		align.assert_called()

	def test_repair_empty_warehouse_caps_discontinuous_svd(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			name="MAT-SLE-2026-294684",
			item_code="30100298",
			warehouse="WH",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-36178-1",
			voucher_detail_no="ap8qcq5nn5",
			company="اسپاد فارمد",
			actual_qty=-24.048583,
			qty_after_transaction=0.0,
			stock_value=0.0,
			stock_value_difference=-35929217136873.0,
			outgoing_rate=1494026368908.0,
			valuation_rate=1494026368908.0,
			valuation_method="Moving Average",
		)
		row = frappe._dict(
			set_basic_rate_manually=0,
			allow_zero_valuation_rate=0,
			basic_rate=1494026368908.0,
			amount=35929217136873.0,
			additional_cost=0,
			landed_cost_voucher_amount=0,
		)
		with mock.patch.object(ses, "_lookup_previous_stock_value", return_value=0.0), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle"
		):
			changed = ses._repair_empty_warehouse_valuation_continuity(
				sle, row=row, ccy="IRR", purpose="Manufacture"
			)
		self.assertTrue(changed)
		self.assertEqual(flt(sle.stock_value), 0.0)
		self.assertEqual(flt(sle.stock_value_difference), 0.0)

	def test_clear_manufacture_overclaim_outgoing_rate(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-36178-1",
			actual_qty=-24.048583,
			outgoing_rate=1494026368908.0,
		)
		engine = frappe._dict(wh_data=frappe._dict(stock_value=0.0))
		with mock.patch("frappe.db.get_value", return_value="Manufacture"):
			cleared = ses.clear_manufacture_overclaim_outgoing_rate_for_riv(sle, engine=engine)
		self.assertTrue(cleared)
		self.assertEqual(flt(sle.outgoing_rate), 0.0)

	def test_document_authority_huge_real_layer_still_fail_closed(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
			ValuationIntegrityError,
			mark_sle_running_balance_processed,
		)

		sle = frappe._dict(
			name="SLE-HUGE",
			item_code="X",
			warehouse="WH",
			voucher_type="Stock Entry",
			voucher_no="STE-X",
			voucher_detail_no=None,
			company="اسپاد فارمد",
			actual_qty=-1.0,
			qty_after_transaction=0.0,
			stock_value=0.0,
			stock_value_difference=0.0,
			outgoing_rate=0.0,
			valuation_rate=0.0,
			incoming_rate=0.0,
			valuation_method="Moving Average",
		)
		mark_sle_running_balance_processed(sle)
		# Real previous layer itself exceeds auto ceiling — must not silent-clear.
		with mock.patch.object(ses, "_lookup_previous_stock_value", return_value=2_000_000_000.0):
			with self.assertRaises(ValuationIntegrityError):
				ses._apply_document_authority_movement(
					sle,
					movement=-3_500_000_000.0,
					rate=3_500_000_000.0,
					ccy="IRR",
					purpose="Manufacture",
					row=None,
				)
