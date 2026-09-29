# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.35 — stage-equivalent By-Product pre-Core bridge + qualification."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	EQUIV_FACTOR_FIELD,
	EQUIV_QTY_FIELD,
	PHYSICAL_CONV_FIELD,
	STAGE_EQUIV_BRIDGE_FLAG,
	assert_bridged_stage_outputs_priced,
	clear_core_auto_valuation_for_stage_bridge,
	is_stage_equivalent_output_candidate,
	normalize_equivalent_factor,
	permit_stage_equivalent_zero_valuation,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	apply_iran_manufacture_output_contract,
)

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"

BOX = "BOX(2pfs)"
SYRINGE = "سرنگ"
FG = "20100064"
BY = "30500006"
CP = "30500003"


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
		self.__dict__.setdefault("job_card", "PO-JOB07352")
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


def _canary_shape(fg_qty=882, by_qty=1, pool=2_269_890_977, operating=0):
	fg = _output(FG, fg_qty, is_fg=1, rate=0, stock_uom=BOX, t_warehouse="FG")
	by_row = _output(
		BY,
		by_qty,
		secondary_item_type="By-Product",
		rate=0,
		stock_uom=SYRINGE,
		t_warehouse="CO",
		**{EQUIV_FACTOR_FIELD: 0.0},
	)
	return _Doc(
		job_card="PO-JOB07352",
		total_additional_costs=operating,
		items=[_consumed("RM", 1, pool), fg, by_row],
	)


@contextmanager
def _env(
	main_item_codes=None,
	stock_uoms=None,
	uom_factors=None,
	jc_secondaries=None,
	jc_exists=None,
	bom_factors=None,
	bom_types=None,
):
	main_item_codes = main_item_codes or {}
	stock_uoms = stock_uoms or {
		FG: BOX,
		BY: SYRINGE,
		CP: "Nos",
		"FG": "Nos",
		"CP": "Nos",
		"BY": "Nos",
		"BY2": "Nos",
		"RM": "Nos",
	}
	uom_factors = uom_factors if uom_factors is not None else {
		(FG, SYRINGE): 2,
		(FG, BOX): 1,
		(BY, SYRINGE): 1,
	}
	jc_secondaries = jc_secondaries if jc_secondaries is not None else {
		"PO-JOB07352": [_Dict(item_code=BY, secondary_item_type="By-Product", idx=1)],
		"PO-JOB-TEST": [_Dict(item_code=BY, secondary_item_type="By-Product", idx=1)],
	}
	jc_exists = jc_exists if jc_exists is not None else {
		("PO-JOB07352", BY, "By-Product"): True,
		("PO-JOB-TEST", BY, "By-Product"): True,
		("PO-JOB-TEST", CP, "Co-Product"): True,
		("PO-JOB-TEST", "BY2", "By-Product"): True,
	}
	bom_factors = bom_factors or {}
	bom_types = bom_types or {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		if doctype == "Item" and fieldname == "custom_main_item_code":
			return main_item_codes.get(name)
		if doctype == "Item" and fieldname == "stock_uom":
			return stock_uoms.get(name)
		if doctype == "UOM Conversion Detail" and isinstance(name, dict):
			return uom_factors.get((name.get("parent"), name.get("uom")))
		if doctype == "BOM Secondary Item":
			if fieldname == "secondary_item_type":
				return bom_types.get(name)
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

	def _exists(doctype, name=None, **kwargs):
		if doctype == "Job Card Secondary Item":
			filters = name if isinstance(name, dict) else kwargs.get("filters") or {}
			key = (
				filters.get("parent"),
				filters.get("item_code"),
				filters.get("secondary_item_type"),
			)
			return bool(jc_exists.get(key))
		if doctype == "BOM Secondary Item":
			ref = name if isinstance(name, str) else (name or {}).get("name")
			return ref in bom_types or ref in bom_factors
		return False

	with (
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
		mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.is_irr_company", return_value=True),
		mock.patch(f"{STAGE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{STAGE}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.frappe.get_all", side_effect=_get_all),
		mock.patch(f"{STAGE}.frappe.db.exists", side_effect=_exists),
	):
		yield


class TestNormalizeEquivalentFactor(unittest.TestCase):
	def test_none_and_empty_are_unset(self):
		self.assertIsNone(normalize_equivalent_factor(None))
		self.assertIsNone(normalize_equivalent_factor(""))

	def test_zero_is_frappe_unset(self):
		self.assertIsNone(normalize_equivalent_factor(0))
		self.assertIsNone(normalize_equivalent_factor(0.0))

	def test_positive_preserved(self):
		self.assertEqual(normalize_equivalent_factor(2.5), 2.5)

	def test_negative_preserved_for_fail_closed(self):
		self.assertEqual(normalize_equivalent_factor(-1), -1.0)


class TestQualification(unittest.TestCase):
	def test_qualifying_by_product(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		with _env():
			self.assertTrue(is_stage_equivalent_output_candidate(doc, by_row))

	def test_explicit_manual_excluded(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.valuation_type = "Manual"
		by_row.set_basic_rate_manually = 1
		with _env():
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))

	def test_explicit_valuation_rate_excluded(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.valuation_type = "Valuation Rate"
		with _env():
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))

	def test_pct_component_cost_excluded(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.valuation_type = "% of Component Cost"
		by_row.bom_secondary_item = "BOM-SEC-1"
		with _env(bom_types={"BOM-SEC-1": "By-Product"}):
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))

	def test_non_jc_origin_fail_closed(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		with _env(jc_exists={}):
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))

	def test_scrap_not_candidate(self):
		doc = _canary_shape()
		scrap = _output("RM", 1, secondary_item_type="Scrap", stock_uom="Nos", t_warehouse="RJ")
		doc.items.append(scrap)
		with _env():
			self.assertFalse(is_stage_equivalent_output_candidate(doc, scrap))

	def test_zero_qty_fail_closed(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.qty = 0
		by_row.transfer_qty = 0
		with _env():
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))


