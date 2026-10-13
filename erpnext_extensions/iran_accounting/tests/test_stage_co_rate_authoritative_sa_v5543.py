# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.43 — STAGE_CO rate-authoritative composition + TYPE C Stock Adjustment."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
	CLASS_TYPE_C,
	classify_manufacture_irr_residual,
	type_c_residual_bound,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	_assert_row_composition,
	_manufacture_output_amount_authoritative,
	_manufacture_stage_secondary_amount_authoritative,
)
from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	EQUIV_FACTOR_FIELD,
	allocate_stage_output_cost,
)

STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"
SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("valuation_type", None)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.setdefault("is_finished_item", 0)
		self.__dict__.setdefault("is_scrap_item", 0)
		self.__dict__.setdefault("s_warehouse", None)
		self.__dict__.setdefault("bom_secondary_item", None)
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
		self.__dict__.setdefault("name", "STE-TEST")
		self.__dict__.setdefault("job_card", None)
		self.__dict__.setdefault("total_additional_costs", 0)
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.setdefault(
			"custom_manufacturing_costing_contract_version", "5.3.43"
		)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


def _consumed(amount=7_654_007):
	return _Row(
		item_code="RM",
		qty=1,
		transfer_qty=1,
		basic_rate=amount,
		basic_amount=amount,
		amount=amount,
		valuation_rate=amount,
		s_warehouse="WIP",
		t_warehouse=None,
		idx=1,
		name="rm",
		stock_uom="Nos",
	)


def _by_product(
	*,
	qty=14000,
	basic_rate=0,
	basic_amount=0,
	name="by",
	idx=2,
	item_code="30100096",
	**extra,
):
	fields = dict(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=basic_rate,
		basic_amount=basic_amount,
		amount=basic_amount,
		valuation_rate=basic_rate,
		t_warehouse="CO",
		s_warehouse=None,
		secondary_item_type="By-Product",
		stock_uom="Millilitre",
		custom_output_class="CO_PRODUCT",
		idx=idx,
		name=name,
	)
	fields.update(extra)
	return _Row(**fields)


@contextmanager
def _stage_env(stock_uoms=None):
	stock_uoms = stock_uoms or {"30100096": "Millilitre", "RM": "Nos"}

	def _cached(doctype, name, field=None, *args, **kwargs):
		if doctype == "Item" and field == "stock_uom":
			return stock_uoms.get(name)
		if doctype == "Item" and field == "name":
			return name
		return None

	with mock.patch(f"{STAGE}.get_company_currency", return_value="IRR"), mock.patch(
		f"{STAGE}.frappe.get_cached_value", side_effect=_cached
	), mock.patch(f"{SCRAP}.frappe.db.get_value", return_value=None), mock.patch(
		f"{SCRAP}.frappe.get_cached_value", side_effect=_cached
	):
		yield


class TestStageCoRateAuthoritativeSaV5543(unittest.TestCase):
	def test_t1_target_shape_rate_amount_and_sa_residual(self):
		by_item = _by_product(
			**{
				EQUIV_FACTOR_FIELD: 1,
				"custom_physical_conversion": 2.25,
				"custom_equivalent_qty": 31500,
				"custom_common_uom": "سرنگ",
			}
		)
		doc = _Doc(items=[_consumed(), by_item])
		with _stage_env():
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(by_item.basic_rate), 547)
		self.assertEqual(flt(by_item.basic_amount), 7_658_000)
		self.assertEqual(flt(by_item.basic_amount), 14_000 * 547)
		self.assertEqual(flt(doc.total_outgoing_value), 7_654_007)
		self.assertEqual(flt(doc.total_incoming_value), 7_658_000)
		self.assertEqual(flt(doc.value_difference), 3993)

	def test_t2_no_hidden_r2_amount_authoritative_exemption(self):
		by_item = _by_product(basic_rate=547, basic_amount=7_654_007, amount=7_654_007)
		doc = _Doc(items=[_consumed(), by_item])
		self.assertFalse(_manufacture_output_amount_authoritative(doc, by_item, doc.company))
		self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
		failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_amount == transfer_qty" in f for f in failures))

	def test_t3_ledger_accepts_qty_times_rate(self):
		by_item = _by_product(
			basic_rate=547, basic_amount=7_658_000, amount=7_658_000, valuation_rate=547
		)
		doc = _Doc(items=[_consumed(), by_item])
		failures = _assert_row_composition(doc, doc.company)
		self.assertEqual(failures, [])

	def test_t4_type_c_classifies_stage_co_sa_residual(self):
		by_item = _by_product(
			basic_rate=547, basic_amount=7_658_000, amount=7_658_000, valuation_rate=547
		)
		doc = _Doc(items=[_consumed(), by_item])
		result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_C)
		self.assertEqual(flt(result.residual), -3993)
		self.assertLessEqual(abs(result.residual), type_c_residual_bound([14000]))

	def test_t5_decimal_rate_rejected(self):
		by_item = _by_product(
			basic_rate=546.7, basic_amount=7_658_000, amount=7_658_000, valuation_rate=546.7
		)
		doc = _Doc(items=[_consumed(), by_item])
		failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_rate integer IRR" in f for f in failures))

	def test_t6_single_fg_without_stage_co_keeps_fg_amount_gate(self):
		fg = _Row(
			item_code="FG",
			qty=2,
			transfer_qty=2,
			basic_rate=50,
			basic_amount=101,
			amount=101,
			valuation_rate=51,
			t_warehouse="FG",
			s_warehouse=None,
			is_finished_item=1,
			idx=2,
			name="fg",
		)
		doc = _Doc(items=[_consumed(amount=101), fg])
		self.assertTrue(_manufacture_output_amount_authoritative(doc, fg, doc.company))

	def test_t7_idempotent_rate_compose(self):
		by_item = _by_product(
			**{
				EQUIV_FACTOR_FIELD: 1,
				"custom_physical_conversion": 1,
				"custom_equivalent_qty": 14000,
				"custom_common_uom": "Millilitre",
			}
		)
		doc = _Doc(items=[_consumed(), by_item])
		with _stage_env():
			self.assertTrue(allocate_stage_output_cost(doc))
			rate1 = flt(by_item.basic_rate)
			amt1 = flt(by_item.basic_amount)
			vd1 = flt(doc.value_difference)
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(by_item.basic_rate), rate1)
		self.assertEqual(flt(by_item.basic_amount), amt1)
		self.assertEqual(flt(doc.value_difference), vd1)
		self.assertEqual(amt1, 7_658_000)
		self.assertEqual(vd1, 3993)

	def test_t8_finance_excluded_still_fails_closed(self):
		by_item = _by_product(
			qty=1,
			basic_rate=100,
			basic_amount=100,
			amount=100,
			valuation_rate=100,
			valuation_type="Valuation Rate",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed(), by_item])
		with _stage_env(stock_uoms={"30100096": "Nos", "RM": "Nos"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				allocate_stage_output_cost(doc)
		self.assertIn("no eligible output", str(ctx.exception).lower())


if __name__ == "__main__":
	unittest.main()
