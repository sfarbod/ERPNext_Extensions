# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture IRR residual classifier — TYPE A / B / C (v5.3.34)."""

from __future__ import annotations

import unittest
from math import floor
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
	CLASS_INVALID,
	CLASS_NONE,
	CLASS_TYPE_A,
	CLASS_TYPE_B,
	CLASS_TYPE_C,
	classify_manufacture_irr_residual,
	integer_qtys_gcd,
	type_c_residual_bound,
	uses_type_c_sa_residual_policy,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	_integer_rate_pair,
	allocate_scrap_absorbed_cost,
)

MODULE = "erpnext_extensions.iran_accounting.domain.manufacture_irr_residual"
SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _consumed(item, qty, rate):
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		s_warehouse="WIP",
		t_warehouse=None,
		is_finished_item=0,
	)


def _fg(item, qty, rate, additional_cost=0):
	ba = qty * rate
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=ba,
		additional_cost=additional_cost,
		amount=ba + additional_cost,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse="FG-WH",
		is_finished_item=1,
	)


def _reject(item, qty, rate):
	ba = qty * rate
	return _Row(
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=ba,
		amount=ba,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse="Reject-WH",
		is_finished_item=0,
		secondary_item_type="Scrap",
		type="Scrap",
	)


def _irr_patches():
	return (
		mock.patch(f"{MODULE}.is_irr_company", return_value=True),
		mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{MODULE}.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
		mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.frappe.db.get_value", return_value=None),
	)


class TestTypeCBoundMath(unittest.TestCase):
	def test_gcd_and_bound(self):
		self.assertEqual(integer_qtys_gcd([5698, 2]), 2)
		self.assertEqual(type_c_residual_bound([5698, 2]), 1.0)
		self.assertEqual(integer_qtys_gcd([650, 100]), 50)
		self.assertEqual(type_c_residual_bound([650, 100]), 25.0)
		self.assertEqual(integer_qtys_gcd([10]), 10)
		self.assertEqual(type_c_residual_bound([10]), 5.0)

	def test_pair_matches_bound_for_37762(self):
		pair = _integer_rate_pair(2045815423, 5698, 2)
		self.assertIsNotNone(pair)
		_gr, _sr, leftover = pair
		self.assertEqual(leftover, 1.0)
		self.assertLessEqual(abs(leftover), type_c_residual_bound([5698, 2]))


