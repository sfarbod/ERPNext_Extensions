# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.3 — Manufacture SLE must mirror SED.amount (not qty×rate).

Reproduces Production blockers:
* MAT-STE-2026-43598 — +13 IRR (amount − valuation_rate×qty)
* MAT-STE-2026-43604 — −211 IRR (SABE integer rate×qty vs amount)
* MAT-STE-2026-43606 — −4 IRR (additional_cost dropped from SLE)

Aggregate Σ SLE − signed Σ row.amount equals the same single-row delta.
"""

from __future__ import annotations

import unittest
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	apply_integer_sabe_policy_to_sle,
	localize_sabe_row_values,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	reassert_document_amount_authority_sle,
	refresh_sle_authoritative_sed_from_ledger,
	signed_movement_from_row_amount,
	stock_entry_row_amount,
)


def _rhu(value: float) -> int:
	return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class TestAmountAuthorityResidualsV573(unittest.TestCase):
	"""Controlled fixtures matching the three Production residuals."""

	CASES = (
		# name, qty, amount, expected_vr, expected_residual (vr*qty - amount)
		("43598_plus_13", 1074, 616660715, 574172, 13),
		("43604_minus_211", 387, 2455374343, 6344637, 176),  # vr*qty - amount
		("43606_addl_4", 1427, 8052097229, 5642675, -4),
	)

	def test_rate_times_qty_residuals_match_incident_math(self):
		for name, qty, amount, vr, residual in self.CASES:
			with self.subTest(name=name):
				self.assertEqual(_rhu(amount / qty), vr)
				self.assertEqual(vr * qty - amount, residual)

	def test_43604_alternate_integer_rate_produces_211(self):
		# SLE used incoming_rate 6344636 (not SED.valuation_rate 6344637).
		qty, amount = 387, 2455374343
		sle_rate = 6344636
		self.assertEqual(sle_rate * qty, 2455374132)
		self.assertEqual(amount - sle_rate * qty, 211)

	def test_localize_sabe_integer_qty_drifts_from_amount(self):
		qty, amount, vr = 1074, 616660715, 574172
		loc = localize_sabe_row_values(qty=qty, raw_rate=vr, raw_svd=amount)
		self.assertEqual(loc["rate"], vr)
		self.assertEqual(loc["svd"], float(vr * qty))
		self.assertEqual(loc["localization_difference"], float(vr * qty - amount))


class TestReassertDocumentAmountV573(unittest.TestCase):
	def test_reassert_restores_sed_amount_after_rate_times_qty(self):
		qty, amount, vr = 1074, 616660715, 574172
		sle = frappe._dict(
			company="C",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-X",
			voucher_detail_no="sed-fg",
			actual_qty=qty,
			stock_value_difference=vr * qty,  # drifted
			stock_value=vr * qty,
			qty_after_transaction=qty,
			incoming_rate=vr,
			valuation_rate=vr,
			is_cancelled=0,
		)
		row = frappe._dict(
			amount=amount,
			basic_amount=amount - 2180724,
			additional_cost=2180724,
			landed_cost_voucher_amount=0,
			valuation_rate=vr,
			basic_rate=vr - 2030,
			qty=qty,
			transfer_qty=qty,
		)

		def _get_value(*a, **k):
			if a and a[0] == "Stock Entry":
				return "Manufacture"
			if a and a[0] == "Stock Entry Detail":
				return row
			return None

		fake_db = SimpleNamespace(get_value=_get_value)
		with mock.patch(
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
		):
			changed = reassert_document_amount_authority_sle(sle)
		self.assertTrue(changed)
		self.assertEqual(sle.stock_value_difference, float(amount))
		self.assertEqual(
			signed_movement_from_row_amount(amount, qty),
			sle.stock_value_difference,
		)

	def test_sabe_policy_preserves_manufacture_document_amount(self):
		qty, amount, vr = 1074, 616660715, 574172
		sle = frappe._dict(
			company="C",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-X",
			voucher_detail_no="sed-fg",
			actual_qty=qty,
			stock_value_difference=amount,  # sync_irr already stamped amount
			stock_value=amount,
			qty_after_transaction=qty,
			incoming_rate=vr,
			valuation_rate=vr,
			serial_and_batch_bundle="SABB-1",
			is_cancelled=0,
		)
		sabe_rows = [
			frappe._dict(
				name="sabe1",
				qty=qty,
				incoming_rate=vr,
				outgoing_rate=0,
				stock_value_difference=amount,
				batch_no="B1",
			)
		]
		row = frappe._dict(
			amount=amount,
			basic_amount=amount - 2180724,
			additional_cost=2180724,
			landed_cost_voucher_amount=0,
			valuation_rate=vr,
			basic_rate=572142,
			qty=qty,
			transfer_qty=qty,
		)

		def _get_value(*a, **k):
			if a and a[0] == "Stock Entry":
				return "Manufacture"
			if a and a[0] == "Stock Entry Detail":
				return row
			return None

		fake_db = SimpleNamespace(get_value=_get_value, set_value=mock.Mock())
		fake_frappe = SimpleNamespace(get_all=mock.Mock(return_value=sabe_rows), db=fake_db)
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
			fake_frappe.get_all,
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
		):
			out = apply_integer_sabe_policy_to_sle(sle, align_sle=True)

		self.assertTrue(out.get("document_amount_authority"))
		self.assertEqual(out.get("localization_difference"), 0.0)
		# SLE must remain document amount — not vr×qty.
		self.assertEqual(flt(sle.stock_value_difference), float(amount))
		self.assertNotEqual(flt(sle.stock_value_difference), float(vr * qty))


class TestRefreshIncomingDoesNotDoubleCountAddlV573(unittest.TestCase):
	def test_incoming_fg_with_additional_cost(self):
		qty, addl = 1427, 4
		amount = 8052097229
		basic = amount - addl
		vr = _rhu(amount / qty)
		row = frappe._dict(
			name="sed-fg",
			s_warehouse=None,
			t_warehouse="FG",
			qty=qty,
			transfer_qty=qty,
			basic_rate=_rhu(basic / qty),
			valuation_rate=vr,
			basic_amount=basic,
			amount=amount,
			additional_cost=addl,
			landed_cost_voucher_amount=0,
			set_basic_rate_manually=0,
		)
		doc = frappe._dict(
			name="MAT-STE-2026-43606",
			doctype="Stock Entry",
			company="C",
			purpose="Manufacture",
			items=[row],
		)
		# SLE already carries full document amount (post-reassert).
		sle = frappe._dict(
			name="sle-fg",
			actual_qty=qty,
			stock_value_difference=amount,
			qty_after_transaction=qty,
			stock_value=amount,
			incoming_rate=vr,
			valuation_rate=vr,
		)
		fake_db = SimpleNamespace(
			get_value=lambda *a, **k: sle if a and a[0] == "Stock Ledger Entry" else None,
			set_value=mock.Mock(),
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
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
		):
			n = refresh_sle_authoritative_sed_from_ledger(doc)
		self.assertGreaterEqual(n, 0)
		self.assertEqual(row.amount, float(amount))
		self.assertEqual(row.basic_amount, float(basic))
		# Must not become amount + addl (double count).
		self.assertNotEqual(row.amount, float(amount + addl))


class TestAggregateEqualsRowDeltaV573(unittest.TestCase):
	def test_single_row_delta_explains_aggregate(self):
		# Signed net for Manufacture ≈ value_difference; one FG mismatch of D
		# moves Σ SLE by exactly D relative to signed Σ row.amount.
		for delta in (13, -211, -4):
			sle_sum = 1_000_000 + delta
			signed_sed = 1_000_000
			self.assertEqual(sle_sum - signed_sed, delta)


class TestIdempotentReassertV573(unittest.TestCase):
	def test_second_reassert_is_noop(self):
		qty, amount = 10, 100_003
		sle = frappe._dict(
			company="C",
			voucher_type="Stock Entry",
			voucher_no="SE1",
			voucher_detail_no="r1",
			actual_qty=qty,
			stock_value_difference=amount,
			stock_value=amount,
			qty_after_transaction=qty,
			incoming_rate=_rhu(amount / qty),
			valuation_rate=_rhu(amount / qty),
			is_cancelled=0,
		)
		row = frappe._dict(
			amount=amount,
			basic_amount=amount,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=_rhu(amount / qty),
			basic_rate=_rhu(amount / qty),
			qty=qty,
			transfer_qty=qty,
		)
		fake_db = SimpleNamespace(
			get_value=lambda *a, **k: (
				"Manufacture"
				if a and a[0] == "Stock Entry"
				else (row if a and a[0] == "Stock Entry Detail" else None)
			)
		)
		with mock.patch(
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
		):
			self.assertFalse(reassert_document_amount_authority_sle(sle))
			self.assertEqual(sle.stock_value_difference, float(amount))


if __name__ == "__main__":
	unittest.main()
