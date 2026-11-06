# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.4 — SABE qty×rate must not re-poison Case D (qty>0, stock_value<0).

HRC-00009 / RIV fc2nf25o2f / MAT-SLE-2026-210109:
  previous layer 632_210_600, SABE outgoing 1_497_227 × 526 = 787_541_402
  → stock_value -155_330_802 with qty_after 9274 (NEGATIVE_STOCK_VALUE).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.utils import flt


class TestSabeCaseDAfterAlignV574(unittest.TestCase):
	def test_sabe_align_overshoot_reconstructs_to_zero_remaining(self):
		from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
			apply_integer_sabe_policy_to_sle,
		)

		qty, available, rate = 526.0, 632_210_600.0, 1_497_227.0
		poison_svd = -(rate * qty)
		sle = frappe._dict(
			company="C",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-29941",
			voucher_detail_no="sed-210109",
			actual_qty=-qty,
			qty_after_transaction=9274.0,
			# Pre-SABE Case D reconstruct already left sv=0 / movement=-available.
			stock_value=0.0,
			stock_value_difference=-available,
			outgoing_rate=available / qty,
			valuation_rate=0.0,
			serial_and_batch_bundle="SABB-210109",
			is_cancelled=0,
		)
		sabe_rows = [
			frappe._dict(
				name="sabe1",
				qty=-qty,
				incoming_rate=rate,
				outgoing_rate=rate,
				stock_value_difference=poison_svd,
				batch_no="1250-13100023-240507",
			)
		]
		row = frappe._dict(
			amount=abs(poison_svd),
			basic_amount=abs(poison_svd),
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			basic_rate=rate,
			qty=qty,
			transfer_qty=qty,
		)
		fake_db = SimpleNamespace(
			get_value=lambda *a, **k: (
				"Material Transfer for Manufacture"
				if a and a[0] == "Stock Entry"
				else (row if a and a[0] == "Stock Entry Detail" else None)
			),
			set_value=mock.Mock(),
		)
		engine = SimpleNamespace(
			wh_data=SimpleNamespace(stock_value=0.0, valuation_rate=0.0, prev_stock_value=0.0)
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_monetary_rate",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.frappe.get_all",
			return_value=sabe_rows,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.should_seed_sabe_from_document_sle",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			# Start from a prior Case D repair (sv=0). SABE qty×rate re-poisons;
			# post-align Case D must restore sv=0 / movement=-available.
			out = apply_integer_sabe_policy_to_sle(sle, engine=engine, align_sle=True)

		self.assertTrue(out.get("case_d_reconstructed"))
		self.assertEqual(flt(sle.stock_value), 0.0)
		self.assertEqual(flt(sle.qty_after_transaction), 9274.0)
		self.assertEqual(flt(sle.stock_value_difference), -available)
		self.assertGreaterEqual(flt(sle.stock_value), -0.5)
		self.assertEqual(flt(engine.wh_data.stock_value), 0.0)

	def test_helper_idempotent_when_already_nonnegative(self):
		from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
			_apply_case_d_after_sabe_align,
		)

		sle = frappe._dict(
			stock_value=0.0,
			stock_value_difference=-100.0,
			qty_after_transaction=10.0,
			actual_qty=-5.0,
			outgoing_rate=20.0,
			valuation_rate=0.0,
			voucher_detail_no=None,
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db"
		):
			self.assertFalse(_apply_case_d_after_sabe_align(sle, ccy="IRR"))


if __name__ == "__main__":
	unittest.main()
