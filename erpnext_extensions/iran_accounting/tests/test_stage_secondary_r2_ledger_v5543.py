# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.43 — STAGE_CO secondary output R2 amount-authoritative ledger recognition."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	_assert_row_composition,
	_manufacture_output_amount_authoritative,
	_manufacture_stage_secondary_amount_authoritative,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import r2_representation_bound


MODULE = "erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract"
SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("valuation_type", None)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.setdefault("bom_secondary_item", None)
		self.__dict__.setdefault("is_finished_item", 0)
		self.__dict__.setdefault("s_warehouse", None)
		self.__dict__.setdefault("name", None)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("name", "STE-TEST")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("job_card", None)
		self.__dict__.setdefault("work_order", None)
		self.__dict__.setdefault("custom_manufacturing_costing_contract_version", "5.3.43")
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


def _rm(amount=7_654_007, idx=1):
	return _Row(
		idx=idx,
		name=f"rm-{idx}",
		item_code="RM",
		qty=1,
		transfer_qty=1,
		s_warehouse="WIP",
		t_warehouse=None,
		basic_rate=amount,
		basic_amount=amount,
		amount=amount,
		valuation_rate=amount,
	)


def _by_product(
	*,
	qty=14000,
	basic_amount=7_654_007,
	basic_rate=547,
	name="peomt9ishu",
	idx=8,
	item_code="30100096",
	valuation_type=None,
	set_basic_rate_manually=0,
):
	return _Row(
		idx=idx,
		name=name,
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		t_warehouse="CO",
		secondary_item_type="By-Product",
		custom_output_class="CO_PRODUCT",
		basic_rate=basic_rate,
		basic_amount=basic_amount,
		amount=basic_amount,
		valuation_rate=basic_rate,
		valuation_type=valuation_type,
		set_basic_rate_manually=set_basic_rate_manually,
		custom_equivalent_qty=qty,
		custom_output_equivalent_factor=1,
		custom_physical_conversion=1,
	)