class TestClassifier(unittest.TestCase):
	def test_exact_pool_none(self):
		# 10×100 + 0 reject — exact
		doc = _Doc(
			items=[
				_consumed("RM", 10, 100),
				_fg("FG", 10, 100),
			]
		)
		with _irr_patches()[0], _irr_patches()[1], _irr_patches()[2], _irr_patches()[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_NONE)

	def test_single_fg_type_b(self):
		# pool 1003, qty 10 → rate 100, basic 1000, amount 1003 residual in amount
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1003),
				_fg("FG", 10, 100),
			]
		)
		fg = doc.items[1]
		fg.basic_amount = 1003
		fg.amount = 1003
		fg.basic_rate = 100
		with _irr_patches()[0], _irr_patches()[1], _irr_patches()[2], _irr_patches()[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_B)
		self.assertEqual(result.residual, 3.0)

	def test_type_c_37762_class(self):
		pair = _integer_rate_pair(2045815423, 5698, 2)
		gr, sr, leftover = pair
		doc = _Doc(
			items=[
				_consumed("RM", 1, 2045815423),
				_fg("30100023", 5698, gr),
				_reject("30100023", 2, sr),
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_C)
		self.assertEqual(result.residual, leftover)
		self.assertEqual(result.mathematical_bound, 1.0)
		self.assertEqual(result.gcd, 2.0)

	def test_type_c_negative_residual(self):
		# Force rates that overshoot pool by 1 within bound
		# gcd(10,2)=2, bound=1; pool 1000; 10*99 + 2*6 = 990+12=1002 → residual -2 outside?
		# 10*100 + 2*0 invalid. Use pair search.
		pair = _integer_rate_pair(1001, 10, 2)
		self.assertIsNotNone(pair)
		gr, sr, leftover = pair
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1001),
				_fg("FG", 10, gr),
				_reject("FG", 2, sr),
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		if leftover == 0:
			self.assertIn(result.classification, (CLASS_NONE, CLASS_TYPE_A))
		else:
			self.assertEqual(result.classification, CLASS_TYPE_C)
			self.assertEqual(result.residual, leftover)
			self.assertLessEqual(abs(leftover), result.mathematical_bound)

	def test_outside_bound_invalid(self):
		# Manually set rates so residual exceeds bound
		doc = _Doc(
			items=[
				_consumed("RM", 1, 2045815423),
				_fg("30100023", 5698, 358915),
				_reject("30100023", 2, 358876),
			]
		)
		# Corrupt FG basic down by 5 → residual becomes 6 > bound 1
		doc.items[1].basic_amount = 2045097670 - 5
		doc.items[1].amount = 2045097670 - 5
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_INVALID)
		self.assertEqual(result.reason, "basic_amount_not_qty_times_rate")

	def test_residual_outside_bound_after_valid_composition(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000000),
				_fg("FG", 650, 1000),
				_reject("FG", 100, 1000),
			]
		)
		# composed = 750000; pool=1000000; residual=250000 >> bound 25
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_INVALID)
		self.assertEqual(result.reason, "residual_outside_mathematical_bound")

	def test_header_additional_cost_type_a_when_exact(self):
		doc = _Doc(
			items=[
				_consumed("RM", 10, 100),
				_fg("FG", 10, 100, additional_cost=50),
			],
			additional_costs=[_Row(amount=50, base_amount=50)],
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_A)

	def test_policy_legacy_submitted_53(self):
		doc = _Doc(docstatus=1, name="SE-LEGACY", custom_manufacturing_costing_contract_version="5.3.3")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.frappe.db.get_value",
			return_value=1,
		):
			self.assertFalse(uses_type_c_sa_residual_policy(doc))

	def test_policy_draft_true(self):
		doc = _Doc(docstatus=0, name="SE-DRAFT", custom_manufacturing_costing_contract_version="5.3.3")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.frappe.db.get_value",
			return_value=0,
		):
			# Draft even if stamped 5.3.3 — upgrades to TYPE C on apply.
			self.assertTrue(uses_type_c_sa_residual_policy(doc))

	def test_policy_submit_in_progress_upgrades(self):
		"""In-memory docstatus=1 during submit, but DB still draft → TYPE C."""
		doc = _Doc(docstatus=1, name="SE-DRAFT", custom_manufacturing_costing_contract_version="5.3.3")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.frappe.db.get_value",
			return_value=0,
		):
			self.assertTrue(uses_type_c_sa_residual_policy(doc))

	def test_policy_submitted_534(self):
		doc = _Doc(docstatus=1, name="SE-NEW", custom_manufacturing_costing_contract_version="5.3.34")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.frappe.db.get_value",
			return_value=1,
		):
			self.assertTrue(uses_type_c_sa_residual_policy(doc))

	def test_allocate_type_c_idempotent_no_ac_drift(self):
		pair = _integer_rate_pair(2045815423, 5698, 2)
		gr, sr, leftover = pair
		# Build consume rows that sum to pool (simplified single consume).
		doc = _Doc(
			items=[
				_consumed("RM", 1, 2045815423),
				_fg("30100023", 5698, gr),
				_reject("30100023", 2, sr),
			]
		)
		# Seed phantom leftover in ac as if prior buggy run occurred.
		doc.items[1].additional_cost = leftover
		doc.items[1].amount = doc.items[1].basic_amount + leftover
		patches = (
			mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
			mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
			mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
			mock.patch(f"{SCRAP}.frappe.db.get_value", return_value=None),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.is_irr_company",
				return_value=True,
			),
		)
		with patches[0], patches[1], patches[2], patches[3], patches[4]:
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
			fg_amt_1 = flt(doc.items[1].amount)
			fg_ac_1 = flt(doc.items[1].additional_cost)
			self.assertEqual(fg_ac_1, 0.0)
			self.assertEqual(fg_amt_1, 5698 * gr)
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
			self.assertEqual(flt(doc.items[1].amount), fg_amt_1)
			self.assertEqual(flt(doc.items[1].additional_cost), 0.0)
			self.assertEqual(flt(doc.items[2].amount), 2 * sr)


