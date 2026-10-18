# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1: CASE A — refuse absorb of negative previous into positive outbound SVD (I3).

MAT-STE-2026-40942 / item 13200244 failed with:
  actual_qty=-1, stock_value_difference=+95984263
because mid-chain RIV persisted ~−95.9M at qty=1 and absorb did
  effective_movement = −value_before.
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt


class TestCaseA40942I3AbsorbGuard(unittest.TestCase):
	def test_absorb_skips_negative_prev_that_would_i3(self):
		from erpnext_extensions.iran_accounting.domain.historical_valuation_residual import (
			apply_zero_qty_movement_contract,
		)

		sle = frappe._dict(
			name="MAT-SLE-2026-308219",
			item_code="13200244",
			warehouse="انبار پایکار خط تولید اسپاد فارمد",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-40942",
			voucher_detail_no="c5ujjhs8ma",
			company="اسپاد فارمد",
			actual_qty=-1.0,
			qty_after_transaction=0.0,
			stock_value=0.0,
			# Reconstructed from poisoned previous: value_before ≈ −95.9M
			stock_value_difference=0.0,
			outgoing_rate=376000.0,
			valuation_rate=376000.0,
			incoming_rate=0.0,
		)
		# Present negative previous via stock_value so contract sees it.
		sle.stock_value = -95984263.0
		sle.stock_value_difference = 0.0

		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"is_final_zero_qty_consume_state",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"classify_zero_qty_ghost",
			return_value={
				"disposition": "ABSORB_INTO_MOVEMENT",
				"ghost_amount": -95984263.0,
				"category": "HISTORICAL_RESIDUAL",
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"queue_pending_zero_qty_residual"
		):
			apply_zero_qty_movement_contract(
				sle,
				movement=-376000.0,  # native/SED outbound
				rate=376000.0,
				ccy="IRR",
				purpose="Material Transfer for Manufacture",
				row=None,
			)

		# Must NOT become +95984263 (I3). Keep caller movement (negative).
		self.assertLess(flt(sle.stock_value_difference), 0)
		self.assertEqual(flt(sle.stock_value), 0.0)

	def test_lookup_absorb_rejects_negative_db_prev(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = frappe._dict(
			name="MAT-SLE-2026-308219",
			item_code="13200244",
			warehouse="WH",
			voucher_no="MAT-STE-2026-40942",
			company="اسپاد فارمد",
			actual_qty=-1.0,
			qty_after_transaction=0.0,
			# Vanilla reconstructed non-negative previous layer:
			# value_before = 0 - (-58983128) = 58983128
			stock_value=0.0,
			stock_value_difference=-58983128.0,
			outgoing_rate=58983128.0,
			valuation_rate=58983128.0,
		)
		row = frappe._dict(
			set_basic_rate_manually=0,
			allow_zero_valuation_rate=0,
			basic_rate=58983128,
			additional_cost=0,
			landed_cost_voucher_amount=0,
		)
		captured = {}

		def fake_apply(sle_arg, **kwargs):
			captured["value_before"] = flt(sle_arg.stock_value)
			captured["movement"] = kwargs.get("movement")
			sle_arg.stock_value_difference = -flt(sle_arg.stock_value)
			sle_arg.stock_value = 0.0

		with mock.patch.object(ses, "_lookup_previous_stock_value", return_value=-95984263.0), mock.patch(
			"frappe.db.get_value", return_value="Material Transfer for Manufacture"
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard."
			"is_final_zero_qty_consume_state",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"classify_zero_qty_ghost",
			return_value={
				"disposition": "ABSORB_INTO_MOVEMENT",
				"ghost_amount": 1.0,
				"category": "HISTORICAL_RESIDUAL",
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual."
			"apply_zero_qty_movement_contract",
			side_effect=fake_apply,
		):
			ses._absorb_native_zero_qty_historical_residual(sle, row, "IRR")

		# Must present reconstructed 58983128, not poisoned −95.9M.
		self.assertEqual(captured["value_before"], 58983128.0)
		self.assertEqual(captured["movement"], -58983128.0)

	def test_document_authority_reconstructs_negative_remaining_layer(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		# value_before = 187146099 (vanilla sv - svd identity with svd=0 pre-stamp)
		sle = frappe._dict(
			stock_value=187146099.0,
			stock_value_difference=0.0,
			qty_after_transaction=1.0,
			actual_qty=-753.0,
			outgoing_rate=376000.0,
			incoming_rate=0.0,
			valuation_rate=376000.0,
			voucher_detail_no=None,
		)
		# SED movement would poison: 187146099 + (-283128000) < 0
		ses._apply_document_authority_movement(
			sle,
			movement=-283128000.0,
			rate=376000.0,
			ccy="IRR",
			purpose="Manufacture",
			row=None,
		)
		# Reconstruct: expense only available layer; leave qty=1 at sv=0.
		self.assertEqual(flt(sle.stock_value), 0.0)
		self.assertEqual(flt(sle.stock_value_difference), -187146099.0)
		self.assertLess(flt(sle.stock_value_difference), 0)


if __name__ == "__main__":
	unittest.main()
