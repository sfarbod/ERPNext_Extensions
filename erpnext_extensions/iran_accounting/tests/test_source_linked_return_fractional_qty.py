# Copyright (c) 2026, ERPNext Extensions contributors
"""Source-linked return × fractional qty: rate authority vs IRR amount lattice.

Primary case (MAT-STE-2026-43017):
  source rate R = 121055292, qty = 0.19
  exact = 23000505.48 → ROUND_HALF_UP amount = 23000505
  ROUND_HALF_UP(amount / qty) = 121055289  (≠ R)

No integer amount A satisfies ROUND_HALF_UP(A / 0.19) == R. Amount bumping
therefore cannot reconcile source-rate authority with the ordinary
valuation_rate == ROUND(amount/qty) composition rule. Class A residual under
rate-first amount is already 0 — there is nothing to post to Stock Adjustment.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.core.rounding import (
	rate_qty_amount_residual,
	round_row_amount,
)
from erpnext_extensions.iran_accounting.domain.currency import (
	amount_rate_qty_residual,
	integer_valuation_rate_from_amount,
)
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	align_stock_entry_item_amounts,
	is_source_linked_consumption_return,
	source_linked_return_keeps_rate_authoritative,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	_assert_row_composition,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_prevention import (
	assert_source_linked_return_valuation,
)

SOURCE_RATE = 121_055_292
FRAC_QTY = 0.19
RATE_FIRST_AMOUNT = 23_000_505  # ROUND_HALF_UP(0.19 × 121055292)
DERIVED_VR = 121_055_289  # ROUND_HALF_UP(23000505 / 0.19)

QRA = "erpnext_extensions.iran_accounting.domain.qty_rate_amount"
PREV = "erpnext_extensions.iran_accounting.domain.stock_entry_prevention"
CONTRACT = "erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("s_warehouse", None)
		self.__dict__.setdefault("t_warehouse", None)
		self.__dict__.setdefault("idx", 1)
		self.__dict__.setdefault("name", "row1")
		self.__dict__.setdefault("item_code", "17400002")
		self.__dict__.setdefault("batch_no", "5577-17400002-0000647585")
		self.__dict__.update(fields)
		if "transfer_qty" not in self.__dict__ and "qty" in self.__dict__:
			self.transfer_qty = self.qty

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Material Receipt")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("name", "MAT-STE-RET")
		self.__dict__.setdefault("custom_is_returned", 0)
		self.__dict__.setdefault("custom_reference_no", None)
		self.__dict__.setdefault("items", [])
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


@contextmanager
def _align_ctx():
	with ExitStack() as stack:
		stack.enter_context(mock.patch(f"{QRA}.rounding.is_irr_company", return_value=True))
		stack.enter_context(mock.patch(f"{QRA}.rounding.get_company_currency", return_value="IRR"))
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_output_contract.protect_closed_row_amounts",
				return_value=False,
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_output_contract.polish_closed_plan_after_align",
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.row_is_component_scrap_fg_pool_residual",
				return_value=False,
			)
		)
		yield


@contextmanager
def _composition_ctx():
	with ExitStack() as stack:
		stack.enter_context(mock.patch(f"{CONTRACT}.get_company_currency", return_value="IRR"))
		stack.enter_context(mock.patch(f"{CONTRACT}.get_currency_precision", return_value=0))
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				return_value="IRR",
			)
		)
		yield


class TestAmountLatticeAndResiduals(unittest.TestCase):
	"""Pure arithmetic: amount adjustment cannot hit the source rate."""

	def test_rate_first_amount_and_derived_vr_for_primary_case(self):
		exact = Decimal(str(FRAC_QTY)) * Decimal(SOURCE_RATE)
		self.assertEqual(exact, Decimal("23000505.48"))
		amount = int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
		self.assertEqual(amount, RATE_FIRST_AMOUNT)
		self.assertEqual(round_row_amount(FRAC_QTY, SOURCE_RATE, 0), RATE_FIRST_AMOUNT)
		self.assertEqual(
			integer_valuation_rate_from_amount(RATE_FIRST_AMOUNT, FRAC_QTY, "IRR"),
			float(DERIVED_VR),
		)

	def test_no_integer_amount_yields_source_rate_via_amount_over_qty(self):
		hits = []
		for amount in range(RATE_FIRST_AMOUNT - 100, RATE_FIRST_AMOUNT + 100):
			derived = (Decimal(amount) / Decimal(str(FRAC_QTY))).quantize(
				Decimal("1"), rounding=ROUND_HALF_UP
			)
			if derived == Decimal(SOURCE_RATE):
				hits.append(amount)
		self.assertEqual(
			hits,
			[],
			"amount lattice must not contain source rate for qty=0.19",
		)

	def test_class_a_residual_zero_at_rate_first_amount(self):
		# residual = amount − ROUND(qty × rate); rate-first amount → 0
		self.assertEqual(
			rate_qty_amount_residual(RATE_FIRST_AMOUNT, FRAC_QTY, SOURCE_RATE, 0),
			0,
		)
		self.assertEqual(
			amount_rate_qty_residual(RATE_FIRST_AMOUNT, FRAC_QTY, SOURCE_RATE, "IRR"),
			0.0,
		)

	def test_positive_rounding_residual_when_amount_bumped(self):
		# Inventing +1 IRR on amount creates a Class A residual — not a free lunch.
		self.assertEqual(
			rate_qty_amount_residual(RATE_FIRST_AMOUNT + 1, FRAC_QTY, SOURCE_RATE, 0),
			1,
		)
		bumped_vr = integer_valuation_rate_from_amount(RATE_FIRST_AMOUNT + 1, FRAC_QTY, "IRR")
		self.assertEqual(bumped_vr, 121_055_295.0)
		self.assertNotEqual(bumped_vr, float(SOURCE_RATE))

	def test_negative_rounding_residual_when_amount_cut(self):
		self.assertEqual(
			rate_qty_amount_residual(RATE_FIRST_AMOUNT - 1, FRAC_QTY, SOURCE_RATE, 0),
			-1,
		)
		cut_vr = integer_valuation_rate_from_amount(RATE_FIRST_AMOUNT - 1, FRAC_QTY, "IRR")
		self.assertEqual(cut_vr, 121_055_284.0)
		self.assertNotEqual(cut_vr, float(SOURCE_RATE))

	def test_exact_integer_qty_has_no_lattice_gap(self):
		qty = 1.0
		amount = round_row_amount(qty, SOURCE_RATE, 0)
		self.assertEqual(amount, SOURCE_RATE)
		self.assertEqual(
			integer_valuation_rate_from_amount(amount, qty, "IRR"),
			float(SOURCE_RATE),
		)
		self.assertEqual(rate_qty_amount_residual(amount, qty, SOURCE_RATE, 0), 0)


class TestAlignAndReturnGuards(unittest.TestCase):
	def test_align_preserves_source_rate_on_fractional_return(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=DERIVED_VR,  # contaminated pre-align
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		self.assertTrue(is_source_linked_consumption_return(doc))
		with _align_ctx():
			align_stock_entry_item_amounts(doc)
		self.assertEqual(flt(row.valuation_rate), float(SOURCE_RATE))
		self.assertEqual(flt(row.amount), float(RATE_FIRST_AMOUNT))
		self.assertTrue(source_linked_return_keeps_rate_authoritative(doc, row, "IRR"))

	def test_align_does_not_bump_amount_to_chase_derived_vr(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		with _align_ctx():
			align_stock_entry_item_amounts(doc)
		self.assertEqual(flt(row.amount), float(RATE_FIRST_AMOUNT))
		self.assertNotEqual(
			integer_valuation_rate_from_amount(row.amount, FRAC_QTY, "IRR"),
			float(SOURCE_RATE),
		)

	def test_ordinary_receipt_still_derives_vr_from_amount(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=0,
			amount=0,
		)
		doc = _Doc(custom_is_returned=0, items=[row])
		with _align_ctx():
			align_stock_entry_item_amounts(doc)
		self.assertEqual(flt(row.amount), float(RATE_FIRST_AMOUNT))
		self.assertEqual(flt(row.valuation_rate), float(DERIVED_VR))
		self.assertFalse(source_linked_return_keeps_rate_authoritative(doc, row, "IRR"))

	def test_exact_rate_full_qty_return_passes_prevention(self):
		src = _Row(
			qty=1,
			s_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			amount=SOURCE_RATE,
		)
		source = _Doc(name="MAT-STE-SRC", docstatus=1, purpose="Material Issue", items=[src])
		ret = _Row(
			qty=1,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			amount=SOURCE_RATE,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-SRC",
			items=[ret],
		)
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}.frappe.db.exists", return_value=True
		), mock.patch(f"{PREV}.frappe.get_doc", return_value=source), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			assert_source_linked_return_valuation(doc)

	def test_fractional_return_passes_prevention_after_align(self):
		src = _Row(
			qty=1,
			s_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			amount=SOURCE_RATE,
		)
		source = _Doc(
			name="MAT-STE-2026-42580", docstatus=1, purpose="Material Issue", items=[src]
		)
		ret = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=DERIVED_VR,
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[ret],
		)
		with _align_ctx():
			align_stock_entry_item_amounts(doc)
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}.frappe.db.exists", return_value=True
		), mock.patch(f"{PREV}.frappe.get_doc", return_value=source), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			assert_source_linked_return_valuation(doc)

	def test_non_zero_residual_disqualifies_rate_authoritative_gate(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT + 1,
			amount=RATE_FIRST_AMOUNT + 1,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		self.assertFalse(source_linked_return_keeps_rate_authoritative(doc, row, "IRR"))


class TestLedgerCompositionException(unittest.TestCase):
	def test_source_linked_fractional_return_composition_passes(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		with _composition_ctx():
			failures = _assert_row_composition(doc, doc.company)
		self.assertEqual(failures, [])

	def test_ordinary_fractional_receipt_still_requires_derived_vr(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,  # wrong for ordinary: must be DERIVED_VR
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		doc = _Doc(custom_is_returned=0, items=[row])
		with _composition_ctx():
			failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(
			any("ROUND_HALF_UP(amount / transfer_qty)" in f for f in failures),
			failures,
		)

	def test_return_with_amount_bump_still_fails_composition(self):
		"""Positive residual must not sneak through the rate-authoritative gate."""
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT + 1,
			amount=RATE_FIRST_AMOUNT + 1,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		with _composition_ctx():
			failures = _assert_row_composition(doc, doc.company)
		# basic_amount ≠ ROUND(qty×rate) and/or VR ≠ ROUND(amount/qty)
		self.assertTrue(failures, "bumped amount must fail composition")

	def test_exact_qty_return_composition_passes_without_needing_exception(self):
		row = _Row(
			qty=1,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=SOURCE_RATE,
			amount=SOURCE_RATE,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-SRC",
			items=[row],
		)
		with _composition_ctx():
			failures = _assert_row_composition(doc, doc.company)
		self.assertEqual(failures, [])

	def test_return_with_amount_cut_still_fails_composition(self):
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT - 1,
			amount=RATE_FIRST_AMOUNT - 1,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		self.assertFalse(source_linked_return_keeps_rate_authoritative(doc, row, "IRR"))
		with _composition_ctx():
			failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(failures, "cut amount must fail composition")


class TestZeroResidualNoStockAdjustment(unittest.TestCase):
	"""Class A residual must be 0 → collect residuals empty (no SA / Round Off)."""

	def test_class_a_and_collector_empty_for_primary_case(self):
		from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
			collect_stock_entry_residuals,
			compute_rounding_residual,
		)

		self.assertEqual(
			compute_rounding_residual(RATE_FIRST_AMOUNT, FRAC_QTY, SOURCE_RATE, "IRR"),
			0.0,
		)
		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
			additional_cost=0,
		)
		doc = _Doc(
			purpose="Material Receipt",
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[row],
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.stock_entry_excludes_irr_residual_round_off",
			return_value=False,
		):
			self.assertEqual(collect_stock_entry_residuals(doc), [])

	def test_plus_one_amount_produces_class_a_residual(self):
		from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
			compute_rounding_residual,
		)

		self.assertEqual(
			compute_rounding_residual(RATE_FIRST_AMOUNT + 1, FRAC_QTY, SOURCE_RATE, "IRR"),
			1.0,
		)

	def test_sle_mirror_uses_row_amount_not_rate_times_qty(self):
		from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
			signed_movement_from_row_amount,
			stock_entry_row_amount,
		)

		row = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.round_currency",
			side_effect=lambda v, _c: float(int(round(float(v)))),
		):
			mag = stock_entry_row_amount(row, "اسپاد فارمد دارو")
		self.assertEqual(mag, float(RATE_FIRST_AMOUNT))
		self.assertEqual(signed_movement_from_row_amount(mag, FRAC_QTY), float(RATE_FIRST_AMOUNT))
		# Not the unrounded exact product, and not derived-rate×qty fiction.
		self.assertNotEqual(mag, float(FRAC_QTY * SOURCE_RATE))

	def test_mismatched_batch_still_rejected(self):
		src = _Row(
			qty=1,
			s_warehouse="WH",
			item_code="17400002",
			batch_no="OTHER-BATCH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			amount=SOURCE_RATE,
		)
		source = _Doc(name="MAT-STE-2026-42580", docstatus=1, purpose="Material Issue", items=[src])
		ret = _Row(
			qty=FRAC_QTY,
			t_warehouse="WH",
			basic_rate=SOURCE_RATE,
			valuation_rate=SOURCE_RATE,
			basic_amount=RATE_FIRST_AMOUNT,
			amount=RATE_FIRST_AMOUNT,
		)
		doc = _Doc(
			custom_is_returned=1,
			custom_reference_no="MAT-STE-2026-42580",
			items=[ret],
		)

		def _fake_throw(msg, exc=Exception, title=None, **kwargs):
			raise (exc or Exception)(str(msg))

		with mock.patch(f"{PREV}.frappe.throw", side_effect=_fake_throw), mock.patch(
			f"{PREV}.is_irr_company", return_value=True
		), mock.patch(f"{PREV}.frappe.db.exists", return_value=True), mock.patch(
			f"{PREV}.frappe.get_doc", return_value=source
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			with self.assertRaises(Exception) as ctx:
				assert_source_linked_return_valuation(doc)
		self.assertIn("no matching outgoing row", str(ctx.exception))


if __name__ == "__main__":
	unittest.main()
