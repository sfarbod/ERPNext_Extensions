# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.26 — same-item multi-FG plus same-item product reject.

Retain sample stays MAIN_FG. The reject stays MAIN_PRODUCT_REJECT.
The shared integer-rate leftover is the value difference, not an R1 gap.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
	CLASS_TYPE_C,
	classify_manufacture_irr_residual,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import (
	STRATEGY_SAME_ITEM_MULTI_FG,
	STRATEGY_SAME_ITEM_OUTPUT_FAMILY,
	get_allocation_owner,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	apply_iran_manufacture_output_contract,
	classify_manufacture_outputs,
)

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"
CONTRACT = "erpnext_extensions.iran_accounting.manufacture_output_contract"
RESIDUAL = "erpnext_extensions.iran_accounting.domain.manufacture_irr_residual"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.setdefault("custom_output_class", None)
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
		self.__dict__.setdefault("work_order", None)
		self.__dict__.setdefault("total_additional_costs", 0)
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _consumed(item, qty, rate, idx):
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse="WIP",
		t_warehouse=None,
		is_finished_item=0,
		idx=idx,
	)


def _fg(item, qty, warehouse, idx, rate=0):
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse=warehouse,
		is_finished_item=1,
		idx=idx,
	)


def _reject(item, qty, warehouse, idx, rate=0):
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse=warehouse,
		is_finished_item=0,
		is_scrap_item=1,
		secondary_item_type="Scrap",
		custom_output_class="MAIN_PRODUCT_REJECT",
		idx=idx,
	)


def _component_scrap(item, qty, rate, idx, warehouse="SCRAP-WH"):
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse=warehouse,
		is_finished_item=0,
		secondary_item_type="Scrap",
		custom_output_class="COMPONENT_SCRAP",
		idx=idx,
	)


