# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.30 — Manufacture residual must survive rate-first finalize + ledger contract.

Regression class for MAT-STE-2026-37736: post-SLE rate-first rewrite destroyed the
Manufacture FG pool residual when additional_cost > IRR tolerance, then
enforce_stock_entry_ledger_contract rejected its own changed state.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import round_monetary_rate
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import align_stock_entry_item_amounts
from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	_assert_row_composition,
)
from erpnext_extensions.iran_accounting.manufacture_rounding import (
	align_manufacture_finished_good_residual,
)
from erpnext_extensions.iran_accounting.scrap_costing import apply_iran_manufacture_output_contract
from erpnext_extensions.iran_accounting.stock_entry import apply_irr_manufacture_economic_finalize

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("name", fields.get("name") or f"row-{id(self)}")
		self.__dict__.setdefault("item_code", "ITEM")
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "Test IRR Co")
		self.__dict__.setdefault("name", "STE-TEST")
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("total_additional_costs", 0)
		self.__dict__.setdefault("custom_manufacturing_costing_contract_version", "5.3.3")
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


def _consumed(item_code, qty, rate, **extra):
	amount = int(round(qty * rate))
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		valuation_rate=rate,
		basic_amount=amount,
		amount=amount,
		s_warehouse="WIP",
		t_warehouse=None,
		is_finished_item=0,
		**extra,
	)


def _fg(item_code, qty, rate, additional_cost=0, **extra):
	basic = int(round(qty * rate))
	amount = basic + additional_cost
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		valuation_rate=rate,
		basic_amount=basic,
		amount=amount,
		additional_cost=additional_cost,
		s_warehouse=None,
		t_warehouse="FG",
		is_finished_item=1,
		**extra,
	)


def _scrap(item_code, qty, rate, **extra):
	amount = int(round(qty * rate))
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		valuation_rate=rate,
		basic_amount=amount,
		amount=amount,
		s_warehouse=None,
		t_warehouse="Scrap",
		is_finished_item=0,
		is_scrap_item=1,
		secondary_item_type="Scrap",
		custom_output_class="COMPONENT_SCRAP",
		**extra,
	)


@contextmanager
def _irr_patches(*, contract=True):
	"""Patch IRR currency helpers + optional Manufacture contract stamp."""
	patches = [
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_rounding.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_rounding.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_rounding.get_currency_precision",
			return_value=0,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.rounding.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.rounding.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
			return_value=True,
		),
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
	]
	if contract:
		patches.extend(
			[
				mock.patch(f"{STAGE}.uses_v533_contract", return_value=True),
				mock.patch(f"{STAGE}.stamp_contract_version", return_value=None),
				mock.patch(f"{STAGE}.validate_job_card_secondary_match", return_value=None),
				mock.patch(f"{STAGE}.snapshot_equivalent_factors_from_sources", return_value=None),
				mock.patch(f"{STAGE}.allocate_stage_output_cost", return_value=False),
				mock.patch(f"{SCRAP}._item_main_code", return_value=None),
			]
		)
	for p in patches:
		p.start()
	try:
		yield
	finally:
		for p in reversed(patches):
			p.stop()


def _snapshot(doc):
	return [
		{
			"item_code": r.item_code,
			"basic_rate": flt(r.basic_rate),
			"basic_amount": flt(r.basic_amount),
			"amount": flt(r.amount),
			"valuation_rate": flt(r.valuation_rate),
			"additional_cost": flt(r.additional_cost),
		}
		for r in doc.items
	]


def _pool_ok(doc, tol=1.0):
	outgoing = sum(flt(r.amount) for r in doc.items if r.get("s_warehouse"))
	incoming = sum(flt(r.amount) for r in doc.items if r.get("t_warehouse"))
	add = sum(flt(r.additional_cost) for r in doc.items)
	return abs(incoming - (outgoing + add)) <= tol


def _whole_irr(doc):
	for r in doc.items:
		for field in ("basic_amount", "amount", "additional_cost", "basic_rate", "valuation_rate"):
			val = flt(r.get(field))
			if abs(val - round(val)) > 1e-9:
				return False
	return True


def _fg_row(doc):
	return next(r for r in doc.items if r.get("is_finished_item"))


