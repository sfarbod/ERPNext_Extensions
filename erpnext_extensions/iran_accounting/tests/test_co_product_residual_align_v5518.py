# Copyright (c) 2026, ERPNext Extensions contributors
"""Stage-owned CO_PRODUCT must survive FG residual align (v5.5.18).

Historical bug: allocate_stage_output_cost closed material + OH pools, then
align_manufacture_finished_good_residual recomputed FG.basic from
outgoing − Σ non-FG amount (which embeds CO_PRODUCT additional_cost), cutting
FG material by exactly the Co-Product OH share and inventing Stock Adjustment.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_rounding import (
	align_manufacture_finished_good_residual,
)
from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	has_stage_participating_co_product,
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


def _consumed(item_code, qty, rate, s_warehouse="WIP"):
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
	)


def _output(
	item_code,
	qty,
	is_fg=0,
	rate=0.0,
	secondary_item_type=None,
	stock_uom="Nos",
	t_warehouse="FG",
	**extra,
):
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
		is_finished_item=is_fg,
		stock_uom=stock_uom,
	)
	if secondary_item_type is not None:
		fields["secondary_item_type"] = secondary_item_type
	fields.update(extra)
	return _Row(**fields)


def _sa(doc) -> float:
	out = sum(flt(r.amount) for r in doc.items if r.get("s_warehouse"))
	inc = sum(flt(r.amount) for r in doc.items if r.get("t_warehouse") and not r.get("s_warehouse"))
	addl = flt(doc.get("total_additional_costs"))
	if not addl and doc.get("additional_costs"):
		addl = sum(flt(t.get("base_amount") or t.get("amount")) for t in doc.additional_costs)
	return round(out + addl - inc)


def _material_hole(doc) -> float:
	out_b = sum(flt(r.basic_amount) for r in doc.items if r.get("s_warehouse"))
	in_b = sum(
		flt(r.basic_amount) for r in doc.items if r.get("t_warehouse") and not r.get("s_warehouse")
	)
	return round(out_b - in_b)


@contextmanager
def _env(stock_uoms=None, uom_factors=None, jc_secondaries=None, main_item_codes=None):
	stock_uoms = stock_uoms or {}
	uom_factors = uom_factors or {}
	jc_secondaries = jc_secondaries if jc_secondaries is not None else {}
	main_item_codes = main_item_codes or {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		field = fieldname
		if doctype == "Item" and field == "custom_main_item_code":
			return main_item_codes.get(name)
		if doctype == "Item" and field == "stock_uom":
			return stock_uoms.get(name) or "Nos"
		if doctype == "UOM Conversion Detail" and isinstance(name, dict):
			return uom_factors.get((name.get("parent"), name.get("uom")))
		return None

	def _get_all(doctype, filters=None, fields=None, pluck=None, **kwargs):
		filters = filters or {}
		if doctype == "Job Card Secondary Item":
			return list(jc_secondaries.get(filters.get("parent"), []))
		if doctype == "UOM Conversion Detail":
			item = filters.get("parent")
			uoms = [uom for (code, uom) in uom_factors if code == item]
			if pluck == "uom":
				return uoms
			return [_Dict(uom=uom) for uom in uoms]
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


class TestCoProductResidualAlignV5518(unittest.TestCase):
	"""Mandatory regression suite for stage-owned CO_PRODUCT residual guard."""

	def test_01_exact_bug_reproduction_mini_pool(self):
		"""material=1000, OH=100, eq 9+1 → SA must stay 0 after full finalize."""
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos")
		cp = _output(
			"CP",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
		)
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 1, 1000), fg, cp],
		)
		uoms = {"FG": "Nos", "CP": "Nos", "RM": "Nos"}
		with _env(stock_uoms=uoms, uom_factors={}, jc_secondaries={}):
			self.assertTrue(has_stage_participating_co_product(doc))
			apply_irr_manufacture_economic_finalize(doc)

		self.assertEqual(flt(fg.basic_amount), 900)
		self.assertEqual(flt(cp.basic_amount), 100)
		self.assertEqual(flt(fg.additional_cost), 90)
		self.assertEqual(flt(cp.additional_cost), 10)
		self.assertEqual(flt(fg.amount) + flt(cp.amount), 1100)
		self.assertEqual(_material_hole(doc), 0)
		self.assertEqual(_sa(doc), 0)

	def test_02_full_finalize_preserves_stage_fg_basic(self):
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos")
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
		)
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 10, 100), fg, cp],
		)
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_iran_manufacture_output_contract(doc)
			stage_fg = flt(fg.basic_amount)
			apply_irr_manufacture_economic_finalize(doc)

		self.assertEqual(flt(fg.basic_amount), stage_fg)
		self.assertEqual(_material_hole(doc), 0)
		self.assertEqual(_sa(doc), 0)

	def test_05_simple_main_fg_with_additional_cost_still_aligns(self):
		"""No CO_PRODUCT — legacy residual aligner must still run."""
		fg = _output("FG", 10, is_fg=1, stock_uom="Nos", rate=90, additional_cost=100)
		# Deliberately wrong FG basic so aligner restores pool:
		# outgoing 1000, FG.ac 100 → FG.basic should become 1000, amount 1100
		fg.basic_amount = 900
		fg.basic_rate = 90
		fg.amount = 1000  # missing OH in amount
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 10, 100), fg],
		)
		with _env(stock_uoms={"FG": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertFalse(has_stage_participating_co_product(doc))
			align_manufacture_finished_good_residual(doc)

		self.assertEqual(flt(fg.basic_amount), 1000)
		self.assertEqual(flt(fg.amount), 1100)
		self.assertEqual(_sa(doc), 0)

	def test_06_component_scrap_without_co_product(self):
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos")
		scrap = _output(
			"RM",
			1,
			secondary_item_type="Scrap",
			stock_uom="Nos",
			t_warehouse="SCRAP",
			rate=100,
		)
		# Same item as consumed → COMPONENT_SCRAP at issued rate
		doc = _Doc(
			total_additional_costs=0,
			items=[_consumed("RM", 10, 100), fg, scrap],
		)
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_irr_manufacture_economic_finalize(doc)

		self.assertFalse(has_stage_participating_co_product(doc))
		self.assertEqual(flt(scrap.basic_amount), 100)
		self.assertEqual(flt(fg.basic_amount) + flt(scrap.basic_amount), 1000)
		self.assertEqual(_material_hole(doc), 0)

	def test_07_finance_excluded_by_product_does_not_skip_aligner(self):
		"""Independent Valuation Rate By-Product is not stage-owned."""
		fg = _output("FG", 10, is_fg=1, stock_uom="Nos", rate=80, additional_cost=100)
		fg.basic_amount = 800
		fg.amount = 900
		bp = _output(
			"BP",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			rate=200,
			valuation_type="Valuation Rate",
			set_basic_rate_manually=0,
		)
		bp.basic_amount = 200
		bp.amount = 200
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			custom_manufacturing_costing_contract_version="5.3.43",
			items=[_consumed("RM", 10, 100), fg, bp],
		)
		with _env(stock_uoms={"FG": "Nos", "BP": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertFalse(has_stage_participating_co_product(doc))
			# Aligner uses other_incoming amount (BP 200): expected FG basic = 1000-200=800
			# With FG.ac=100, pool_target = 1000-200+100=900 — already matches; leave intact
			# Force a wrong FG amount so aligner must act:
			fg.basic_amount = 700
			fg.amount = 800
			align_manufacture_finished_good_residual(doc)

		# expected_basic = out - other_amount = 1000 - 200 = 800; amount = 800 + 100 = 900
		self.assertEqual(flt(fg.basic_amount), 800)
		self.assertEqual(flt(fg.amount), 900)
		self.assertEqual(flt(bp.basic_amount), 200)

	def test_08_multiple_co_products_with_additional_cost(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp1 = _output("CP1", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		cp2 = _output("CP2", 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(
			total_additional_costs=100,
			additional_costs=[_Dict(amount=100, base_amount=100)],
			items=[_consumed("RM", 10, 100), fg, cp1, cp2],
		)
		with _env(
			stock_uoms={"FG": "Nos", "CP1": "Nos", "CP2": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			apply_irr_manufacture_economic_finalize(doc)

		self.assertEqual(
			flt(fg.basic_amount) + flt(cp1.basic_amount) + flt(cp2.basic_amount), 1000
		)
		self.assertEqual(
			flt(fg.additional_cost) + flt(cp1.additional_cost) + flt(cp2.additional_cost), 100
		)
		self.assertEqual(_material_hole(doc), 0)
		self.assertEqual(_sa(doc), 0)

	def test_09_fractional_equivalent_factors(self):
		fg = _output(
			"FG",
			10,
			is_fg=1,
			stock_uom="Nos",
			custom_output_equivalent_factor=1.0,
		)
		cp = _output(
			"CP",
			2,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			custom_output_equivalent_factor=0.5,
		)
		doc = _Doc(
			total_additional_costs=30,
			additional_costs=[_Dict(amount=30, base_amount=30)],
			items=[_consumed("RM", 1, 1000), fg, cp],
		)
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_irr_manufacture_economic_finalize(doc)

		# eq: FG 10*1=10, CP 2*0.5=1, total 11
		self.assertEqual(flt(fg.basic_amount) + flt(cp.basic_amount), 1000)
		self.assertEqual(flt(fg.additional_cost) + flt(cp.additional_cost), 30)
		self.assertEqual(_material_hole(doc), 0)
		self.assertEqual(_sa(doc), 0)

	def test_10_zero_additional_cost_no_invented_sa(self):
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos")
		cp = _output(
			"CP",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
		)
		doc = _Doc(
			total_additional_costs=0,
			items=[_consumed("RM", 1, 1000), fg, cp],
		)
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_irr_manufacture_economic_finalize(doc)

		self.assertEqual(flt(cp.additional_cost), 0)
		self.assertEqual(flt(fg.basic_amount) + flt(cp.basic_amount), 1000)
		self.assertEqual(_sa(doc), 0)

	def test_guard_helper_excludes_finance_excluded_only(self):
		fg = _output("FG", 9, is_fg=1)
		stage_cp = _output("CP", 1, secondary_item_type="By-Product", t_warehouse="CO")
		indep = _output(
			"BP",
			1,
			secondary_item_type="By-Product",
			t_warehouse="CO",
			valuation_type="Valuation Rate",
			rate=50,
		)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg, stage_cp, indep])
		with _env(
			stock_uoms={"FG": "Nos", "CP": "Nos", "BP": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			self.assertTrue(has_stage_participating_co_product(doc))
			# Only independent BP → false
			doc2 = _Doc(items=[_consumed("RM", 1, 1000), fg, indep])
			self.assertFalse(has_stage_participating_co_product(doc2))


class TestTypeBPreserved(unittest.TestCase):
	def test_11_type_b_single_fg_amount_authoritative(self):
		"""Single FG, no CO_PRODUCT: ± residual in amount vs qty×rate may remain."""
		fg = _output("FG", 3, is_fg=1, stock_uom="Nos")
		# amount-authoritative: basic_amount not equal to rate*qty
		fg.basic_rate = 10
		fg.basic_amount = 29  # 10*3=30 → residual -1
		fg.amount = 29
		fg.valuation_rate = 10
		doc = _Doc(
			custom_manufacturing_costing_contract_version="5.3.43",
			items=[_consumed("RM", 3, 10), fg],
		)
		# Force totals already closed on amount
		with _env(stock_uoms={"FG": "Nos"}, uom_factors={}, jc_secondaries={}):
			align_manufacture_finished_good_residual(doc)
		# No capitalization; within residual tol of 0 delta on amounts → integer rates polish
		self.assertEqual(flt(fg.amount) + 0, flt(fg.amount))
		self.assertEqual(_sa(doc), 0)


if __name__ == "__main__":
	unittest.main()