class TestBridgeLifecycle(unittest.TestCase):
	def test_permit_sets_flag_and_allow_zero(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
		self.assertTrue(getattr(by_row, STAGE_EQUIV_BRIDGE_FLAG))
		self.assertEqual(cint_safe(by_row.allow_zero_valuation_rate), 1)

	def test_clear_core_auto_vr(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
		by_row.valuation_type = "Valuation Rate"  # Core auto-default
		clear_core_auto_valuation_for_stage_bridge(doc)
		self.assertFalse(by_row.valuation_type)

	def test_core_auto_vr_does_not_steal_stage_pool(self):
		"""B11 — Core empty→VR must not finance-exclude a bridged By-Product."""
		doc = _canary_shape()
		fg, by_row = doc.items[1], doc.items[2]
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
			by_row.valuation_type = "Valuation Rate"
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertGreater(flt(by_row.basic_rate), 0)
		self.assertGreater(flt(fg.basic_rate), 0)
		self.assertEqual(flt(by_row.allow_zero_valuation_rate), 0)

	def test_bridge_without_allocation_fails_closed(self):
		"""N08 — temporary bridge must not leak zero into ledger."""
		doc = _canary_shape()
		by_row = doc.items[2]
		setattr(by_row, STAGE_EQUIV_BRIDGE_FLAG, True)
		by_row.basic_rate = 0
		by_row.basic_amount = 0
		with self.assertRaises(frappe.ValidationError) as ctx:
			assert_bridged_stage_outputs_priced(doc)
		self.assertIn("stage-equivalent output was not priced", str(ctx.exception))

	def test_explicit_manual_not_bridged(self):
		"""B08 / N03"""
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.valuation_type = "Manual"
		by_row.set_basic_rate_manually = 1
		by_row.basic_rate = 1_433_287
		by_row.basic_amount = 1_433_287
		by_row.amount = 1_433_287
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
			self.assertFalse(getattr(by_row, STAGE_EQUIV_BRIDGE_FLAG, False))
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(by_row.basic_rate), 1_433_287)

	def test_explicit_vr_not_bridged(self):
		"""B09 / N04"""
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.valuation_type = "Valuation Rate"
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
			self.assertFalse(getattr(by_row, STAGE_EQUIV_BRIDGE_FLAG, False))


class TestAllocationMatrix(unittest.TestCase):
	def test_b01_same_uom(self):
		fg = _output("FG", 10, is_fg=1, stock_uom="Nos")
		by_row = _output("BY", 2, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 12, 1000), fg, by_row])
		with _env(
			stock_uoms={"FG": "Nos", "BY": "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [_Dict(item_code="BY", secondary_item_type="By-Product", idx=1)]
			},
			jc_exists={("PO-JOB-TEST", "BY", "By-Product"): True},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.basic_rate), flt(by_row.basic_rate))
		self.assertEqual(doc.get("custom_manufacturing_costing_contract_version"), MANUFACTURE_COSTING_CONTRACT_VERSION)

	def test_b02_b03_different_uom_box_syringe(self):
		doc = _canary_shape()
		fg, by_row = doc.items[1], doc.items[2]
		with _env():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.get(PHYSICAL_CONV_FIELD)), 2)
		self.assertEqual(flt(by_row.get(PHYSICAL_CONV_FIELD)), 1)
		self.assertEqual(flt(fg.get(EQUIV_QTY_FIELD)), 1764)
		self.assertEqual(flt(by_row.get(EQUIV_QTY_FIELD)), 1)
		self.assertEqual(flt(by_row.basic_rate), 1_286_057)
		self.assertEqual(flt(by_row.basic_amount), 1_286_057)
		self.assertEqual(flt(fg.basic_rate), 2_572_114)
		self.assertEqual(flt(fg.basic_amount), 2_268_604_920)
		# UOM equivalence: 1 BOX = 2 syringe → FG rate == 2 × BY rate
		self.assertEqual(flt(fg.basic_rate), flt(by_row.basic_rate) * 2)
		self.assertEqual(flt(fg.basic_amount) + flt(by_row.basic_amount), 2_269_890_977)

	def test_b04_explicit_factor(self):
		fg = _output("FG", 1, is_fg=1, stock_uom="Nos", **{EQUIV_FACTOR_FIELD: 4})
		by_row = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 1, 5000), fg, by_row])
		with _env(
			stock_uoms={"FG": "Nos", "BY": "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [_Dict(item_code="BY", secondary_item_type="By-Product", idx=1)]
			},
			jc_exists={("PO-JOB-TEST", "BY", "By-Product"): True},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.basic_amount), 4000)
		self.assertEqual(flt(by_row.basic_amount), 1000)

	def test_b05_missing_conversion_fail_closed(self):
		fg = _output("FG", 1, is_fg=1, stock_uom=BOX)
		by_row = _output("BY", 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 2, 100), fg, by_row])
		with _env(
			stock_uoms={"FG": BOX, "BY": "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [_Dict(item_code="BY", secondary_item_type="By-Product", idx=1)]
			},
			jc_exists={("PO-JOB-TEST", "BY", "By-Product"): True},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("1:1 is not assumed", str(ctx.exception))

	def test_b06_invalid_factor_fail_closed(self):
		fg = _output("FG", 1, is_fg=1, stock_uom="Nos", **{EQUIV_FACTOR_FIELD: -2})
		by_row = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 2, 100), fg, by_row])
		with _env(
			stock_uoms={"FG": "Nos", "BY": "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [_Dict(item_code="BY", secondary_item_type="By-Product", idx=1)]
			},
			jc_exists={("PO-JOB-TEST", "BY", "By-Product"): True},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("greater than zero", str(ctx.exception))

	def test_b13_by_product_plus_co_product_joint_pool(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		cp = _output(CP, 1, secondary_item_type="Co-Product", stock_uom="Nos", t_warehouse="CO")
		by_row = _output(BY, 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(
			job_card="PO-JOB-TEST",
			items=[_consumed("RM", 10, 1000), fg, cp, by_row],
		)
		with _env(
			stock_uoms={"FG": "Nos", CP: "Nos", BY: "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [
					_Dict(item_code=CP, secondary_item_type="Co-Product", idx=1),
					_Dict(item_code=BY, secondary_item_type="By-Product", idx=2),
				]
			},
			jc_exists={
				("PO-JOB-TEST", CP, "Co-Product"): True,
				("PO-JOB-TEST", BY, "By-Product"): True,
			},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(cp.basic_amount), 1000)
		self.assertEqual(flt(by_row.basic_amount), 1000)
		self.assertEqual(flt(fg.basic_amount), 8000)

	def test_b14_multiple_by_products(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		by1 = _output(BY, 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		by2 = _output("BY2", 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 10, 1000), fg, by1, by2])
		with _env(
			stock_uoms={"FG": "Nos", BY: "Nos", "BY2": "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [
					_Dict(item_code=BY, secondary_item_type="By-Product", idx=1),
					_Dict(item_code="BY2", secondary_item_type="By-Product", idx=2),
				]
			},
			jc_exists={
				("PO-JOB-TEST", BY, "By-Product"): True,
				("PO-JOB-TEST", "BY2", "By-Product"): True,
			},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(by1.basic_amount), 1000)
		self.assertEqual(flt(by2.basic_amount), 1000)

	def test_b16_component_scrap_preserved(self):
		fg = _output("FG", 8, is_fg=1, stock_uom="Nos")
		by_row = _output(BY, 1, secondary_item_type="By-Product", stock_uom="Nos", t_warehouse="CO")
		scrap = _output("RM", 1, secondary_item_type="Scrap", stock_uom="Nos", t_warehouse="RJ")
		doc = _Doc(job_card="PO-JOB-TEST", items=[_consumed("RM", 10, 1000), fg, by_row, scrap])
		with _env(
			stock_uoms={"FG": "Nos", BY: "Nos", "RM": "Nos"},
			uom_factors={},
			jc_secondaries={
				"PO-JOB-TEST": [_Dict(item_code=BY, secondary_item_type="By-Product", idx=1)]
			},
			jc_exists={("PO-JOB-TEST", BY, "By-Product"): True},
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(scrap.basic_rate), 1000)
		self.assertEqual(flt(scrap.additional_cost), 0)
		self.assertEqual(flt(fg.basic_amount) + flt(by_row.basic_amount), 9000)

	def test_b18_real_additional_cost_separate_pool(self):
		doc = _canary_shape(operating=10_000)
		fg, by_row = doc.items[1], doc.items[2]
		with _env():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		# Material pool still fully distributed; operating shares by eq qty
		self.assertEqual(flt(fg.basic_amount) + flt(by_row.basic_amount), 2_269_890_977)
		self.assertEqual(flt(fg.additional_cost) + flt(by_row.additional_cost), 10_000)
		self.assertGreater(flt(by_row.additional_cost), 0)

	def test_b24_idempotency(self):
		doc = _canary_shape()
		with _env():
			apply_iran_manufacture_output_contract(doc)
			r1 = (flt(doc.items[1].basic_rate), flt(doc.items[2].basic_rate), flt(doc.items[2].additional_cost))
			apply_iran_manufacture_output_contract(doc)
			apply_iran_manufacture_output_contract(doc)
			r3 = (flt(doc.items[1].basic_rate), flt(doc.items[2].basic_rate), flt(doc.items[2].additional_cost))
		self.assertEqual(r1, r3)


class TestBridgeCoverageGaps(unittest.TestCase):
	"""Exercise every qualification / bridge branch for 100% new-logic coverage."""

	def test_uses_v533_legacy_and_submitted_unstamped(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import uses_v533_contract

		legacy = _Doc(docstatus=1, custom_manufacturing_costing_contract_version="5.3.3")
		self.assertTrue(uses_v533_contract(legacy))
		stamped534 = _Doc(docstatus=1, custom_manufacturing_costing_contract_version="5.3.34")
		self.assertTrue(uses_v533_contract(stamped534))
		unstamped = _Doc(docstatus=1, custom_manufacturing_costing_contract_version="")
		self.assertFalse(uses_v533_contract(unstamped))
		draft = _Doc(docstatus=0, custom_manufacturing_costing_contract_version="")
		self.assertTrue(uses_v533_contract(draft))

	def test_manual_flag_alone_is_explicit(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			_has_explicit_independent_valuation_policy,
		)

		row = _Row(valuation_type=None, set_basic_rate_manually=1)
		self.assertTrue(_has_explicit_independent_valuation_policy(row))

	def test_origin_bom_fallback_with_job_card(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			_secondary_originates_from_manufacture,
		)

		doc = _Doc(job_card="PO-JOB-MISS")
		row = _output(BY, 1, secondary_item_type="By-Product", bom_secondary_item="BOM-SEC-BY")
		with _env(
			jc_exists={},
			bom_types={"BOM-SEC-BY": "By-Product"},
			bom_factors={"BOM-SEC-BY": 1},
		):
			self.assertTrue(_secondary_originates_from_manufacture(doc, row))

	def test_origin_bom_only_no_job_card(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			_secondary_originates_from_manufacture,
		)

		doc = _Doc(job_card=None)
		row = _output(BY, 1, secondary_item_type="By-Product", bom_secondary_item="BOM-SEC-BY")
		with _env(bom_types={"BOM-SEC-BY": "By-Product"}, bom_factors={"BOM-SEC-BY": 1}):
			self.assertTrue(_secondary_originates_from_manufacture(doc, row))
		row2 = _output(BY, 1, secondary_item_type="By-Product")
		with _env():
			self.assertFalse(_secondary_originates_from_manufacture(doc, row2))
		row3 = _output(None, 1, secondary_item_type="By-Product")
		row3.item_code = None
		with _env():
			self.assertFalse(_secondary_originates_from_manufacture(doc, row3))

	def test_origin_jc_bom_type_mismatch(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			_secondary_originates_from_manufacture,
		)

		doc = _Doc(job_card="PO-JOB-MISS")
		row = _output(BY, 1, secondary_item_type="By-Product", bom_secondary_item="BOM-SEC-CP")
		with _env(jc_exists={}, bom_types={"BOM-SEC-CP": "Co-Product"}):
			self.assertFalse(_secondary_originates_from_manufacture(doc, row))

	def test_candidate_early_exits(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		doc.purpose = "Material Transfer"
		with _env():
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))
		doc.purpose = "Manufacture"
		with mock.patch(f"{STAGE}.is_irr_company", return_value=False):
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))
		with _env():
			doc.set("custom_manufacturing_costing_contract_version", "")
			doc.docstatus = 1
			self.assertFalse(is_stage_equivalent_output_candidate(doc, by_row))
		doc.docstatus = 0
		outgoing = _consumed("RM", 1, 1)
		with _env():
			self.assertFalse(is_stage_equivalent_output_candidate(doc, outgoing))

	def test_permit_early_exits(self):
		doc = _Doc(purpose="Repack", items=[])
		permit_stage_equivalent_zero_valuation(doc)
		doc = _canary_shape()
		with mock.patch(f"{STAGE}.is_irr_company", return_value=False):
			permit_stage_equivalent_zero_valuation(doc)
		self.assertFalse(getattr(doc.items[2], STAGE_EQUIV_BRIDGE_FLAG, False))

	def test_clear_resets_manual_flag_from_core(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
		by_row.valuation_type = "Valuation Rate"
		by_row.set_basic_rate_manually = 1
		clear_core_auto_valuation_for_stage_bridge(doc)
		self.assertFalse(by_row.valuation_type)
		self.assertEqual(cint_safe(by_row.set_basic_rate_manually), 0)

	def test_apply_stage_output_contract_entry(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			apply_stage_output_contract,
		)

		doc = _canary_shape()
		with _env():
			permit_stage_equivalent_zero_valuation(doc)
			doc.items[2].valuation_type = "Valuation Rate"
			self.assertTrue(apply_stage_output_contract(doc))
		self.assertGreater(flt(doc.items[2].basic_rate), 0)

	def test_apply_stage_output_contract_guards(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			apply_stage_output_contract,
		)

		doc = _Doc(purpose="Repack", items=[])
		self.assertFalse(apply_stage_output_contract(doc))
		doc = _canary_shape()
		with mock.patch(f"{STAGE}.is_irr_company", return_value=False):
			self.assertFalse(apply_stage_output_contract(doc))
		doc = _Doc(
			docstatus=1,
			custom_manufacturing_costing_contract_version="",
			items=[_consumed("RM", 1, 1)],
		)
		self.assertFalse(apply_stage_output_contract(doc))

	def test_finance_excluded_vr_and_pct_branches(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			_is_finance_excluded,
			stamp_contract_version,
		)

		self.assertTrue(_is_finance_excluded(_Row(valuation_type="Valuation Rate")))
		self.assertTrue(
			_is_finance_excluded(
				_Row(valuation_type="% of Component Cost", bom_secondary_item="BOM-SEC-1")
			)
		)
		self.assertTrue(
			_is_finance_excluded(_Row(valuation_type="Manual", set_basic_rate_manually=1))
		)
		self.assertFalse(_is_finance_excluded(_Row(valuation_type="Manual", set_basic_rate_manually=0)))
		# submitted stamp is a no-op
		doc = _Doc(name="SE-SUB", docstatus=1, custom_manufacturing_costing_contract_version="5.3.34")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual._persisted_docstatus",
			return_value=1,
		):
			stamp_contract_version(doc)
		self.assertEqual(doc.get("custom_manufacturing_costing_contract_version"), "5.3.34")

	def test_set_field_exception_fallback(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import _set_field

		class Broken:
			def set(self, field, value):
				raise RuntimeError("nope")

		obj = Broken()
		_set_field(obj, "custom_x", 7)
		self.assertEqual(obj.custom_x, 7)

		class NoSet:
			pass

		bare = NoSet()
		_set_field(bare, "custom_y", 3)
		self.assertEqual(bare.custom_y, 3)

	def test_clear_skips_unflagged_rows(self):
		doc = _canary_shape()
		by_row = doc.items[2]
		by_row.valuation_type = "Valuation Rate"
		# no bridge flag → leave Core VR alone
		clear_core_auto_valuation_for_stage_bridge(doc)
		self.assertEqual(by_row.valuation_type, "Valuation Rate")
		# flagged but not VR → no mutation (branch 240→next)
		setattr(by_row, STAGE_EQUIV_BRIDGE_FLAG, True)
		by_row.valuation_type = "Manual"
		clear_core_auto_valuation_for_stage_bridge(doc)
		self.assertEqual(by_row.valuation_type, "Manual")
		# empty items list path
		clear_core_auto_valuation_for_stage_bridge(_Doc(items=None))
		clear_core_auto_valuation_for_stage_bridge(_Doc(items=[]))


def cint_safe(value):
	from frappe.utils import cint

	return cint(value)


if __name__ == "__main__":
	unittest.main()