class TestManufactureResidualSubmitV5330(unittest.TestCase):
	"""Twelve scenarios required by the v5.3.30 release gate."""

	def test_01_manufacture_no_scrap_no_add_cost(self):
		doc = _Doc(
			items=[
				_consumed("RM", 10, 1000),
				_fg("FG", 10, 1000),
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		self.assertTrue(_whole_irr(doc))
		self.assertTrue(_pool_ok(doc))
		fg = _fg_row(doc)
		self.assertEqual(flt(fg.amount), 10000)
		self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_02_integer_rate_residual_absorbed_by_fg(self):
		# qty × int rate cannot reproduce pool: material 10007, qty 3 → rate 3336 → 10008
		doc = _Doc(
			items=[
				_consumed("RM", 1, 10007),
				_fg("FG", 3, 3336),  # qty×rate seed; residual restore must win
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		fg = _fg_row(doc)
		self.assertEqual(flt(fg.basic_amount), 10007)
		self.assertEqual(flt(fg.amount), 10007)
		self.assertTrue(_pool_ok(doc))
		self.assertTrue(_whole_irr(doc))
		# valuation_rate is integer from amount/qty; amount authoritative
		self.assertEqual(flt(fg.valuation_rate), round_monetary_rate(10007 / 3, "IRR"))

	def test_03_additional_cost_eq_one_irr(self):
		doc = _Doc(
			items=[
				_consumed("RM", 10, 1000),
				_fg("FG", 10, 1000, additional_cost=1),
			],
			total_additional_costs=1,
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		fg = _fg_row(doc)
		self.assertEqual(flt(fg.additional_cost), 1)
		self.assertEqual(flt(fg.amount), 10001)
		self.assertTrue(_pool_ok(doc))

	def test_04_additional_cost_gt_tolerance_does_not_skip_residual(self):
		"""Critical: add_cost=5 must not prevent Manufacture residual restore."""
		# Pool material 10073; qty 10 → int rate 1007 → 10070; residual 3
		doc = _Doc(
			items=[
				_consumed("RM", 1, 10073),
				_fg("FG", 10, 1007, additional_cost=5),
			],
			total_additional_costs=5,
		)
		with _irr_patches():
			# Simulate on_submit wrong-path damage then correct finalize
			align_stock_entry_item_amounts(doc)
			self.assertEqual(flt(_fg_row(doc).basic_amount), 10070)  # qty×rate
			apply_irr_manufacture_economic_finalize(doc)
		fg = _fg_row(doc)
		self.assertEqual(flt(fg.basic_amount), 10073)
		self.assertEqual(flt(fg.amount), 10078)  # +5
		self.assertEqual(flt(fg.additional_cost), 5)
		self.assertTrue(_pool_ok(doc))
		self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_05_component_scrap_pool_conservation(self):
		doc = _Doc(
			items=[
				_consumed("LABEL", 100, 10),
				_consumed("SEMI", 50, 100),
				_fg("FG", 50, 100),
				_scrap("LABEL", 10, 10),
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		outgoing = sum(flt(r.amount) for r in doc.items if r.s_warehouse)
		incoming = sum(flt(r.amount) for r in doc.items if r.t_warehouse)
		self.assertEqual(incoming, outgoing)
		scrap = next(r for r in doc.items if r.get("custom_output_class") == "COMPONENT_SCRAP")
		self.assertEqual(flt(scrap.basic_rate), 10)
		self.assertTrue(_pool_ok(doc))

	def test_06_component_scrap_plus_additional_cost_canary_class(self):
		"""Class of MAT-STE-2026-37736: scrap + add_cost=5 + integer-rate residual."""
		# Outgoing 5221574554-class miniature: 1138*14548 + 965*5393802 scaled down
		label_rate = 14548
		semi_rate = 5393802
		label_qty = 1138
		semi_qty = 965
		scrap_qty = 172
		fg_qty = 965
		add_cost = 5
		outgoing = label_qty * label_rate + semi_qty * semi_rate
		scrap_amt = scrap_qty * label_rate
		material = outgoing - scrap_amt
		int_rate = int(round(material / fg_qty))
		doc = _Doc(
			items=[
				_consumed("13200489", label_qty, label_rate),
				_consumed("30200061", semi_qty, semi_rate),
				_fg("30300068", fg_qty, int_rate, additional_cost=add_cost),
				_scrap("13200489", scrap_qty, label_rate),
			],
			total_additional_costs=add_cost,
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
			# Idempotency: second pass must not drift toward qty×rate
			first = _snapshot(doc)
			apply_irr_manufacture_economic_finalize(doc)
			second = _snapshot(doc)
		self.assertEqual(first, second)
		fg = _fg_row(doc)
		self.assertEqual(flt(fg.basic_amount), material)
		self.assertEqual(flt(fg.amount), material + add_cost)
		# Not the destroyed qty×rate figure
		self.assertNotEqual(flt(fg.amount), int_rate * fg_qty + add_cost)
		self.assertTrue(_pool_ok(doc))
		self.assertTrue(_whole_irr(doc))
		self.assertEqual(_assert_row_composition(doc, doc.company), [])
		# Exact canary numbers from investigation
		self.assertEqual(material, 5219072298)
		self.assertEqual(flt(fg.amount), 5219072303)

	def test_07_multiple_consumed_components(self):
		# Exact divisible pool 1750 / qty 5 = 350.
		doc = _Doc(
			items=[
				_consumed("A", 3, 100),
				_consumed("B", 7, 200),
				_consumed("C", 1, 50),
				_fg("FG", 5, 350),
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		self.assertEqual(flt(_fg_row(doc).amount), 1750)
		self.assertTrue(_pool_ok(doc))

	def test_08_multiple_scrap_rows_deterministic(self):
		doc = _Doc(
			items=[
				_consumed("A", 100, 10),
				_consumed("B", 50, 20),
				_fg("FG", 40, 47),
				_scrap("A", 5, 10),
				_scrap("B", 2, 20),
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
			snap = _snapshot(doc)
			apply_irr_manufacture_economic_finalize(doc)
		self.assertEqual(snap, _snapshot(doc))
		scrap_rates = {
			r.item_code: flt(r.basic_rate)
			for r in doc.items
			if r.get("secondary_item_type") == "Scrap"
		}
		self.assertEqual(scrap_rates.get("A"), 10)
		self.assertEqual(scrap_rates.get("B"), 20)
		self.assertTrue(_pool_ok(doc))

	def test_09_fractional_source_rates_persist_whole_irr(self):
		# Source economics that do not divide cleanly
		doc = _Doc(
			items=[
				_Row(
					item_code="RM",
					qty=7,
					transfer_qty=7,
					basic_rate=1000.142857,  # will be integerized
					valuation_rate=1000.142857,
					basic_amount=7001,
					amount=7001,
					s_warehouse="WIP",
					t_warehouse=None,
					is_finished_item=0,
				),
				_fg("FG", 7, 1000),
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		self.assertTrue(_whole_irr(doc))
		self.assertTrue(_pool_ok(doc))
		outgoing = sum(flt(r.amount) for r in doc.items if r.s_warehouse)
		incoming = sum(flt(r.amount) for r in doc.items if r.t_warehouse)
		self.assertEqual(incoming, outgoing)
		self.assertEqual(outgoing, int(outgoing))

	def test_10_exact_divisible_fg_unchanged_economics(self):
		doc = _Doc(
			items=[
				_consumed("RM", 8, 125),
				_fg("FG", 8, 125),
			]
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
		fg = _fg_row(doc)
		self.assertEqual(flt(fg.amount), 1000)
		self.assertEqual(flt(fg.basic_amount), 1000)
		self.assertEqual(flt(fg.basic_amount) % 8, 0)

	def test_11_ledger_contract_still_fails_on_real_mismatch(self):
		doc = _Doc(
			purpose="Manufacture",
			items=[
				_consumed("RM", 10, 100),
				_fg("FG", 10, 100),
			],
		)
		# Intentionally strip capitalization from amount while leaving add_cost
		fg = _fg_row(doc)
		fg.additional_cost = 5
		fg.amount = flt(fg.basic_amount)  # stripped — invalid
		failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("additional_cost" in f or "amount ≈" in f for f in failures))

	def test_12_non_manufacture_material_transfer_unchanged(self):
		doc = _Doc(
			purpose="Material Transfer",
			items=[
				_Row(
					item_code="RM",
					qty=5,
					transfer_qty=5,
					basic_rate=100,
					valuation_rate=100,
					basic_amount=500,
					amount=500,
					s_warehouse="A",
					t_warehouse="B",
					is_finished_item=0,
				)
			],
		)
		with _irr_patches(contract=False):
			before = _snapshot(doc)
			# Transfer finalize path: rate-first only via align (contract no-ops on purpose)
			align_stock_entry_item_amounts(doc)
			align_manufacture_finished_good_residual(doc)
			after = _snapshot(doc)
		self.assertEqual(before, after)
		self.assertEqual(flt(doc.items[0].amount), 500)

	def test_on_submit_order_matches_validate_helper(self):
		"""Wrong historical order destroyed residual; helper must restore it."""
		doc = _Doc(
			items=[
				_consumed("RM", 1, 10073),
				_fg("FG", 10, 1007, additional_cost=5),
			],
			total_additional_costs=5,
		)
		with _irr_patches():
			apply_iran_manufacture_output_contract(doc)
			align_stock_entry_item_amounts(doc)
			damaged = flt(_fg_row(doc).amount)
			align_manufacture_finished_good_residual(doc)
			fixed_by_residual = flt(_fg_row(doc).amount)
			apply_irr_manufacture_economic_finalize(doc)
			fixed = flt(_fg_row(doc).amount)
		self.assertEqual(damaged, 10075)  # 10070+5 qty×rate path
		self.assertEqual(fixed_by_residual, 10078)
		self.assertEqual(fixed, 10078)  # 10073+5 pool path

	def test_idempotent_double_finalize(self):
		doc = _Doc(
			items=[
				_consumed("13200489", 1138, 14548),
				_consumed("30200061", 965, 5393802),
				_fg("30300068", 965, 5408365, additional_cost=5),
				_scrap("13200489", 172, 14548),
			],
			total_additional_costs=5,
		)
		with _irr_patches():
			apply_irr_manufacture_economic_finalize(doc)
			a = _snapshot(doc)
			apply_irr_manufacture_economic_finalize(doc)
			b = _snapshot(doc)
		self.assertEqual(a, b)
		self.assertEqual(flt(_fg_row(doc).amount), 5219072303)


if __name__ == "__main__":
	unittest.main()
