# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.43 — Zero MAIN_FG STAGE_CO equivalent-unit allocation."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	EQUIV_FACTOR_FIELD,
	EQUIV_QTY_FIELD,
	allocate_stage_output_cost,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	apply_iran_manufacture_output_contract,
	classify_manufacture_outputs,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	CLASS_CO_PRODUCT,
)

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


@contextmanager
def _env(main_item_codes=None, stock_uoms=None, uom_factors=None, jc_secondaries=None):
	main_item_codes = main_item_codes or {}
	stock_uoms = stock_uoms or {}
	uom_factors = uom_factors or {}
	jc_secondaries = jc_secondaries if jc_secondaries is not None else {}

	def _get_value(doctype, name, fieldname=None, **kwargs):
		if doctype == "Item" and fieldname == "custom_main_item_code":
			return main_item_codes.get(name)
		if doctype == "Item" and fieldname == "stock_uom":
			return stock_uoms.get(name)
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

	with (
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
		mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.is_irr_company", return_value=True),
		mock.patch(f"{STAGE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{STAGE}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.frappe.get_all", side_effect=_get_all),
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_output_contract.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_output_contract.get_company_currency",
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
	):
		yield


class TestZeroFgStageAllocationV5543(unittest.TestCase):
	"""T1–T15 regression matrix for zero-FG STAGE_CO."""

	def test_t1_one_fg_plus_by_product_unchanged(self):
		fg = _output("FG", 2, is_fg=1, stock_uom="Nos", idx=1, name="row-fg")
		by_item = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=2,
			name="row-by",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		fg.set(EQUIV_FACTOR_FIELD, 1)
		doc = _Doc(items=[_consumed("RM", 1, 100), fg, by_item])
		with _env(stock_uoms={"FG": "Nos", "BY": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		# Shares: BY 33 (qty1→33), FG leftover 67 (qty2→ROUND(33.5)=34 → 68).
		# Leftover share still on MAIN_FG; rate composition may leave SA residual.
		self.assertEqual(flt(by_item.basic_amount), 33)
		self.assertEqual(flt(fg.basic_amount), 68)
		self.assertEqual(flt(fg.basic_rate), 34)
		self.assertEqual(flt(by_item.basic_amount), flt(by_item.qty) * flt(by_item.basic_rate))
		self.assertEqual(flt(fg.basic_amount), flt(fg.qty) * flt(fg.basic_rate))
		self.assertEqual(flt(doc.value_difference), 1)

	def test_t2_zero_fg_one_by_product_rate_authoritative_sa_residual(self):
		by_item = _output(
			"30100096",
			14000,
			secondary_item_type="By-Product",
			stock_uom="Millilitre",
			t_warehouse="CO",
			idx=9,
			name="peomt9ishu",
			**{
				EQUIV_FACTOR_FIELD: 1,
				"custom_physical_conversion": 2.25,
				"custom_equivalent_qty": 31500,
				"custom_common_uom": "سرنگ",
				"custom_output_class": "CO_PRODUCT",
			},
		)
		doc = _Doc(items=[_consumed("RM", 1, 7_654_007), by_item])
		with _env(stock_uoms={"30100096": "Millilitre", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		# Rate-authoritative: ROUND_HALF_UP(7654007/14000)=547; amount=14000×547.
		self.assertEqual(flt(by_item.basic_rate), 547)
		self.assertEqual(flt(by_item.valuation_rate), 547)
		self.assertEqual(flt(by_item.basic_amount), 7_658_000)
		self.assertEqual(flt(by_item.amount), 7_658_000)
		self.assertEqual(flt(doc.value_difference), 3993)
		self.assertEqual(flt(by_item.basic_amount), 14_000 * 547)

	def test_t3_zero_fg_multiple_by_products_proportional(self):
		by1 = _output(
			"BY1",
			2,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="a-by1",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		by2 = _output(
			"BY2",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=2,
			name="b-by2",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 3000), by1, by2])
		with _env(stock_uoms={"BY1": "Nos", "BY2": "Nos", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(by1.basic_amount) + flt(by2.basic_amount), 3000)
		self.assertEqual(flt(by1.basic_amount), 2000)
		self.assertEqual(flt(by2.basic_amount), 1000)

	def test_t4_zero_fg_co_products(self):
		cp1 = _output(
			"CP1",
			3,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="cp1",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		cp2 = _output(
			"CP2",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=2,
			name="cp2",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 4000), cp1, cp2])
		with _env(stock_uoms={"CP1": "Nos", "CP2": "Nos", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(cp1.basic_amount) + flt(cp2.basic_amount), 4000)
		self.assertEqual(flt(cp1.basic_amount), 3000)
		self.assertEqual(flt(cp2.basic_amount), 1000)

	def test_t5_zero_fg_eligible_co_product_reject_joins_stage(self):
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="cp",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		reject = _output(
			"CP",
			1,
			secondary_item_type="Scrap",
			stock_uom="Nos",
			t_warehouse="RJ",
			idx=2,
			name="rj",
		)
		doc = _Doc(items=[_consumed("RM", 1, 2000), cp, reject])
		with _env(stock_uoms={"CP": "Nos", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(cp.basic_amount) + flt(reject.basic_amount), 2000)
		self.assertEqual(flt(cp.basic_rate), flt(reject.basic_rate))

	def test_t6_zero_fg_finance_excluded_by_product_fails_closed(self):
		by_item = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="by",
			rate=100,
			valuation_type="Valuation Rate",
		)
		doc = _Doc(items=[_consumed("RM", 1, 7_654_007), by_item])
		with _env(stock_uoms={"BY": "Nos", "RM": "Nos"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				allocate_stage_output_cost(doc)
		self.assertIn("no eligible output", str(ctx.exception).lower())
		# Independent rate untouched.
		self.assertEqual(flt(by_item.basic_amount), 100)

	def test_t7_zero_fg_mixed_eligible_outputs(self):
		cp = _output(
			"CP",
			1,
			secondary_item_type="Co-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="cp",
			**{EQUIV_FACTOR_FIELD: 2},
		)
		by_item = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=2,
			name="by",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 3000), cp, by_item])
		with _env(stock_uoms={"CP": "Nos", "BY": "Nos", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(cp.basic_amount) + flt(by_item.basic_amount), 3000)
		self.assertEqual(flt(cp.basic_amount), 2000)
		self.assertEqual(flt(by_item.basic_amount), 1000)

	def test_t8_zero_fg_no_eligible_outputs(self):
		by_item = _output(
			"BY",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			valuation_type="Manual",
			set_basic_rate_manually=1,
			rate=50,
		)
		doc = _Doc(items=[_consumed("RM", 1, 1000), by_item])
		with _env(stock_uoms={"BY": "Nos", "RM": "Nos"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				allocate_stage_output_cost(doc)
		self.assertIn(
			"Manufacturing has no eligible output to receive the production cost.",
			str(ctx.exception),
		)

	def test_t9_multiple_main_fg_still_blocked(self):
		fg1 = _output("FG", 1, is_fg=1, stock_uom="Nos", idx=1, name="fg1")
		fg2 = _output("FG", 1, is_fg=1, stock_uom="Nos", idx=2, name="fg2")
		cp = _output(
			"CP",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=3,
			name="cp",
		)
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2, cp])
		with _env(stock_uoms={"FG": "Nos", "CP": "Nos", "RM": "Nos"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				allocate_stage_output_cost(doc)
		self.assertIn("exactly one finished good", str(ctx.exception).lower())

	def test_t10_irr_rounding_residual_share_then_rate_compose(self):
		by1 = _output(
			"BY1",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="by1",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		by2 = _output(
			"BY2",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=2,
			name="by2",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		by3 = _output(
			"BY3",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=3,
			name="by3",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 100), by1, by2, by3])
		with _env(stock_uoms={"BY1": "Nos", "BY2": "Nos", "BY3": "Nos", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		total = flt(by1.basic_amount) + flt(by2.basic_amount) + flt(by3.basic_amount)
		# qty=1 → rate composition preserves integer shares; leftover share on by1.
		self.assertEqual(total, 100)
		self.assertEqual(flt(doc.value_difference), 0)
		self.assertEqual(flt(by1.basic_amount), 34)
		self.assertEqual(flt(by2.basic_amount), 33)
		self.assertEqual(flt(by3.basic_amount), 33)
		for row in (by1, by2, by3):
			self.assertEqual(flt(row.basic_amount), flt(row.qty) * flt(row.basic_rate))

	def test_t11_equal_eq_qty_deterministic_residual_by_name(self):
		# Same eq qty; residual owner must be stable name ascending ("a-row" before "z-row")
		# when sorted by (-eq, stable_key). First in that order gets residual.
		low = _output(
			"BYL",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=99,
			name="z-row",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		high_name = _output(
			"BYH",
			1,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="a-row",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 101), low, high_name])
		with _env(stock_uoms={"BYL": "Nos", "BYH": "Nos", "RM": "Nos"}):
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(high_name.basic_amount) + flt(low.basic_amount), 101)
		# Residual owner is a-row; other row takes rounded proportional share first.
		self.assertEqual(flt(low.basic_amount), 51)
		self.assertEqual(flt(high_name.basic_amount), 50)

	def test_t12_product_reject_bridge_no_regression(self):
		"""Classic MAIN_FG + MAIN_PRODUCT_REJECT still absorbs via Product Reject path."""
		fg = _output("FG", 9, is_fg=1, stock_uom="Nos", idx=1, name="fg", rate=0)
		reject = _output(
			"FG",
			1,
			secondary_item_type="Scrap",
			stock_uom="Nos",
			t_warehouse="RJ",
			idx=2,
			name="rj",
			is_scrap_item=1,
			rate=0,
		)
		doc = _Doc(items=[_consumed("RM", 1, 10000), fg, reject])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}, main_item_codes={}):
			# No participating co → STAGE_CO does not claim.
			self.assertFalse(allocate_stage_output_cost(doc))
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_MAIN_FG]), 1)
		self.assertEqual(len(classified[CLASS_MAIN_PRODUCT_REJECT]), 1)
		self.assertEqual(flt(fg.basic_amount) + flt(reject.basic_amount), 10000)
		self.assertEqual(flt(fg.basic_rate), flt(reject.basic_rate))

	def test_t13_same_item_multi_fg_not_stolen_by_stage_co(self):
		"""Multi same-item FG without participating co remains outside STAGE_CO."""
		fg1 = _output("FG", 6, is_fg=1, stock_uom="Nos", idx=1, name="fg1", t_warehouse="Q")
		fg2 = _output("FG", 4, is_fg=1, stock_uom="Nos", idx=2, name="fg2", t_warehouse="R")
		doc = _Doc(items=[_consumed("RM", 1, 1000), fg1, fg2])
		with _env(stock_uoms={"FG": "Nos", "RM": "Nos"}):
			# No participating co → STAGE_CO returns False; Multi-FG strategies own elsewhere.
			self.assertFalse(allocate_stage_output_cost(doc))
		self.assertEqual(len(classify_manufacture_outputs(doc)[CLASS_MAIN_FG]), 2)

	def test_t14_draft_and_resubmit_path_consistent(self):
		by_item = _output(
			"BY",
			10,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="by",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 7777), by_item], docstatus=0)
		with _env(stock_uoms={"BY": "Nos", "RM": "Nos"}):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			first = flt(by_item.basic_amount)
			first_vd = flt(doc.value_difference)
			doc.docstatus = 1
			doc.set("custom_manufacturing_costing_contract_version", "5.3.43")
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		# ROUND_HALF_UP(7777/10)=778 → amount 7780; SA residual +3.
		self.assertEqual(flt(by_item.basic_amount), first)
		self.assertEqual(first, 7780)
		self.assertEqual(flt(by_item.basic_rate), 778)
		self.assertEqual(first_vd, 3)
		self.assertEqual(flt(doc.value_difference), first_vd)

	def test_t15_repeated_validation_no_drift(self):
		by_item = _output(
			"BY",
			5,
			secondary_item_type="By-Product",
			stock_uom="Nos",
			t_warehouse="CO",
			idx=1,
			name="by",
			**{EQUIV_FACTOR_FIELD: 1},
		)
		doc = _Doc(items=[_consumed("RM", 1, 5555), by_item])
		with _env(stock_uoms={"BY": "Nos", "RM": "Nos"}):
			for _ in range(5):
				self.assertTrue(allocate_stage_output_cost(doc))
				self.assertEqual(flt(by_item.basic_amount), 5555)
				self.assertEqual(flt(by_item.get(EQUIV_QTY_FIELD)), 5)

	def test_target_mat_ste_41263_zero_fg_simulation(self):
		"""In-memory MAT-STE-2026-41263 without row 8 — By-Product takes full pool."""
		by_item = _output(
			"30100096",
			14000,
			secondary_item_type="By-Product",
			stock_uom="Millilitre",
			t_warehouse="انبار Quarantine محصول نیمه ساخته اسپاد",
			idx=9,
			name="peomt9ishu",
			**{
				EQUIV_FACTOR_FIELD: 1,
				"custom_physical_conversion": 2.25,
				"custom_equivalent_qty": 31500.0,
				"custom_common_uom": "سرنگ",
				"custom_output_class": "CO_PRODUCT",
			},
		)
		# Snapshot fields make equivalence complete without UOM conversion lookups.
		doc = _Doc(
			items=[
				_consumed("17100001", 11.04, 7462),
				_consumed("17100155", 12.51, 4366),
				_consumed("17100054", 57.36, 13300),
				_consumed("17000018", 7.09, 21406),
				_consumed("17000011", 80.83, 5045),
				_consumed("17100038", 4.42, 2734),
				_consumed("17100103", 293.9, 21036),
				by_item,
			]
		)
		# Fix consumed amounts to match production integers (qty*rate may float).
		amounts = [82380, 54619, 762888, 151769, 407787, 12084, 6182480]
		for row, amount in zip(doc.items[:7], amounts, strict=True):
			row.basic_amount = amount
			row.amount = amount
			row.basic_rate = amount / row.qty if row.qty else 0
		pool = sum(amounts)
		self.assertEqual(pool, 7_654_007)
		with _env(stock_uoms={"30100096": "Millilitre"}):
			classified = classify_manufacture_outputs(doc)
			self.assertEqual(len(classified[CLASS_MAIN_FG]), 0)
			self.assertEqual(len(classified[CLASS_CO_PRODUCT]), 1)
			self.assertTrue(allocate_stage_output_cost(doc))
		self.assertEqual(flt(by_item.basic_rate), 547)
		self.assertEqual(flt(by_item.basic_amount), 7_658_000)
		self.assertEqual(flt(by_item.amount), 7_658_000)
		self.assertEqual(flt(doc.value_difference), 3993)
		self.assertEqual(flt(by_item.basic_amount), 14_000 * flt(by_item.basic_rate))


if __name__ == "__main__":
	unittest.main()
