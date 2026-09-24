# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.3 Gap 2 — equivalent-unit Stage Output costing."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	CONTRACT_VERSION_FIELD,
	EQUIV_FACTOR_FIELD,
	EQUIV_QTY_FIELD,
	PARENT_CO_PRODUCT_FIELD,
	PHYSICAL_CONV_FIELD,
	allocate_stage_output_cost,
	apply_stage_output_contract,
	uses_v533_contract,
	validate_job_card_secondary_match,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	apply_iran_manufacture_output_contract,
)

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"

BOX = "BOX(2pfs)"
SYRINGE = "سرنگ"
FG = "20100067"
CP = "30500003"
JC_MISMATCH = "30300020"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("valuation_type", None)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.setdefault("bom_secondary_item", None)
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


def _box_syringe_doc(fg_qty=1428, cp_qty=1, consume_amount=2_857_000, stale_cp_rate=0, operating=0):
	consume_qty = 1
	consume_rate = consume_amount
	fg = _output(FG, fg_qty, is_fg=1, rate=0, stock_uom=BOX, t_warehouse="FG")
	cp = _output(
		CP,
		cp_qty,
		secondary_item_type="By-Product",
		rate=stale_cp_rate,
		stock_uom=SYRINGE,
		t_warehouse="CO",
	)
	return _Doc(
		job_card="PO-JOB-TEST",
		total_additional_costs=operating,
		items=[_consumed("RM", consume_qty, consume_rate), fg, cp],
	)


