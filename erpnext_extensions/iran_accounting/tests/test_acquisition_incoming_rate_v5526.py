# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.26 — authoritative acquisition incoming rate is not the previous average."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_svd_direction,
)
from erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic import (
	align_authoritative_acquisition_movement,
	apply_authoritative_acquisition_incoming_rate,
	apply_irr_deterministic_sle_valuation,
	carry_stock_value_into_moving_average,
)

MODULE = "erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic"


def _sle(**kwargs):
	base = {
		"company": "اسپاد فارمد دارو",
		"voucher_type": "Purchase Receipt",
		"voucher_no": "MAT-PRE-2026-01815",
		"voucher_detail_no": "g7e55p4mi0",
		"item_code": "16000297",
		"actual_qty": 5,
		"qty_after_transaction": 25,
		"stock_value": 820_588_646,
		"stock_value_difference": 230_000_000,
		"incoming_rate": 1_039_613,
		"valuation_rate": 1_039_613,
	}
	base.update(kwargs)
	return base


class TestAuthoritativeAcquisitionIncoming(unittest.TestCase):
	def _apply(self, sle, rows):
		def _get_value(doctype, name, fieldname=None, **kwargs):
			if doctype == "Company" and fieldname == "default_currency":
				return "IRR"
			key = (doctype, name)
			if key not in rows:
				return None
			row = rows[key]
			if isinstance(fieldname, (list, tuple)):
				return {field: row.get(field) for field in fieldname}
			if fieldname:
				return row.get(fieldname)
			return row

		with (
			mock.patch(f"{MODULE}.is_irr_company", return_value=True),
			mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"),
			mock.patch(f"{MODULE}.frappe.db.get_value", side_effect=_get_value),
		):
			apply_irr_deterministic_sle_valuation(sle, sle["company"])
		return sle

	def test_purchase_receipt_keeps_document_rate_not_previous_average(self):
		sle = _sle()
		rows = {
			("Purchase Receipt", "MAT-PRE-2026-01815"): {
				"is_return": 0,
				"is_internal_supplier": 0,
			},
			("Purchase Receipt Item", "g7e55p4mi0"): {
				"item_code": "16000297",
				"valuation_rate": 46_000_000,
			},
		}
		self._apply(sle, rows)
		self.assertEqual(sle["incoming_rate"], 46_000_000)
		self.assertNotEqual(sle["incoming_rate"], 29_529_432)

	def test_stock_entry_incoming_stays_balance_derived(self):
		# Previous value 590,588,646 / qty 20 = 29,529,432.
		sle = _sle(
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-1",
			voucher_detail_no="row",
			stock_value=590_588_646,
			stock_value_difference=0,
			actual_qty=5,
			qty_after_transaction=25,
			incoming_rate=1,
		)
		self._apply(sle, {})
		self.assertEqual(sle["incoming_rate"], 29_529_432)

	def test_zero_document_rate_is_not_an_acquisition_rate(self):
		sle = _sle(stock_value=590_588_646, stock_value_difference=0, incoming_rate=0)
		rows = {
			("Purchase Receipt", "MAT-PRE-2026-01815"): {
				"is_return": 0,
				"is_internal_supplier": 0,
			},
			("Purchase Receipt Item", "g7e55p4mi0"): {
				"item_code": "16000297",
				"valuation_rate": 0,
			},
		}
		self._apply(sle, rows)
		self.assertEqual(sle["incoming_rate"], 29_529_432)

	def test_return_and_unlinked_invoice_do_not_own_the_rate(self):
		sle = _sle()
		rows = {
			("Purchase Receipt", "MAT-PRE-2026-01815"): {
				"is_return": 1,
				"is_internal_supplier": 0,
			},
			("Purchase Receipt Item", "g7e55p4mi0"): {
				"item_code": "16000297",
				"valuation_rate": 46_000_000,
			},
		}
		self._apply(sle, rows)
		self.assertNotEqual(sle["incoming_rate"], 46_000_000)

	def test_i3_still_blocks_negative_incoming_movement(self):
		sle = _sle(incoming_rate=46_000_000, stock_value_difference=-322_084_466, actual_qty=5)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_svd_direction(sle)
		self.assertIn("negative stock_value_difference", str(ctx.exception))

	def test_apply_sets_rate_before_moving_average(self):
		sle = _sle(incoming_rate=1_039_613)
		rows = {
			("Purchase Receipt", "MAT-PRE-2026-01815"): {
				"is_return": 0,
				"is_internal_supplier": 0,
			},
			("Purchase Receipt Item", "g7e55p4mi0"): {
				"item_code": "16000297",
				"valuation_rate": 46_000_000,
			},
		}

		def _get_value(doctype, name, fieldname=None, **kwargs):
			if doctype == "Company":
				return "IRR"
			row = rows.get((doctype, name))
			if not row:
				return None
			if isinstance(fieldname, (list, tuple)):
				return {field: row.get(field) for field in fieldname}
			return row.get(fieldname)

		with (
			mock.patch(f"{MODULE}.is_irr_company", return_value=True),
			mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"),
			mock.patch(f"{MODULE}.frappe.db.get_value", side_effect=_get_value),
		):
			self.assertTrue(apply_authoritative_acquisition_incoming_rate(sle, sle["company"]))
		self.assertEqual(sle["incoming_rate"], 46_000_000)

	def test_align_movement_is_quantity_times_document_rate(self):
		sle = _sle(
			actual_qty=20,
			qty_after_transaction=438,
			incoming_rate=1_390_000,
			stock_value=628_020_000,
			stock_value_difference=-351_699_984,
			voucher_no="MAT-PRE-2026-00498",
			voucher_detail_no="sh0go3sio0",
			item_code="16200040",
		)
		rows = {
			("Purchase Receipt", "MAT-PRE-2026-00498"): {
				"is_return": 0,
				"is_internal_supplier": 0,
			},
			("Purchase Receipt Item", "sh0go3sio0"): {
				"item_code": "16200040",
				"valuation_rate": 2_350_000,
			},
		}

		def _get_value(doctype, name, fieldname=None, **kwargs):
			if doctype == "Company":
				return "IRR"
			row = rows.get((doctype, name))
			if not row:
				return None
			if isinstance(fieldname, (list, tuple)):
				return {field: row.get(field) for field in fieldname}
			return row.get(fieldname)

		with (
			mock.patch(f"{MODULE}.is_irr_company", return_value=True),
			mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"),
			mock.patch(f"{MODULE}.frappe.db.get_value", side_effect=_get_value),
		):
			self.assertTrue(align_authoritative_acquisition_movement(sle, sle["company"]))
		self.assertEqual(sle["incoming_rate"], 2_350_000)
		self.assertEqual(sle["stock_value_difference"], 47_000_000)

	def test_carry_reloads_balance_instead_of_stale_row_rate(self):
		class _Wh:
			qty_after_transaction = 418
			stock_value = 979_719_984
			valuation_rate = 1_390_000

		class _Engine:
			company = "اسپاد فارمد دارو"
			wh_data = _Wh()

		with mock.patch(f"{MODULE}.is_irr_company", return_value=True):
			carry_stock_value_into_moving_average(_Engine(), {})
		self.assertEqual(_Engine.wh_data.valuation_rate, 979_719_984 / 418)
		self.assertNotEqual(_Engine.wh_data.valuation_rate, 1_390_000)


if __name__ == "__main__":
	unittest.main()
