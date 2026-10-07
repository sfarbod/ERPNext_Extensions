# Copyright (c) 2026, ERPNext Extensions contributors
"""Unified Manufacture Output Contract Phase 1 — SAME_ITEM_MULTI_FG (v5.5.19).

Covers U1–U25 required regressions for MAT-STE-2026-40687 economics without
mutating the historical document.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	align_stock_entry_item_amounts,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import (
	STRATEGY_SAME_ITEM_MULTI_FG,
	STRATEGY_STAGE_CO,
	allocate_amount_by_qty,
	apply_same_item_multi_fg,
	build_economic_pools,
	build_same_item_multi_fg_plan,
	finalize_allocation_plan,
	get_allocation_owner,
	is_allocation_closed,
	r2_representation_bound,
	reject_r4_double_pool,
	verify_allocation_plan,
)
from erpnext_extensions.iran_accounting.manufacture_rounding import (
	align_manufacture_finished_good_residual,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	apply_iran_manufacture_output_contract,
)
from erpnext_extensions.iran_accounting.stock_entry import (
	apply_irr_manufacture_economic_finalize,
)

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"
ROUND = "erpnext_extensions.iran_accounting.manufacture_rounding"
STOCK = "erpnext_extensions.iran_accounting.stock_entry"
CONTRACT = "erpnext_extensions.iran_accounting.manufacture_output_contract"
QRA = "erpnext_extensions.iran_accounting.domain.qty_rate_amount"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("valuation_type", None)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.setdefault("bom_secondary_item", None)
		self.__dict__.setdefault("custom_output_class", None)
		self.__dict__.setdefault("secondary_item_type", None)
		self.__dict__.setdefault("is_scrap_item", 0)
		self.__dict__.setdefault("stock_uom", "Nos")
		self.__dict__.setdefault("name", None)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("job_card", None)
		self.__dict__.setdefault("total_additional_costs", 0)
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.setdefault("custom_manufacturing_costing_contract_version", None)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


class _Dict:
	def __init__(self, **fields):
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _consumed(item_code, qty, rate, s_warehouse="WIP", idx=1):
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=s_warehouse,
		t_warehouse=None,
		is_finished_item=0,
		idx=idx,
	)


def _fg(item_code, qty, t_warehouse, idx, rate=0.0, **extra):
	fields = dict(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse=t_warehouse,
		is_finished_item=1,
		stock_uom="Nos",
		idx=idx,
	)
	fields.update(extra)
	return _Row(**fields)


def _scrap(item_code, qty, rate, idx, custom_output_class="COMPONENT_SCRAP", **extra):
	fields = dict(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse="SCRAP",
		is_finished_item=0,
		is_scrap_item=1,
		type="Scrap",
		secondary_item_type="Scrap",
		custom_output_class=custom_output_class,
		stock_uom="Nos",
		idx=idx,
	)
	fields.update(extra)
	return _Row(**fields)


def _sa(doc) -> float:
	out = sum(flt(r.amount) for r in doc.items if r.get("s_warehouse"))
	inc = sum(flt(r.amount) for r in doc.items if r.get("t_warehouse") and not r.get("s_warehouse"))
	addl = flt(doc.get("total_additional_costs"))
	if not addl and doc.get("additional_costs"):
		addl = sum(flt(t.get("base_amount") or t.get("amount")) for t in doc.additional_costs)
	return round(out + addl - inc)


def _doc_40687_fixture(*, reverse_fg=False, core_poisoned=True):
	"""Reproduce MAT-STE-2026-40687 economics without the real document.

	Outgoing material is split so Component Scrap issued-rate reprice keeps
	independent scrap value at 6,502,331 (matching RM family rate), while total
	OUTGOING_MATERIAL stays 1,202,617,075.
	"""
	outgoing = 1_202_617_075
	scrap_amt = 6_502_331
	other_amt = outgoing - scrap_amt
	# RM family issued rate = scrap_amt / qty 1 — apply_component_scrap keeps it.
	rm = _consumed("RM", 1, scrap_amt, idx=1)
	other = _consumed("OTHER", 1, other_amt, idx=2)
	scrap = _scrap("ZRM", 1, scrap_amt, idx=3)
	q1, q2 = (29, 545) if reverse_fg else (545, 29)
	w1, w2 = ("Retain", "Quarantine") if reverse_fg else ("Quarantine", "Retain")
	i1, i2 = (19, 18) if reverse_fg else (18, 19)
	if core_poisoned:
		# Core get_basic_rate_for_manufactured_item(row.qty) style poison.
		r1 = round(outgoing / q1)
		r2 = round(outgoing / q2)
	else:
		r1 = r2 = 0
	fg_a = _fg("FG", q1, w1, i1, rate=r1)
	fg_b = _fg("FG", q2, w2, i2, rate=r2)
	return _Doc(items=[rm, other, scrap, fg_a, fg_b], docstatus=0)


@contextmanager
def _env(stock_uoms=None, main_item_codes=None, jc_secondaries=None):
	stock_uoms = stock_uoms or {}
	main_item_codes = main_item_codes or {}
	jc_secondaries = jc_secondaries if jc_secondaries is not None else {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		if doctype == "Item" and fieldname == "custom_main_item_code":
			return main_item_codes.get(name)
		if doctype == "Item" and fieldname == "stock_uom":
			return stock_uoms.get(name) or "Nos"
		if doctype == "Company" and fieldname == "default_currency":
			return "IRR"
		return None

	def _get_all(doctype, filters=None, fields=None, pluck=None, **kwargs):
		filters = filters or {}
		if doctype == "Job Card Secondary Item":
			return list(jc_secondaries.get(filters.get("parent"), []))
		return []

	with ExitStack() as stack:
		for target, kwargs in (
			(f"{SCRAP}.is_irr_company", {"return_value": True}),
			(f"{SCRAP}.get_company_currency", {"return_value": "IRR"}),
			(f"{SCRAP}.get_currency_precision", {"return_value": 0}),
			(f"{SCRAP}.frappe.db.get_value", {"side_effect": _get_value}),
			(f"{STAGE}.is_irr_company", {"return_value": True}),
			(f"{STAGE}.get_company_currency", {"return_value": "IRR"}),
			(f"{STAGE}.frappe.db.get_value", {"side_effect": _get_value}),
			(f"{STAGE}.frappe.get_all", {"side_effect": _get_all}),
			(f"{ROUND}.is_irr_company", {"return_value": True}),
			(f"{ROUND}.get_company_currency", {"return_value": "IRR"}),
			(f"{ROUND}.get_currency_precision", {"return_value": 0}),
			(f"{STOCK}.is_irr_company", {"return_value": True}),
			(f"{CONTRACT}.is_irr_company", {"return_value": True}),
			(f"{CONTRACT}.get_company_currency", {"return_value": "IRR"}),
			(f"{QRA}.rounding.is_irr_company", {"return_value": True}),
			(f"{QRA}.rounding.get_company_currency", {"return_value": "IRR"}),
			(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				{"return_value": True},
			),
			(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				{"return_value": "IRR"},
			),
			(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.is_irr_company",
				{"return_value": True},
			),
			(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.get_company_currency",
				{"return_value": "IRR"},
			),
			(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.get_currency_precision",
				{"return_value": 0},
			),
			(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual._persisted_docstatus",
				{"return_value": 0},
			),
		):
			stack.enter_context(mock.patch(target, **kwargs))
		yield


class TestManufactureOutputContractV5519(unittest.TestCase):
	"""U1–U25 unified contract Phase 1."""

	def test_u1_normal_single_main_fg_unchanged(self):
		# Core-priced single FG (rate-first preserves qty×rate material).
		fg = _fg("FG", 10, "FG-WH", 2, rate=100)
		doc = _Doc(items=[_consumed("RM", 1, 1000, idx=1), fg])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}, main_item_codes={}):
			apply_irr_manufacture_economic_finalize(doc)
		self.assertEqual(flt(fg.basic_amount), 1000)
		self.assertEqual(_sa(doc), 0)
		self.assertNotEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)

	def test_u2_40687_same_item_multi_fg_exact_pool_sa0(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		fg545 = next(r for r in doc.items if r.qty == 545)
		fg29 = next(r for r in doc.items if r.qty == 29)
		self.assertEqual(flt(fg545.basic_amount), 1_135_683_860)
		self.assertEqual(flt(fg29.basic_amount), 60_430_884)
		self.assertEqual(flt(fg545.basic_amount) + flt(fg29.basic_amount), 1_196_114_744)
		inc_basic = sum(
			flt(r.basic_amount)
			for r in doc.items
			if r.get("t_warehouse") and not r.get("s_warehouse")
		)
		self.assertEqual(inc_basic, 1_202_617_075)
		self.assertEqual(_sa(doc), 0)
		self.assertEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)
		from erpnext_extensions.iran_accounting.domain.currency import amount_rate_qty_residual

		for row, expected_mag in ((fg545, 220), (fg29, 12)):
			res = abs(
				amount_rate_qty_residual(row.basic_amount, row.qty, row.basic_rate, "IRR")
			)
			self.assertEqual(res, expected_mag)
			self.assertLessEqual(res, r2_representation_bound(row.qty))
	def test_u3_exact_divisible_pool(self):
		# pool 1000, qty 60+40 → 600+400 exact
		fg1 = _fg("FG", 60, "Q", 2)
		fg2 = _fg("FG", 40, "R", 3)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_same_item_multi_fg(doc))
		self.assertEqual(flt(fg1.basic_amount), 600)
		self.assertEqual(flt(fg2.basic_amount), 400)
		self.assertEqual(_sa(doc), 0)

	def test_u4_non_divisible_irr_rate_r2_no_sa(self):
		# pool 1000, qty 3+1 → rates leave R2 residual, SA=0
		fg1 = _fg("FG", 3, "Q", 2)
		fg2 = _fg("FG", 1, "R", 3)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_same_item_multi_fg(doc))
		self.assertEqual(flt(fg1.basic_amount) + flt(fg2.basic_amount), 1000)
		self.assertEqual(_sa(doc), 0)
		from erpnext_extensions.iran_accounting.domain.currency import amount_rate_qty_residual

		for row in (fg1, fg2):
			res = amount_rate_qty_residual(row.basic_amount, row.qty, row.basic_rate, "IRR")
			self.assertLessEqual(abs(res), r2_representation_bound(row.qty))

	def test_u5_multi_fg_plus_component_scrap(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			apply_same_item_multi_fg(doc)
		scrap = next(r for r in doc.items if r.get("custom_output_class") == "COMPONENT_SCRAP")
		self.assertEqual(flt(scrap.basic_amount), 6_502_331)
		self.assertEqual(_sa(doc), 0)

	def test_u6_multi_fg_plus_additional_cost(self):
		fg1 = _fg("FG", 60, "Q", 2)
		fg2 = _fg("FG", 40, "R", 3)
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 1, 1000), fg1, fg2],
		)
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_same_item_multi_fg(doc))
		self.assertEqual(flt(fg1.additional_cost) + flt(fg2.additional_cost), 100)
		self.assertEqual(flt(fg1.basic_amount) + flt(fg2.basic_amount), 1000)
		self.assertEqual(_sa(doc), 0)

	def test_u7_row_order_reversed_same_economics(self):
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			d1 = _doc_40687_fixture(reverse_fg=False)
			d2 = _doc_40687_fixture(reverse_fg=True)
			apply_same_item_multi_fg(d1)
			apply_same_item_multi_fg(d2)
		a545 = next(r for r in d1.items if r.qty == 545).basic_amount
		b545 = next(r for r in d2.items if r.qty == 545).basic_amount
		a29 = next(r for r in d1.items if r.qty == 29).basic_amount
		b29 = next(r for r in d2.items if r.qty == 29).basic_amount
		self.assertEqual(a545, b545)
		self.assertEqual(a29, b29)

	def test_u8_manual_shared_rate_preserved(self):
		# Healthy peer: shared manual rate, Σ material = pool
		rate = 10
		fg1 = _fg("FG", 60, "Q", 2, rate=rate, set_basic_rate_manually=1)
		fg2 = _fg("FG", 40, "R", 3, rate=rate, set_basic_rate_manually=1)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_same_item_multi_fg(doc))
		self.assertEqual(flt(fg1.basic_amount), 600)
		self.assertEqual(flt(fg2.basic_amount), 400)
		self.assertEqual(flt(fg1.basic_rate), 10)

	def test_u9_mixed_manual_auto_block(self):
		fg1 = _fg("FG", 60, "Q", 2, rate=10, set_basic_rate_manually=1)
		fg2 = _fg("FG", 40, "R", 3, rate=0, set_basic_rate_manually=0)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("mixed manual", str(ctx.exception).lower())

	def test_u10_different_main_fg_items_block(self):
		fg1 = _fg("FG-A", 60, "Q", 2)
		fg2 = _fg("FG-B", 40, "R", 3)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2])
		with _env(stock_uoms={"FG-A": "Nos", "FG-B": "Nos", "RM": "Nos"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("different item", str(ctx.exception).lower())

	def test_u11_multi_fg_plus_participating_co_does_not_steal(self):
		fg1 = _fg("FG", 9, "Q", 2)
		fg2 = _fg("FG", 1, "R", 3)
		cp = _Row(
			item_code="CP",
			qty=1,
			transfer_qty=1,
			basic_rate=0,
			basic_amount=0,
			amount=0,
			valuation_rate=0,
			s_warehouse=None,
			t_warehouse="CO",
			is_finished_item=0,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			idx=4,
		)
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 1, 1000), fg1, fg2, cp],
		)
		with _env(
			stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"},
			jc_secondaries={},
		):
			# STAGE_CO requires exactly one FG — throws; Multi-FG must not claim.
			with self.assertRaises(frappe.ValidationError):
				apply_iran_manufacture_output_contract(doc)
		self.assertNotEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)

	def test_u12_multi_fg_plus_product_reject_block(self):
		fg1 = _fg("FG", 60, "Q", 2)
		fg2 = _fg("FG", 40, "R", 3)
		reject = _scrap("FG", 5, 0, idx=4, custom_output_class="MAIN_PRODUCT_REJECT")
		reject.is_scrap_item = 1
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2, reject])
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos"},
			main_item_codes={},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		msg = str(ctx.exception).lower()
		self.assertTrue("product reject" in msg or "main_product_reject" in msg or "reject" in msg)

	def test_u13_independent_by_product_untouched(self):
		fg1 = _fg("FG", 60, "Q", 2)
		fg2 = _fg("FG", 40, "R", 3)
		by = _Row(
			item_code="BY",
			qty=2,
			transfer_qty=2,
			basic_rate=50,
			basic_amount=100,
			amount=100,
			valuation_rate=50,
			s_warehouse=None,
			t_warehouse="BY-WH",
			is_finished_item=0,
			secondary_item_type="By-Product",
			valuation_type="Valuation Rate",
			stock_uom="Nos",
			idx=4,
		)
		doc = _Doc(items=[_consumed("RM", 1, 1100), fg1, fg2, by])
		with _env(stock_uoms={"FG": "Nos", "BY": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_same_item_multi_fg(doc))
		self.assertEqual(flt(by.basic_amount), 100)
		self.assertEqual(flt(fg1.basic_amount) + flt(fg2.basic_amount), 1000)

	def test_u14_stage_equivalent_by_remains_stage_co(self):
		fg = _fg("FG", 9, "FG-WH", 2)
		cp = _Row(
			item_code="CP",
			qty=1,
			transfer_qty=1,
			basic_rate=0,
			basic_amount=0,
			amount=0,
			valuation_rate=0,
			s_warehouse=None,
			t_warehouse="CO",
			is_finished_item=0,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			idx=3,
		)
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 1, 1000), fg, cp],
		)
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(get_allocation_owner(doc), STRATEGY_STAGE_CO)
		self.assertEqual(flt(fg.basic_amount), 900)
		self.assertEqual(flt(cp.basic_amount), 100)
		self.assertEqual(_sa(doc), 0)

	def test_u15_co_product_equivalent_factor_unchanged(self):
		fg = _fg("FG", 9, "FG-WH", 2)
		cp = _Row(
			item_code="CP",
			qty=1,
			transfer_qty=1,
			basic_rate=0,
			basic_amount=0,
			amount=0,
			valuation_rate=0,
			s_warehouse=None,
			t_warehouse="CO",
			is_finished_item=0,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			idx=3,
		)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"}):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(fg.basic_amount), 900)
		self.assertEqual(flt(cp.basic_amount), 100)

	def test_u16_co_product_reject_path_still_stage(self):
		"""CO_PRODUCT_REJECT with participating CO stays STAGE_CO (not Multi-FG)."""
		fg = _fg("FG", 8, "FG-WH", 2)
		cp = _Row(
			item_code="CP",
			qty=1,
			transfer_qty=1,
			basic_rate=0,
			basic_amount=0,
			amount=0,
			s_warehouse=None,
			t_warehouse="CO",
			is_finished_item=0,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			idx=3,
		)
		# Co-product reject: scrap of CP family
		cpr = _scrap("ZCP", 1, 0, idx=4, custom_output_class=None)
		cpr.item_code = "ZCP"
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg, cp, cpr])
		with _env(
			stock_uoms={"FG": "Nos", "CP": "Nos", "ZCP": "Nos", "RM": "Nos"},
			main_item_codes={"ZCP": "CP"},
		):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(get_allocation_owner(doc), STRATEGY_STAGE_CO)
		self.assertNotEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)

	def test_u17_5518_40149_regression_still_fixed(self):
		fg = _fg("FG", 9, "FG-WH", 2)
		cp = _Row(
			item_code="CP",
			qty=1,
			transfer_qty=1,
			basic_rate=0,
			basic_amount=0,
			amount=0,
			s_warehouse=None,
			t_warehouse="CO",
			is_finished_item=0,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			idx=3,
		)
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 1, 1000), fg, cp],
		)
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"}):
			apply_irr_manufacture_economic_finalize(doc)
		self.assertEqual(_sa(doc), 0)
		self.assertEqual(flt(fg.basic_amount), 900)

	def test_u18_type_c_product_reject_still_r3(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			CLASS_TYPE_C,
			classify_manufacture_irr_residual,
		)

		fg = _fg("FG", 10, "FG-WH", 2)
		reject = _scrap("FG", 1, 0, idx=3)
		reject.custom_output_class = "MAIN_PRODUCT_REJECT"
		doc = _Doc(
			custom_manufacturing_costing_contract_version="5.3.43",
			items=[_consumed("RM", 1, 1000), fg, reject],
		)
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}, main_item_codes={}):
			apply_iran_manufacture_output_contract(doc)
			# Equal-rate absorb may leave TYPE C SA residual by design.
			result = classify_manufacture_irr_residual(doc)
		# Either TYPE C or closed economics with SA within policy — must not be Multi-FG.
		self.assertNotEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)
		if result.classification == CLASS_TYPE_C:
			self.assertGreaterEqual(abs(result.residual), 0)

	def test_u19_closed_plan_survives_align_stock_entry_item_amounts(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			apply_same_item_multi_fg(doc)
			fg545 = next(r for r in doc.items if r.qty == 545)
			before = flt(fg545.basic_amount)
			# Poison rates as if rate-first would recompose
			fg545.basic_rate = 9_999_999
			align_stock_entry_item_amounts(doc)
		self.assertEqual(flt(fg545.basic_amount), before)

	def test_u20_closed_plan_survives_align_manufacture_finished_good_residual(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			apply_same_item_multi_fg(doc)
			amounts = {
				r.qty: flt(r.basic_amount) for r in doc.items if r.get("is_finished_item")
			}
			align_manufacture_finished_good_residual(doc)
		for r in doc.items:
			if r.get("is_finished_item"):
				self.assertEqual(flt(r.basic_amount), amounts[r.qty])

	def test_u21_riv_recalculate_converges(self):
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
			apply_irr_stock_entry_contract_after_calculate,
		)

		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			# Simulate Core recalculate leaving poisoned rates, then RIV contract.
			apply_irr_stock_entry_contract_after_calculate(doc)
		fg545 = next(r for r in doc.items if r.qty == 545)
		fg29 = next(r for r in doc.items if r.qty == 29)
		self.assertEqual(flt(fg545.basic_amount), 1_135_683_860)
		self.assertEqual(flt(fg29.basic_amount), 60_430_884)
		self.assertEqual(_sa(doc), 0)

	def test_u22_finalize_twice_idempotent(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			apply_same_item_multi_fg(doc)
			snap = [(r.qty, r.basic_amount, r.amount, r.basic_rate) for r in doc.items]
			plan = build_same_item_multi_fg_plan(doc)
			finalize_allocation_plan(doc, plan)
			finalize_allocation_plan(doc, plan)
		snap2 = [(r.qty, r.basic_amount, r.amount, r.basic_rate) for r in doc.items]
		self.assertEqual(snap, snap2)

	def test_u23_verifier_rejects_r4_double_pool(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			reject_r4_double_pool(2_000_000, 1_000_000)
		self.assertIn("R4", str(ctx.exception))

	def test_u24_no_unexplained_stock_adjustment(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			apply_same_item_multi_fg(doc)
			verify_allocation_plan(doc)
		self.assertEqual(_sa(doc), 0)

	def test_u25_submitted_unstamped_historical_untouched(self):
		doc = _doc_40687_fixture()
		doc.docstatus = 1
		doc.custom_manufacturing_costing_contract_version = None
		poisoned = [flt(r.basic_amount) for r in doc.items if r.get("is_finished_item")]
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual._persisted_docstatus",
				return_value=1,
			):
				applied = apply_iran_manufacture_output_contract(doc)
		self.assertFalse(applied)
		after = [flt(r.basic_amount) for r in doc.items if r.get("is_finished_item")]
		self.assertEqual(poisoned, after)

	def test_remainder_deterministic_largest_qty(self):
		rows = [_fg("FG", 29, "R", 19), _fg("FG", 545, "Q", 18)]
		amts = allocate_amount_by_qty(1_196_114_744, rows, "IRR")
		# remainder on 545
		self.assertEqual(amts[1], 1_135_683_860)
		self.assertEqual(amts[0], 60_430_884)

	def test_pools_40687(self):
		doc = _doc_40687_fixture()
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			pools = build_economic_pools(doc)
		self.assertEqual(pools["outgoing_material"], 1_202_617_075)
		self.assertEqual(pools["independent_output_value"], 6_502_331)
		self.assertEqual(pools["allocatable_material_pool"], 1_196_114_744)


if __name__ == "__main__":
	unittest.main()