@contextmanager
def _env(
	main_item_codes=None,
	stock_uoms=None,
	uom_factors=None,
	jc_secondaries=None,
	bom_factors=None,
):
	main_item_codes = main_item_codes or {}
	stock_uoms = stock_uoms or {
		FG: BOX,
		CP: SYRINGE,
		JC_MISMATCH: "Nos",
	}
	uom_factors = uom_factors or {
		(FG, SYRINGE): 2,
		(FG, BOX): 1,
		(CP, SYRINGE): 1,
	}
	jc_secondaries = jc_secondaries if jc_secondaries is not None else {
		"PO-JOB-TEST": [_Dict(item_code=CP, secondary_item_type="By-Product", idx=1)],
	}
	bom_factors = bom_factors or {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		field = fieldname
		if doctype == "Item" and field == "custom_main_item_code":
			return main_item_codes.get(name)
		if doctype == "Item" and field == "stock_uom":
			return stock_uoms.get(name)
		if doctype == "UOM Conversion Detail" and isinstance(name, dict):
			return uom_factors.get((name.get("parent"), name.get("uom")))
		if doctype == "BOM Secondary Item":
			return bom_factors.get(name)
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

	with (
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
		mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.is_irr_company", return_value=True),
		mock.patch(f"{STAGE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{STAGE}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.frappe.get_all", side_effect=_get_all),
	):
		yield


class TestJobCardSecondaryMatch(unittest.TestCase):
	def test_matching_item_passes(self):
		doc = _box_syringe_doc()
		with _env():
			validate_job_card_secondary_match(doc)

	def test_item_mismatch_blocks(self):
		doc = _box_syringe_doc()
		doc.job_card = "PO-JOB06720"
		with _env(
			jc_secondaries={
				"PO-JOB06720": [
					_Dict(item_code=JC_MISMATCH, secondary_item_type="By-Product", idx=1)
				]
			}
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				validate_job_card_secondary_match(doc)
		message = str(ctx.exception)
		self.assertIn("PO-JOB06720", message)
		self.assertIn(JC_MISMATCH, message)
		self.assertIn(CP, message)
		self.assertIn("By-Product", message)

	def test_type_mismatch_blocks(self):
		doc = _box_syringe_doc()
		doc.items[2].secondary_item_type = "Co-Product"
		with _env():
			with self.assertRaises(frappe.ValidationError) as ctx:
				validate_job_card_secondary_match(doc)
		message = str(ctx.exception)
		self.assertIn("By-Product", message)
		self.assertIn("Co-Product", message)
		self.assertIn(CP, message)


class TestEquivalentUnitCosting(unittest.TestCase):
	def test_main_fg_plus_matching_co_product(self):
		fg = _output("FG", 10, is_fg=1, stock_uom="Nos")
		cp = _output("CP", 10, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(items=[_consumed("RM", 20, 1000), fg, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.basic_amount) + flt(cp.basic_amount), 20000)
		self.assertEqual(flt(fg.basic_rate), flt(cp.basic_rate))

	def test_multiple_co_products(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp1 = _output("CP1", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		cp2 = _output("CP2", 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(items=[_consumed("RM", 10, 1000), fg, cp1, cp2])
		with _env(
			stock_uoms={"FG": "Nos", "CP1": "Nos", "CP2": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.basic_amount) + flt(cp1.basic_amount) + flt(cp2.basic_amount), 10000)
		self.assertEqual(flt(cp1.basic_amount), 1000)
		self.assertEqual(flt(cp2.basic_amount), 1000)
		self.assertEqual(flt(fg.basic_amount), 8000)

	def test_main_fg_plus_co_product_reject(self):
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos")
		cp = _output("CP", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		reject = _output("CP", 1, secondary_item_type="Scrap", stock_uom="Nos", t_warehouse="RJ")
		doc = _Doc(items=[_consumed("RM", 11, 1000), fg, cp, reject])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(reject.get(PARENT_CO_PRODUCT_FIELD), "CP")
		self.assertEqual(
			flt(fg.basic_amount) + flt(cp.basic_amount) + flt(reject.basic_amount),
			11000,
		)
		self.assertEqual(flt(cp.basic_rate), flt(reject.basic_rate))

	def test_component_scrap_plus_co_product(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp = _output("CP", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		scrap = _output("RM", 1, secondary_item_type="Scrap", stock_uom="Nos", t_warehouse="RJ")
		doc = _Doc(items=[_consumed("RM", 10, 1000), fg, cp, scrap])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(scrap.basic_rate), 1000)
		self.assertEqual(flt(scrap.basic_amount), 1000)
		self.assertEqual(flt(scrap.additional_cost), 0)
		self.assertEqual(flt(fg.basic_amount) + flt(cp.basic_amount), 9000)

	def test_same_uom_outputs(self):
		fg = _output("FG", 3, is_fg=1, stock_uom="Nos")
		cp = _output("CP", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(items=[_consumed("RM", 4, 250), fg, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(fg.basic_rate), 250)
		self.assertEqual(flt(cp.basic_rate), 250)

	def test_different_uom_valid_conversion(self):
		doc = _box_syringe_doc()
		with _env():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(doc.items[1].get(PHYSICAL_CONV_FIELD)), 2)
		self.assertEqual(flt(doc.items[2].get(PHYSICAL_CONV_FIELD)), 1)

	def test_box_to_syringe_factor_two(self):
		doc = _box_syringe_doc()
		with _env():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[1].get(EQUIV_QTY_FIELD)), 2856)
		self.assertEqual(flt(doc.items[2].get(EQUIV_QTY_FIELD)), 1)

	def test_one_syringe_is_half_box_equivalent(self):
		doc = _box_syringe_doc()
		with _env():
			apply_iran_manufacture_output_contract(doc)
		fg_eq_rate = flt(doc.items[1].basic_amount) / flt(doc.items[1].get(EQUIV_QTY_FIELD))
		cp_eq_rate = flt(doc.items[2].basic_amount) / flt(doc.items[2].get(EQUIV_QTY_FIELD))
		self.assertAlmostEqual(fg_eq_rate, cp_eq_rate, delta=1)
		self.assertEqual(flt(doc.items[1].basic_rate), 2000)
		self.assertEqual(flt(doc.items[2].basic_rate), 1000)

	def test_missing_conversion_and_factor_blocks(self):
		fg = _output("FG", 1, is_fg=1, stock_uom=BOX)
		cp = _output("CP", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(items=[_consumed("RM", 2, 100), fg, cp])
		with _env(
			stock_uoms={"FG": BOX, "CP": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("1:1 is not assumed", str(ctx.exception))

	def test_explicit_equivalent_factor(self):
		fg = _output("FG", 1, is_fg=1, stock_uom=BOX)
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 2},
		)
		fg.set(EQUIV_FACTOR_FIELD, 4)
		doc = _Doc(items=[_consumed("RM", 1, 6000), fg, cp])
		with _env(stock_uoms={"FG": BOX, "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.get(EQUIV_QTY_FIELD)), 4)
		self.assertEqual(flt(cp.get(EQUIV_QTY_FIELD)), 2)
		self.assertEqual(flt(fg.basic_amount), 4000)
		self.assertEqual(flt(cp.basic_amount), 2000)

	def test_multiple_different_factors(self):
		fg = _output("FG", 1, is_fg=1, stock_uom="Nos", **{EQUIV_FACTOR_FIELD: 5})
		cp1 = _output(
			"CP1",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 3},
		)
		cp2 = _output(
			"CP2",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 2},
		)
		doc = _Doc(items=[_consumed("RM", 1, 10000), fg, cp1, cp2])
		with _env(
			stock_uoms={"FG": "Nos", "CP1": "Nos", "CP2": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(fg.basic_amount), 5000)
		self.assertEqual(flt(cp1.basic_amount), 3000)
		self.assertEqual(flt(cp2.basic_amount), 2000)

	def test_factor_not_positive_blocks(self):
		fg = _output("FG", 1, is_fg=1, stock_uom="Nos", **{EQUIV_FACTOR_FIELD: 0})
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 2, 100), fg, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("greater than zero", str(ctx.exception))

	def test_ambiguous_co_product_parent_blocks(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp_a = _output("CPA", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		cp_b = _output("CPB", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		reject = _output("CPA", 1, secondary_item_type="Scrap", stock_uom="Nos", t_warehouse="RJ")
		doc = _Doc(items=[_consumed("RM", 11, 1000), fg, cp_a, cp_b, reject])
		with _env(
			main_item_codes={"CPA": "CPB"},
			stock_uoms={"FG": "Nos", "CPA": "Nos", "CPB": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("parent Co-Product", str(ctx.exception))

	def test_co_product_scrap_inherits_factor(self):
		fg = _output("FG", 1, is_fg=1, stock_uom="Nos", **{EQUIV_FACTOR_FIELD: 1})
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 3},
		)
		reject = _output("ZCP", 1, secondary_item_type="Scrap", stock_uom="Nos", t_warehouse="RJ")
		doc = _Doc(items=[_consumed("RM", 1, 5000), fg, cp, reject])
		with _env(
			main_item_codes={"ZCP": "CP"},
			stock_uoms={"FG": "Nos", "CP": "Nos", "ZCP": "Nos"},
			uom_factors={},
			jc_secondaries={},
		):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(reject.get(EQUIV_FACTOR_FIELD)), 3)
		self.assertEqual(reject.get(PARENT_CO_PRODUCT_FIELD), "CP")
		self.assertEqual(flt(cp.basic_amount), flt(reject.basic_amount))

	def test_operating_cost_shared_per_equivalent_unit(self):
		doc = _box_syringe_doc(operating=2857)
		with _env():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[1].additional_cost), 2856)
		self.assertEqual(flt(doc.items[2].additional_cost), 1)

	def test_component_scrap_receives_no_operating_cost(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp = _output("CP", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		scrap = _output(
			"RM",
			1,
			secondary_item_type="Scrap",
			stock_uom="Nos",
			t_warehouse="RJ",
			additional_cost=999,
		)
		doc = _Doc(
			total_additional_costs=900,
			items=[_consumed("RM", 10, 1000), fg, cp, scrap],
		)
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(scrap.additional_cost), 0)
		self.assertEqual(flt(fg.additional_cost) + flt(cp.additional_cost), 900)

	def test_irr_rounding_residual_goes_to_main_fg(self):
		fg = _output("FG", 2, is_fg=1, stock_uom="Nos")
		cp = _output("CP", 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(items=[_consumed("RM", 1, 100), fg, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(fg.basic_amount) + flt(cp.basic_amount), 100)
		self.assertEqual(flt(cp.basic_amount), 33)
		self.assertEqual(flt(fg.basic_amount), 67)

	def test_stage_output_total_equals_stage_cost_pool(self):
		doc = _box_syringe_doc()
		with _env():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[1].basic_amount) + flt(doc.items[2].basic_amount), 2_857_000)

	def test_displayed_rates_differ_by_uom_while_equivalent_rate_matches(self):
		doc = _box_syringe_doc()
		with _env():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[1].basic_rate), 2 * flt(doc.items[2].basic_rate))
		fg_eq = flt(doc.items[1].basic_amount) / 2856
		cp_eq = flt(doc.items[2].basic_amount) / 1
		self.assertAlmostEqual(fg_eq, cp_eq, delta=1)

	def test_target_warehouse_valuation_does_not_influence_stage_cost(self):
		doc = _box_syringe_doc(stale_cp_rate=999_999)
		self.assertEqual(flt(doc.items[2].basic_rate), 999_999)
		with _env():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[2].basic_rate), 1000)
		self.assertEqual(doc.items[2].allow_zero_valuation_rate, 0)

	def test_manual_bom_precedence_preserved(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp = _output(
			"CP",
			2,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			rate=5000,
			valuation_type="Manual",
			set_basic_rate_manually=1,
		)
		doc = _Doc(items=[_consumed("RM", 10, 2000), fg, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(cp.basic_rate), 5000)
		self.assertEqual(flt(cp.basic_amount), 10000)
		self.assertEqual(flt(fg.basic_amount), 10000)

	def test_valuation_rate_by_product_keeps_independent_rate(self):
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos")
		by_item = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			rate=100,
			valuation_type="Valuation Rate",
		)
		doc = _Doc(items=[_consumed("RM", 10, 1000), fg, by_item])
		with _env(stock_uoms={"FG": "Nos", "BY": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(by_item.basic_rate), 100)
		self.assertEqual(flt(by_item.basic_amount), 100)
		self.assertEqual(flt(fg.basic_amount), 9900)

	def test_historical_submitted_without_stamp_not_recosted(self):
		doc = _box_syringe_doc(stale_cp_rate=0)
		doc.docstatus = 1
		with _env():
			self.assertFalse(uses_v533_contract(doc))
			self.assertFalse(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertFalse(doc.get(CONTRACT_VERSION_FIELD))

	def test_draft_is_stamped_and_idempotent(self):
		doc = _box_syringe_doc()
		with _env():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			first_fg = flt(doc.items[1].basic_amount)
			first_cp = flt(doc.items[2].basic_amount)
			self.assertEqual(doc.get(CONTRACT_VERSION_FIELD), MANUFACTURE_COSTING_CONTRACT_VERSION)
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(doc.items[1].basic_amount), first_fg)
		self.assertEqual(flt(doc.items[2].basic_amount), first_cp)

	def test_apply_stage_output_contract_runs_match_then_allocate(self):
		doc = _box_syringe_doc()
		with _env():
			self.assertTrue(apply_stage_output_contract(doc))
		self.assertEqual(flt(doc.items[2].basic_rate), 1000)

	def test_allocate_without_co_product_is_noop(self):
		fg = _output("FG", 10, is_fg=1, stock_uom="Nos", rate=100)
		doc = _Doc(items=[_consumed("RM", 10, 100), fg])
		with _env(stock_uoms={"FG": "Nos"}, uom_factors={}, jc_secondaries={}):
			self.assertFalse(allocate_stage_output_cost(doc))

	def test_changed_uom_after_snapshot_does_not_recost(self):
		doc = _box_syringe_doc()
		mutated = {(FG, SYRINGE): 99, (FG, BOX): 1, (CP, SYRINGE): 1}
		with _env():
			apply_iran_manufacture_output_contract(doc)
			fg_amount = flt(doc.items[1].basic_amount)
			cp_amount = flt(doc.items[2].basic_amount)
			self.assertEqual(flt(doc.items[1].get(PHYSICAL_CONV_FIELD)), 2)
		with _env(uom_factors=mutated):
			doc.docstatus = 1
			doc.set(CONTRACT_VERSION_FIELD, MANUFACTURE_COSTING_CONTRACT_VERSION)
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[1].basic_amount), fg_amount)
		self.assertEqual(flt(doc.items[2].basic_amount), cp_amount)
		self.assertEqual(flt(doc.items[1].get(PHYSICAL_CONV_FIELD)), 2)
		self.assertEqual(flt(doc.items[1].get(EQUIV_QTY_FIELD)), 2856)

	def test_changed_factor_after_snapshot_does_not_recost(self):
		fg = _output("FG", 1, is_fg=1, stock_uom="Nos", **{EQUIV_FACTOR_FIELD: 5})
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 5},
		)
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 1, 10000), fg, cp])
		jc = {
			"PO-JOB-TEST": [
				_Dict(
					item_code="CP",
					secondary_item_type="Co-Product",
					idx=1,
					**{EQUIV_FACTOR_FIELD: 5},
				)
			]
		}
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries=jc):
			apply_iran_manufacture_output_contract(doc)
			self.assertEqual(flt(fg.basic_amount), 5000)
			doc.docstatus = 1
			doc.set(CONTRACT_VERSION_FIELD, MANUFACTURE_COSTING_CONTRACT_VERSION)
		jc_changed = {
			"PO-JOB-TEST": [
				_Dict(
					item_code="CP",
					secondary_item_type="Co-Product",
					idx=1,
					**{EQUIV_FACTOR_FIELD: 1},
				)
			]
		}
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos"}, uom_factors={}, jc_secondaries=jc_changed):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(fg.basic_amount), 5000)
		self.assertEqual(flt(cp.get(EQUIV_FACTOR_FIELD)), 5)

	def test_submitted_skips_job_card_rematch(self):
		doc = _box_syringe_doc()
		doc.docstatus = 1
		doc.set(CONTRACT_VERSION_FIELD, MANUFACTURE_COSTING_CONTRACT_VERSION)
		with _env(
			jc_secondaries={
				"PO-JOB-TEST": [_Dict(item_code=JC_MISMATCH, secondary_item_type="By-Product", idx=1)]
			}
		):
			validate_job_card_secondary_match(doc)


if __name__ == "__main__":
	unittest.main()