class TestClassifierExtrasForCoverage(unittest.TestCase):
	def test_result_helpers_and_non_manufacture(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			ManufactureIrrResidualResult,
			proven_type_c_residual,
		)

		r = ManufactureIrrResidualResult(CLASS_TYPE_C, residual=1.0)
		self.assertTrue(r.is_type_c)
		self.assertFalse(r.is_invalid)
		self.assertEqual(r.to_dict()["classification"], CLASS_TYPE_C)
		inv = ManufactureIrrResidualResult(CLASS_INVALID)
		self.assertTrue(inv.is_invalid)

		doc = _Doc(doctype="Purchase Receipt", purpose="Purchase Receipt")
		self.assertFalse(uses_type_c_sa_residual_policy(doc))
		doc2 = _Doc(purpose="Material Transfer")
		self.assertFalse(uses_type_c_sa_residual_policy(doc2))
		with _irr_patches()[0], _irr_patches()[1], _irr_patches()[2]:
			self.assertEqual(classify_manufacture_irr_residual(doc2).classification, CLASS_NONE)

	def test_non_irr_and_precision(self):
		doc = _Doc(items=[_consumed("RM", 1, 100), _fg("FG", 1, 100)])
		with mock.patch(f"{MODULE}.is_irr_company", return_value=False):
			self.assertEqual(classify_manufacture_irr_residual(doc).classification, CLASS_NONE)
		with mock.patch(f"{MODULE}.is_irr_company", return_value=True), mock.patch(
			f"{MODULE}.get_company_currency", return_value="IRR"
		), mock.patch(f"{MODULE}.get_currency_precision", return_value=2):
			self.assertEqual(classify_manufacture_irr_residual(doc).reason, "non_whole_irr_precision")

	def test_version_at_least_and_empty_gcd(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			_version_at_least,
			integer_qtys_gcd,
			type_c_residual_bound,
			proven_type_c_residual,
		)

		self.assertTrue(_version_at_least("5.3.34", "5.3.34"))
		self.assertFalse(_version_at_least("5.3.3", "5.3.34"))
		self.assertFalse(_version_at_least("abc", "5.3.34"))
		self.assertEqual(integer_qtys_gcd([]), 0)
		self.assertEqual(integer_qtys_gcd([0, -1]), 0)
		self.assertEqual(integer_qtys_gcd([1.5, 2]), 0)
		self.assertEqual(type_c_residual_bound([]), 0.0)
		doc = _Doc(docstatus=1, name="X", custom_manufacturing_costing_contract_version="5.3.3")
		with mock.patch(f"{MODULE}.frappe.db.get_value", return_value=1):
			self.assertIsNone(proven_type_c_residual(doc))

	def test_negative_amount_invalid(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 100),
				_reject("FG", 2, 50),
			]
		)
		doc.items[1].amount = -1
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_INVALID)
		self.assertEqual(result.reason, "negative_row_amount")

	def test_non_integer_rate_invalid(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 100.5),
				_reject("FG", 2, 50),
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_INVALID)
		self.assertEqual(result.reason, "non_integer_output_rate")

	def test_multi_fg_unsupported(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 2000),
				_fg("FG1", 10, 100),
				_fg("FG2", 10, 100),
			]
		)
		# two finished goods
		doc.items[2].is_finished_item = 1
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.reason, "multi_fg_or_missing_fg_unsupported_for_auto_type_c")

	def test_submitted_unstamped_policy_false(self):
		doc = _Doc(docstatus=1, name="SE-HIST", custom_manufacturing_costing_contract_version="")
		with mock.patch(f"{MODULE}.frappe.db.get_value", return_value=1):
			self.assertFalse(uses_type_c_sa_residual_policy(doc))

	def test_persisted_docstatus_islocal(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			_persisted_docstatus,
		)

		doc = _Doc(__islocal=1, name="New")
		self.assertEqual(_persisted_docstatus(doc), 0)
		doc2 = _Doc(name=None)
		self.assertEqual(_persisted_docstatus(doc2), 0)

	def test_persisted_docstatus_db_exception(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			_persisted_docstatus,
		)

		doc = _Doc(name="SE-X", docstatus=0)
		with mock.patch(
			f"{MODULE}.frappe.db.get_value", side_effect=Exception("db down")
		):
			self.assertEqual(_persisted_docstatus(doc), 0)

	def test_transfer_qty_fallback_and_negative_rate(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 100),
				_reject("FG", 2, 50),
			]
		)
		doc.items[1].transfer_qty = None
		doc.items[1].basic_rate = -1
		doc.items[1].basic_amount = -10
		doc.items[1].amount = -10
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_INVALID)

	def test_basic_amount_mismatch_invalid(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 100),
				_reject("FG", 2, 50),
			]
		)
		doc.items[1].basic_amount = 999  # not qty×rate
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.reason, "basic_amount_not_qty_times_rate")

	def test_type_c_exact_with_header_is_type_a(self):
		# exact composition + real header → TYPE A
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1200),
				_fg("FG", 10, 100),
				_reject("FG", 2, 100),
			],
			additional_costs=[_Row(amount=50, base_amount=50)],
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_A)

	def test_single_fg_unexplained_gap_invalid(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 90),  # 900 vs pool 1000
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_INVALID)
		self.assertEqual(result.reason, "single_fg_unexplained_pool_gap")

	def test_single_fg_exact_none(self):
		doc = _Doc(items=[_consumed("RM", 1, 1000), _fg("FG", 10, 100)])
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_NONE)

	def test_proven_type_c_residual_happy_path(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			proven_type_c_residual,
		)

		pair = _integer_rate_pair(2045815423, 5698, 2)
		gr, sr, leftover = pair
		doc = _Doc(
			name="SE-NEW",
			docstatus=0,
			custom_manufacturing_costing_contract_version="5.3.34",
			items=[
				_consumed("RM", 1, 2045815423),
				_fg("FG", 5698, gr),
				_reject("FG", 2, sr),
			],
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			self.assertEqual(proven_type_c_residual(doc), float(leftover))

	def test_submitted_534_policy_true(self):
		doc = _Doc(
			docstatus=1,
			name="SE-NEW",
			custom_manufacturing_costing_contract_version="5.3.34",
		)
		with mock.patch(f"{MODULE}.frappe.db.get_value", return_value=1):
			self.assertTrue(uses_type_c_sa_residual_policy(doc))
		doc2 = _Doc(
			docstatus=1,
			name="SE-NEW2",
			custom_manufacturing_costing_contract_version="5.3.99",
		)
		with mock.patch(f"{MODULE}.frappe.db.get_value", return_value=1):
			self.assertTrue(uses_type_c_sa_residual_policy(doc2))

	def test_gcd_early_return_one(self):
		self.assertEqual(integer_qtys_gcd([3, 5]), 1)

	def test_row_qty_empty_transfer_fallback(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import _row_qty

		row = _Row(transfer_qty="", qty=7)
		self.assertEqual(_row_qty(row), 7.0)

	def test_negative_basic_rate_only(self):
		doc = _Doc(items=[_consumed("RM", 1, 1000), _fg("FG", 10, 100)])
		doc.items[1].basic_rate = -5
		doc.items[1].basic_amount = 1000
		doc.items[1].amount = 1000
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.reason, "negative_basic_rate")

	def test_component_scrap_source_mismatch(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 90),
			]
		)
		classified = {
			"MAIN_FG": [doc.items[1]],
			"MAIN_PRODUCT_REJECT": [],
			"CO_PRODUCT": [],
			"CO_PRODUCT_REJECT": [],
			"COMPONENT_SCRAP": [_Row(item_code="RM", basic_rate=1)],
			"OTHER_OUTPUT": [],
		}
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6], mock.patch(
			f"{SCRAP}.classify_manufacture_outputs", return_value=classified
		), mock.patch(f"{SCRAP}._component_scrap_matches_issued_rate", return_value=False):
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.reason, "component_scrap_source_mismatch")

	def test_non_positive_and_fractional_output_qty(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 100),
				_reject("FG", 0, 50),
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.reason, "non_positive_output_qty")

		doc2 = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10.5, 100),
				_reject("FG", 2, 50),
			]
		)
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result2 = classify_manufacture_irr_residual(doc2)
		self.assertEqual(result2.reason, "non_integer_output_qty")

	def test_exact_composition_none(self):
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1200),
				_fg("FG", 10, 100),
				_reject("FG", 2, 100),
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_NONE)
		self.assertEqual(result.reason, "exact_composition")

	def test_type_c_with_phantom_additional_cost_evidence(self):
		pair = _integer_rate_pair(2045815423, 5698, 2)
		gr, sr, leftover = pair
		doc = _Doc(
			items=[
				_consumed("RM", 1, 2045815423),
				_fg("FG", 5698, gr, additional_cost=leftover),
				_reject("FG", 2, sr),
			]
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_C)
		self.assertIn("phantom_additional_cost_detected", result.evidence)

	def test_fg_qty_non_positive(self):
		doc = _Doc(items=[_consumed("RM", 1, 1000), _fg("FG", 0, 100)])
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.reason, "fg_qty_non_positive")

	def test_proven_type_c_none_when_not_type_c(self):
		from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
			proven_type_c_residual,
		)

		doc = _Doc(items=[_consumed("RM", 1, 1000), _fg("FG", 10, 100)])
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			self.assertIsNone(proven_type_c_residual(doc))

	def test_component_scrap_match_continues(self):
		"""COMPONENT_SCRAP present and issued-rate match → continue past integrity gate."""
		scrap = _Row(
			item_code="RM",
			qty=1,
			transfer_qty=1,
			basic_rate=100,
			basic_amount=100,
			amount=100,
			s_warehouse=None,
			t_warehouse="Scrap-WH",
			is_finished_item=0,
			secondary_item_type="Scrap",
			type="Scrap",
		)
		doc = _Doc(
			items=[
				_consumed("RM", 1, 1000),
				_fg("FG", 10, 90),
				scrap,
			]
		)
		classified = {
			"MAIN_FG": [doc.items[1]],
			"MAIN_PRODUCT_REJECT": [],
			"CO_PRODUCT": [],
			"CO_PRODUCT_REJECT": [],
			"COMPONENT_SCRAP": [scrap],
			"OTHER_OUTPUT": [],
		}
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6], mock.patch(
			f"{SCRAP}.classify_manufacture_outputs", return_value=classified
		), mock.patch(f"{SCRAP}._component_scrap_matches_issued_rate", return_value=True):
			result = classify_manufacture_irr_residual(doc)
		# FG 900 + scrap 100 = outgoing 1000 → exact/TYPE B path with residual_vs_rate=0 → NONE
		self.assertIn(result.classification, (CLASS_NONE, CLASS_TYPE_B))
		self.assertNotEqual(result.reason, "component_scrap_source_mismatch")

	def test_type_c_legacy_policy_skips_phantom_flag(self):
		"""Submitted 5.3.3 stamp: TYPE C economics still classified, no phantom flag gate."""
		pair = _integer_rate_pair(2045815423, 5698, 2)
		gr, sr, leftover = pair
		doc = _Doc(
			name="SE-LEG",
			docstatus=1,
			custom_manufacturing_costing_contract_version="5.3.3",
			items=[
				_consumed("RM", 1, 2045815423),
				_fg("FG", 5698, gr, additional_cost=leftover),
				_reject("FG", 2, sr),
			],
		)
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], mock.patch(
			f"{MODULE}.frappe.db.get_value", return_value=1
		):
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_TYPE_C)
		self.assertNotIn("phantom_additional_cost_detected", result.evidence)

	def test_missing_fg_none(self):
		doc = _Doc(items=[_consumed("RM", 1, 1000)])
		patches = _irr_patches()
		with patches[0], patches[1], patches[2], patches[6]:
			result = classify_manufacture_irr_residual(doc)
		self.assertEqual(result.classification, CLASS_NONE)
		self.assertEqual(result.reason, "multi_fg_or_missing_fg_unsupported_for_auto_type_c")


class TestTypeCWithRealAdditionalCost(unittest.TestCase):
	def test_header_add_and_type_c_separated(self):
		pair = _integer_rate_pair(10000, 10, 2)
		gr, sr, leftover = pair
		doc = _Doc(
			items=[
				_consumed("RM", 1, 10000),
				_fg("FG", 10, gr),
				_reject("FG", 2, sr),
			],
			additional_costs=[_Row(amount=100, base_amount=100)],
		)
		patches = (
			mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
			mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
			mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
			mock.patch(f"{SCRAP}.frappe.db.get_value", return_value=None),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.get_currency_precision",
				return_value=0,
			),
		)
		with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
			allocate_scrap_absorbed_cost(doc)
		cap = sum(flt(r.additional_cost) for r in doc.items if r.t_warehouse)
		self.assertEqual(cap, 100.0)
		composed = sum(flt(r.basic_amount) for r in doc.items if r.t_warehouse)
		self.assertEqual(composed, 10 * gr + 2 * sr)
		if leftover:
			self.assertEqual(10000 - composed, leftover)