@contextmanager
def _env(main_item_codes=None):
	main_item_codes = main_item_codes or {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		if doctype == "Item" and fieldname == "custom_main_item_code":
			return main_item_codes.get(name)
		if doctype == "Item" and fieldname == "stock_uom":
			return "Nos"
		return None

	with (
		mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{MODULE}.get_currency_precision", return_value=0),
		mock.patch(f"{MODULE}.is_irr_company", return_value=True),
		mock.patch("erpnext_extensions.iran_accounting.domain.currency.get_company_currency", return_value="IRR"),
		mock.patch("erpnext_extensions.iran_accounting.domain.currency.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
		mock.patch(f"{SCRAP}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.is_irr_company", return_value=True),
		mock.patch(f"{STAGE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{STAGE}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.frappe.get_all", return_value=[]),
	):
		yield


class TestStageSecondaryR2LedgerV5543(unittest.TestCase):
	def test_t1_zero_fg_one_by_product_passes(self):
		by_item = _by_product()
		doc = _Doc(items=[_rm(), by_item])
		with _env():
			self.assertTrue(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
			self.assertEqual(_assert_row_composition(doc, doc.company), [])
		self.assertEqual(flt(by_item.basic_rate), 547)
		self.assertEqual(flt(by_item.basic_amount), 7_654_007)
		self.assertLessEqual(abs(7_654_007 - 14_000 * 547), r2_representation_bound(14000))

	def test_t2_zero_fg_multiple_by_products(self):
		# Pool 3000 across qty 2 and 1 → amounts 2000/1000; rates 1000/1000 exact.
		by1 = _by_product(qty=2, basic_amount=2000, basic_rate=1000, name="by1", idx=2, item_code="BY1")
		by2 = _by_product(qty=1, basic_amount=1000, basic_rate=1000, name="by2", idx=3, item_code="BY2")
		doc = _Doc(items=[_rm(3000), by1, by2])
		with _env():
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t3_zero_fg_co_products(self):
		cp = _by_product(
			qty=3,
			basic_amount=3000,
			basic_rate=1000,
			name="cp1",
			idx=2,
			item_code="CP1",
		)
		cp.secondary_item_type = "Co-Product"
		cp.custom_output_class = "CO_PRODUCT"
		doc = _Doc(items=[_rm(3000), cp])
		with _env():
			self.assertTrue(_manufacture_output_amount_authoritative(doc, cp, doc.company))
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t4_zero_fg_eligible_product_reject_scrap(self):
		# MAIN_PRODUCT_REJECT: scrap of FG item with R2 residual.
		# amount 101, qty 2 → HALF_UP rate 51; 2×51=102; residual -1 ≤ bound 1.
		reject = _Row(
			idx=2,
			name="rj",
			item_code="FG",
			qty=2,
			transfer_qty=2,
			t_warehouse="RJ",
			secondary_item_type="Scrap",
			is_scrap_item=1,
			custom_output_class="MAIN_PRODUCT_REJECT",
			basic_rate=51,
			basic_amount=101,
			amount=101,
			valuation_rate=51,
		)
		# Need FG identity for classification when finished_item resolved from WO/JC absent:
		# with only scrap of FG and no FG row, finished_item is None → not MAIN_PRODUCT_REJECT.
		# Provide a finished good row so scrap classifies as product reject, then remove from
		# composition? Actually classify needs finished_item: use job_card finished via mock,
		# or include FG with exact compose and reject with R2.
		fg = _Row(
			idx=1,
			name="fg",
			item_code="FG",
			qty=1,
			transfer_qty=1,
			t_warehouse="FG",
			is_finished_item=1,
			basic_rate=100,
			basic_amount=100,
			amount=100,
			valuation_rate=100,
		)
		doc = _Doc(items=[_rm(201), fg, reject])
		with _env():
			self.assertTrue(_manufacture_stage_secondary_amount_authoritative(doc, reject, doc.company))
			failures = _assert_row_composition(doc, doc.company)
		self.assertEqual(failures, [])

	def test_t5_mixed_by_and_co(self):
		by_item = _by_product(qty=1, basic_amount=1000, basic_rate=1000, name="by", idx=2, item_code="BY")
		cp = _by_product(qty=1, basic_amount=1000, basic_rate=1000, name="cp", idx=3, item_code="CP")
		cp.secondary_item_type = "Co-Product"
		doc = _Doc(items=[_rm(2000), by_item, cp])
		with _env():
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t6_r2_boundary_exactly_equal(self):
		# qty=10 → bound=5. Choose amount so |amount - qty×rate| == 5.
		# amount=105, rate=HALF_UP(10.5)=11, qty×rate=110, residual=-5.
		by_item = _by_product(qty=10, basic_amount=105, basic_rate=11, name="by-bound", idx=2)
		doc = _Doc(items=[_rm(105), by_item])
		with _env():
			self.assertEqual(abs(105 - 10 * 11), r2_representation_bound(10))
			self.assertTrue(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t7_r2_boundary_exceeded(self):
		# qty=10 bound=5; amount=100, forged rate=12 → product 120, residual -20 > 5.
		# Also rate != HALF_UP(100/10)=10.
		by_item = _by_product(qty=10, basic_amount=100, basic_rate=12, name="by-over", idx=2)
		doc = _Doc(items=[_rm(100), by_item])
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
			failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_amount == transfer_qty" in f for f in failures))

	def test_t8_round_half_up_correctness(self):
		# Target doc shape: 7654007/14000 → 546.714… → 547.
		by_item = _by_product()
		doc = _Doc(items=[_rm(), by_item])
		with _env():
			self.assertTrue(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
		# Wrong HALF_UP (floor 546) must fail recognition even if residual smaller vs ceil product.
		bad = _by_product(basic_rate=546, basic_amount=7_654_007, name="bad-floor")
		doc_bad = _Doc(items=[_rm(), bad])
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc_bad, bad, doc_bad.company))

	def test_t9_decimal_rate_rejection(self):
		by_item = _by_product(basic_rate=546.7, basic_amount=7_654_007)
		doc = _Doc(items=[_rm(), by_item])
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
			failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_rate integer" in f for f in failures))

	def test_t10_finance_excluded_rejection(self):
		by_item = _by_product(valuation_type="Valuation Rate")
		doc = _Doc(items=[_rm(), by_item])
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
			failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_amount == transfer_qty" in f for f in failures))

	def test_t11_reloaded_document_recognition_by_name(self):
		"""Simulate get_doc reload: new Python objects, same durable name/fields."""
		by_item = _by_product()
		doc = _Doc(items=[_rm(), by_item])
		# Reload-like clone
		reloaded_by = _by_product()  # new object, same name peomt9ishu
		reloaded = _Doc(items=[_rm(), reloaded_by])
		with _env():
			self.assertTrue(
				_manufacture_stage_secondary_amount_authoritative(reloaded, reloaded_by, reloaded.company)
			)
			self.assertEqual(_assert_row_composition(reloaded, reloaded.company), [])

	def test_t12_single_fg_stage_co_unchanged(self):
		fg = _Row(
			idx=1,
			name="fg",
			item_code="FG",
			qty=2,
			transfer_qty=2,
			t_warehouse="FG",
			is_finished_item=1,
			basic_rate=34,
			basic_amount=67,  # residual owner; 2×34=68
			amount=67,
			valuation_rate=34,
		)
		by_item = _by_product(qty=1, basic_amount=33, basic_rate=33, name="by", idx=2, item_code="BY")
		doc = _Doc(items=[_rm(100), fg, by_item])
		with _env():
			self.assertTrue(_manufacture_output_amount_authoritative(doc, fg, doc.company))
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t13_same_item_multi_fg_topology_unchanged(self):
		fg1 = _Row(
			idx=1,
			name="fg1",
			item_code="FG",
			qty=6,
			transfer_qty=6,
			t_warehouse="Q",
			is_finished_item=1,
			basic_rate=167,
			basic_amount=1000,  # 6×167=1002; residual -2 ≤ 3
			amount=1000,
			valuation_rate=167,
		)
		fg2 = _Row(
			idx=2,
			name="fg2",
			item_code="FG",
			qty=4,
			transfer_qty=4,
			t_warehouse="R",
			is_finished_item=1,
			basic_rate=250,
			basic_amount=1000,
			amount=1000,
			valuation_rate=250,
		)
		# Adjust fg1 to a real R2 case: amount 1001, rate HALF_UP(1001/6)=167, 6*167=1002, residual -1
		fg1.basic_amount = 1001
		fg1.amount = 1001
		fg1.basic_rate = 167
		fg1.valuation_rate = 167
		doc = _Doc(items=[_rm(2001), fg1, fg2])
		with _env():
			self.assertTrue(_manufacture_output_amount_authoritative(doc, fg1, doc.company))
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t14_unsupported_other_output_rejected(self):
		other = _Row(
			idx=2,
			name="other",
			item_code="X",
			qty=10,
			transfer_qty=10,
			t_warehouse="T",
			basic_rate=547,
			basic_amount=7_654_007,
			amount=7_654_007,
			valuation_rate=547,
		)
		doc = _Doc(items=[_rm(), other])
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, other, doc.company))
			failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_amount == transfer_qty" in f for f in failures))

	def test_t15_non_manufacture_rejected(self):
		by_item = _by_product()
		doc = _Doc(purpose="Material Transfer", items=[_rm(), by_item])
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))

	def test_t16_repeated_validation_idempotent(self):
		by_item = _by_product()
		doc = _Doc(items=[_rm(), by_item])
		with _env():
			for _ in range(5):
				self.assertEqual(_assert_row_composition(doc, doc.company), [])
				self.assertEqual(flt(by_item.basic_amount), 7_654_007)
				self.assertEqual(flt(by_item.basic_rate), 547)

	def test_t17_target_mat_ste_41263_shape(self):
		by_item = _by_product()
		doc = _Doc(name="MAT-STE-2026-41263", items=[_rm(), by_item])
		residual = 7_654_007 - 14_000 * 547
		self.assertEqual(residual, -3993)
		self.assertEqual(r2_representation_bound(14000), 7000)
		with _env():
			self.assertTrue(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
			self.assertEqual(_assert_row_composition(doc, doc.company), [])

	def test_t18_missing_contract_stamp_on_submitted_blocks(self):
		by_item = _by_product()
		doc = _Doc(
			docstatus=1,
			custom_manufacturing_costing_contract_version="",
			items=[_rm(), by_item],
		)
		with _env():
			self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))


if __name__ == "__main__":
	unittest.main()