@contextmanager
def _env(rejected=None, main_codes=None):
	rejected = rejected or {}
	main_codes = main_codes or {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		if doctype == "Warehouse" and fieldname == "is_rejected_warehouse":
			if name not in rejected:
				return None
			return 1 if rejected[name] else 0
		if doctype == "Item" and fieldname == "custom_main_item_code":
			return main_codes.get(name)
		if doctype == "Item" and fieldname == "stock_uom":
			return "Nos"
		if doctype == "Company" and fieldname == "default_currency":
			return "IRR"
		return None

	with ExitStack() as stack:
		for target, kwargs in (
			(f"{SCRAP}.is_irr_company", {"return_value": True}),
			(f"{SCRAP}.get_company_currency", {"return_value": "IRR"}),
			(f"{SCRAP}.get_currency_precision", {"return_value": 0}),
			(f"{SCRAP}.frappe.db.get_value", {"side_effect": _get_value}),
			(f"{STAGE}.is_irr_company", {"return_value": True}),
			(f"{STAGE}.get_company_currency", {"return_value": "IRR"}),
			(f"{STAGE}.frappe.db.get_value", {"side_effect": _get_value}),
			(f"{STAGE}.frappe.get_all", {"return_value": []}),
			(f"{CONTRACT}.is_irr_company", {"return_value": True}),
			(f"{CONTRACT}.get_company_currency", {"return_value": "IRR"}),
			(f"{CONTRACT}.frappe.db.get_value", {"side_effect": _get_value}),
			(f"{RESIDUAL}.is_irr_company", {"return_value": True}),
			(f"{RESIDUAL}.get_company_currency", {"return_value": "IRR"}),
			(f"{RESIDUAL}.get_currency_precision", {"return_value": 0}),
			(f"{RESIDUAL}._persisted_docstatus", {"return_value": 0}),
			(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				{"return_value": True},
			),
			(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				{"return_value": "IRR"},
			),
		):
			stack.enter_context(mock.patch(target, **kwargs))
		yield


def _family_doc():
	"""35260 shape: quarantine 990, retain 32, reject 78, component scrap separate."""
	return _Doc(
		items=[
			_consumed("SEMI", 1, 889_588_522, 1),
			_consumed("PACK", 1, 811_912, 2),
			_component_scrap("PACK", 1, 811_912, 3),
			_fg("FG", 990, "Quarantine", 12),
			_fg("FG", 32, "Retain", 13),
			_reject("FG", 78, "Reject", 14),
		]
	)


_REJECTED = {"Quarantine": False, "Retain": False, "Reject": True, "SCRAP-WH": True, "FG-WH": False}


class TestSameItemOutputFamily(unittest.TestCase):
	def test_retain_sample_stays_finished_and_reject_stays_reject(self):
		doc = _family_doc()
		with _env(rejected=_REJECTED):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_MAIN_FG]), 2)
		self.assertEqual(len(classified[CLASS_MAIN_PRODUCT_REJECT]), 1)
		self.assertEqual(doc.items[3].is_finished_item, 1)
		self.assertEqual(doc.items[4].is_finished_item, 1)
		self.assertEqual(doc.items[5].is_finished_item, 0)
		self.assertEqual(doc.items[5].custom_output_class, "MAIN_PRODUCT_REJECT")
		self.assertEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_OUTPUT_FAMILY)
		self.assertNotEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)
		for row in doc.items[3:6]:
			self.assertEqual(flt(row.basic_rate), 808717)
			self.assertGreaterEqual(flt(row.basic_amount), 0)
		self.assertEqual(flt(doc.value_difference), 178)
		self.assertEqual(flt(doc.total_incoming_value) - flt(doc.total_outgoing_value), 178)

	def test_second_apply_is_idempotent(self):
		doc = _family_doc()
		with _env(rejected=_REJECTED):
			apply_iran_manufacture_output_contract(doc)
			first = [(flt(r.basic_rate), flt(r.basic_amount), flt(r.amount)) for r in doc.items]
			apply_iran_manufacture_output_contract(doc)
			second = [(flt(r.basic_rate), flt(r.basic_amount), flt(r.amount)) for r in doc.items]
		self.assertEqual(first, second)

	def test_component_scrap_keeps_issued_rate(self):
		doc = _family_doc()
		with _env(rejected=_REJECTED):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[2].basic_rate), 811912)
		self.assertEqual(flt(doc.items[2].basic_amount), 811912)

	def test_type_c_residual_is_the_integer_rate_leftover(self):
		doc = _family_doc()
		with _env(rejected=_REJECTED):
			apply_iran_manufacture_output_contract(doc)
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_C)
		self.assertEqual(result.residual, -178)

	def test_different_item_multi_fg_plus_reject_fails_closed(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000, 1),
				_fg("FG1", 60, "Quarantine", 2),
				_fg("FG2", 40, "Retain", 3),
				_reject("FG1", 5, "Reject", 4),
			]
		)
		with _env(rejected=_REJECTED):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("not supported", str(ctx.exception).lower())

	def test_main_fg_in_rejected_warehouse_fails_closed(self):
		doc = _family_doc()
		doc.items[3].t_warehouse = "Reject"
		with _env(rejected=_REJECTED):
			with self.assertRaises(frappe.ValidationError) as ctx:
				apply_iran_manufacture_output_contract(doc)
		self.assertIn("main_fg_in_rejected_warehouse", str(ctx.exception))

	def test_unproven_warehouse_fails_closed(self):
		doc = _family_doc()
		with _env(rejected={}):
			with self.assertRaises(frappe.ValidationError):
				apply_iran_manufacture_output_contract(doc)

	def test_single_fg_product_reject_still_product_reject_owner(self):
		from erpnext_extensions.iran_accounting.manufacture_output_contract import (
			STRATEGY_PRODUCT_REJECT,
		)

		doc = _Doc(
			items=[
				_consumed("FG", 10, 100, 1),
				_fg("FG", 8, "FG-WH", 2),
				_reject("FG", 2, "Reject", 3),
			]
		)
		with _env(rejected=_REJECTED):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(get_allocation_owner(doc), STRATEGY_PRODUCT_REJECT)
		self.assertEqual(flt(doc.items[1].basic_rate), 100)
		self.assertEqual(flt(doc.items[2].basic_rate), 100)

	def test_same_item_multi_fg_without_reject_unchanged(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000, 1),
				_fg("FG", 60, "Quarantine", 2),
				_fg("FG", 40, "Retain", 3),
			]
		)
		with _env(rejected=_REJECTED):
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)
		self.assertEqual(flt(doc.value_difference), 0)
		self.assertEqual(
			flt(doc.items[1].basic_amount) + flt(doc.items[2].basic_amount),
			1000,
		)

	def test_additional_cost_is_spread_and_not_a_new_adjustment_policy(self):
		doc = _family_doc()
		doc.total_additional_costs = 1100
		doc.additional_costs = [type("T", (), {"get": lambda self, k, d=None: {"amount": 1100, "base_amount": 1100}.get(k, d)})()]
		with _env(rejected=_REJECTED):
			apply_iran_manufacture_output_contract(doc)
		family_oh = sum(flt(r.additional_cost) for r in doc.items[3:6])
		self.assertEqual(family_oh, 1100)
		self.assertEqual(flt(doc.items[3].basic_rate), 808717)
