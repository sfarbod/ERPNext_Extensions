# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.44 prevention-only guards — manufacturing, batch, MA remainder, returns.

Does not mutate historical operational documents. Unit tests use mocks / synthetic docs.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_entry_prevention import (
	StockEntryPreventionError,
	assert_batch_valuation_identity_before_submit,
	assert_no_duplicate_manufacture_capitalization,
	assert_no_unapproved_indivisible_ma_remainder,
	assert_source_linked_return_valuation,
	collect_fractional_sabe_failures,
	run_before_submit_prevention_guards,
	run_validate_prevention_guards,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import (
	STRATEGY_SAME_ITEM_MULTI_FG,
	apply_same_item_multi_fg,
	get_allocation_owner,
)

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"
CONTRACT = "erpnext_extensions.iran_accounting.manufacture_output_contract"
PREV = "erpnext_extensions.iran_accounting.domain.stock_entry_prevention"
QRA = "erpnext_extensions.iran_accounting.domain.qty_rate_amount"
STOCK = "erpnext_extensions.iran_accounting.stock_entry"
ROUND = "erpnext_extensions.iran_accounting.manufacture_rounding"


def _fake_throw(msg, exc=Exception, title=None, **kwargs):
	raise (exc or Exception)(str(msg))


@contextmanager
def _throwing():
	"""frappe.throw needs a bound local; raise the prevention exc directly."""
	with mock.patch(f"{PREV}.frappe.throw", side_effect=_fake_throw):
		yield


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
		self.__dict__.setdefault("batch_no", None)
		self.__dict__.setdefault("serial_and_batch_bundle", None)
		self.__dict__.setdefault("use_serial_batch_fields", 0)
		self.__dict__.setdefault("s_warehouse", None)
		self.__dict__.setdefault("t_warehouse", None)
		self.__dict__.setdefault("is_finished_item", 0)
		self.__dict__.update(fields)
		if "transfer_qty" not in self.__dict__ and "qty" in self.__dict__:
			self.transfer_qty = self.qty
		if "basic_amount" not in self.__dict__ and "basic_rate" in self.__dict__ and "qty" in self.__dict__:
			self.basic_amount = flt(self.qty) * flt(self.basic_rate)
		if "amount" not in self.__dict__ and "basic_amount" in self.__dict__:
			self.amount = self.basic_amount
		if "valuation_rate" not in self.__dict__ and "basic_rate" in self.__dict__:
			self.valuation_rate = self.basic_rate

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
		self.__dict__.setdefault("name", "NEW-SE")
		self.__dict__.setdefault("custom_is_returned", 0)
		self.__dict__.setdefault("custom_reference_no", None)
		self.__dict__.setdefault("custom_manufacturing_costing_contract_version", None)
		self.__dict__.setdefault("total_additional_costs", 0)
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.setdefault("items", [])
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


def _consumed(item, idx, amount, qty=1):
	rate = amount / qty
	return _Row(
		idx=idx,
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		s_warehouse="WIP",
		basic_rate=rate,
		basic_amount=amount,
		amount=amount,
		valuation_rate=rate,
		is_finished_item=0,
	)


def _fg(item, qty, wh, idx, rate=0, **extra):
	amt = rate * qty
	return _Row(
		idx=idx,
		item_code=item,
		qty=qty,
		transfer_qty=qty,
		t_warehouse=wh,
		basic_rate=rate,
		basic_amount=amt,
		amount=amt,
		valuation_rate=rate,
		is_finished_item=1,
		batch_no=extra.pop("batch_no", "BATCH-A"),
		**extra,
	)


@contextmanager
def _mfg_env():
	with ExitStack() as stack:
		for target, kwargs in (
			(f"{STAGE}.uses_v533_contract", {"return_value": True}),
			(f"{CONTRACT}.is_irr_company", {"return_value": True}),
			(f"{CONTRACT}.get_company_currency", {"return_value": "IRR"}),
			(f"{PREV}.is_irr_company", {"return_value": True}),
			(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				{"return_value": True},
			),
			(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				{"return_value": "IRR"},
			),
			(
				"erpnext_extensions.iran_accounting.domain.currency.get_currency_precision",
				{"return_value": 0},
			),
			(
				"frappe.get_cached_value",
				{"side_effect": lambda *a, **k: ("Nos" if a and a[0] == "Item" else None)},
			),
		):
			stack.enter_context(mock.patch(target, **kwargs))
		yield


class TestManufacturePreventionV5544(unittest.TestCase):
	"""Group A — manufacturing prevention / SAME_ITEM_MULTI_FG."""

	def test_a_same_item_multi_warehouse_batch_allocates_once(self):
		"""29742-like: 300 + 58 same item/batch, two warehouses — pool once."""
		pool = 1_815_038_712
		fg_q = _fg("20100094", 300, "Quarantine", 2, rate=0, batch_no="6163")
		fg_r = _fg("20100094", 58, "Retain", 3, rate=0, batch_no="6163")
		# Simulate Core double-capital rates before Iran contract.
		fg_q.basic_rate = fg_q.valuation_rate = pool / 300
		fg_q.basic_amount = fg_q.amount = pool
		fg_r.basic_rate = fg_r.valuation_rate = pool / 58
		fg_r.basic_amount = fg_r.amount = pool
		doc = _Doc(items=[_consumed("30300051", 1, pool, qty=708), fg_q, fg_r])
		with _mfg_env():
			self.assertTrue(apply_same_item_multi_fg(doc))
			run_validate_prevention_guards(doc)
		self.assertEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)
		incoming = flt(fg_q.basic_amount) + flt(fg_r.basic_amount)
		self.assertAlmostEqual(incoming, pool, places=0)
		self.assertLess(flt(fg_q.basic_amount), pool * 0.95)
		self.assertLess(flt(fg_r.basic_amount), pool * 0.95)

	def test_a_double_capital_detector_blocks_if_contract_skipped(self):
		pool = 1_000_000
		fg1 = _fg("FG", 300, "Q", 2, rate=pool / 300)
		fg2 = _fg("FG", 58, "R", 3, rate=pool / 58)
		fg1.amount = fg1.basic_amount = pool
		fg2.amount = fg2.basic_amount = pool
		doc = _Doc(items=[_consumed("RM", 1, pool), fg1, fg2])
		with _throwing(), mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{STAGE}.uses_v533_contract", return_value=True
		):
			with self.assertRaises(Exception) as ctx:
				assert_no_duplicate_manufacture_capitalization(doc)
		self.assertIn("Duplicate capitalization", str(ctx.exception))

	def test_a_historical_unstamped_skipped(self):
		pool = 1_000_000
		fg1 = _fg("FG", 300, "Q", 2, rate=pool / 300)
		fg2 = _fg("FG", 58, "R", 3, rate=pool / 58)
		fg1.amount = fg1.basic_amount = pool
		fg2.amount = fg2.basic_amount = pool
		doc = _Doc(docstatus=1, items=[_consumed("RM", 1, pool), fg1, fg2])
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{STAGE}.uses_v533_contract", return_value=False
		):
			assert_no_duplicate_manufacture_capitalization(doc)  # no throw


class TestBatchPreventionV5544(unittest.TestCase):
	"""Group B — batch valuation path."""

	def test_b_use_serial_batch_fields_allows_core_bundle_path(self):
		row = _Row(
			idx=1,
			item_code="20100094",
			batch_no="6163",
			qty=4,
			s_warehouse="Q",
			use_serial_batch_fields=1,
			basic_rate=100,
			amount=400,
		)
		doc = _Doc(purpose="Material Issue", items=[row])
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}._batchwise_required", return_value=True
		), mock.patch(f"{PREV}.frappe.db.get_single_value", return_value=0):
			assert_batch_valuation_identity_before_submit(doc)

	def test_b_outward_without_bundle_or_fields_blocked(self):
		row = _Row(
			idx=1,
			item_code="20100094",
			batch_no="6163",
			qty=4,
			s_warehouse="Q",
			use_serial_batch_fields=0,
			serial_and_batch_bundle=None,
			basic_rate=100,
			amount=400,
		)
		doc = _Doc(purpose="Material Issue", items=[row])
		with _throwing(), mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}._batchwise_required", return_value=True
		), mock.patch(f"{PREV}.frappe.db.get_single_value", return_value=0):
			with self.assertRaises(Exception) as ctx:
				assert_batch_valuation_identity_before_submit(doc)
		self.assertIn("batchwise valuation", str(ctx.exception).lower())

	def test_b_fractional_sabe_detected(self):
		def _get_all(doctype, *args, **kwargs):
			if doctype == "Stock Ledger Entry":
				return ["bundle-1"]
			if doctype == "Serial and Batch Entry":
				return [
					frappe._dict(
						name="sabe-1",
						parent="bundle-1",
						batch_no="6163",
						qty=-4,
						incoming_rate=6030552.368421053,
						stock_value_difference=-24122209.473684210,
						warehouse="Q",
					)
				]
			return []

		import frappe

		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}.frappe.get_all", side_effect=_get_all
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			fails = collect_fractional_sabe_failures("SE-1", "Co")
		self.assertTrue(any("fractional" in f for f in fails))


class TestIntegerIRRPreventionV5544(unittest.TestCase):
	"""Group C — indivisible MA remainder."""

	def test_c_full_depletion_indivisible_remainder_blocked(self):
		row = _Row(
			idx=1,
			item_code="20100094",
			qty=300,
			transfer_qty=300,
			s_warehouse="Quarantine",
			batch_no=None,
			basic_rate=6030552,
			amount=1809165600,
		)
		doc = _Doc(purpose="Material Transfer", items=[row])
		with _throwing(), mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}._through_riv", return_value=False
		), mock.patch(
			f"{PREV}._warehouse_balance", return_value=(300.0, 1_809_165_711.0)
		), mock.patch(f"{PREV}._batchwise_required", return_value=False):
			with self.assertRaises(Exception) as ctx:
				assert_no_unapproved_indivisible_ma_remainder(doc)
		msg = str(ctx.exception)
		self.assertIn("indivisible Moving Average remainder", msg)
		self.assertIn("111", msg)

	def test_c_partial_issue_not_blocked(self):
		row = _Row(
			idx=1,
			item_code="20100094",
			qty=4,
			transfer_qty=4,
			s_warehouse="Quarantine",
			basic_rate=6030552,
			amount=24122208,
		)
		doc = _Doc(purpose="Material Issue", items=[row])
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}._through_riv", return_value=False
		), mock.patch(
			f"{PREV}._warehouse_balance", return_value=(300.0, 1_809_165_711.0)
		), mock.patch(f"{PREV}._batchwise_required", return_value=False):
			assert_no_unapproved_indivisible_ma_remainder(doc)

	def test_c_exact_divisible_depletion_ok(self):
		row = _Row(
			idx=1,
			item_code="X",
			qty=10,
			transfer_qty=10,
			s_warehouse="W",
			basic_rate=100,
			amount=1000,
		)
		doc = _Doc(purpose="Material Issue", items=[row])
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}._through_riv", return_value=False
		), mock.patch(f"{PREV}._warehouse_balance", return_value=(10.0, 1000.0)), mock.patch(
			f"{PREV}._batchwise_required", return_value=False
		):
			assert_no_unapproved_indivisible_ma_remainder(doc)


class TestReturnPreventionV5544(unittest.TestCase):
	"""Group D — source-linked returns."""

	def test_d_valid_matching_return_ok(self):
		src_row = _Row(
			idx=5,
			item_code="20100094",
			batch_no="6163",
			qty=4,
			s_warehouse="Retain",
			basic_rate=31_293_771,
			valuation_rate=31_293_771,
			amount=125_175_084,
		)
		source = _Doc(name="MAT-STE-SRC", docstatus=1, purpose="Material Issue", items=[src_row])
		ret_row = _Row(
			idx=5,
			item_code="20100094",
			batch_no="6163",
			qty=4,
			t_warehouse="Quarantine",
			basic_rate=31_293_771,
			valuation_rate=31_293_771,
			amount=125_175_084,
		)
		doc = _Doc(
			purpose="Material Receipt",
			custom_is_returned=1,
			custom_reference_no="MAT-STE-SRC",
			items=[ret_row],
		)
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}.frappe.db.exists", return_value=True
		), mock.patch(f"{PREV}.frappe.get_doc", return_value=source), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			assert_source_linked_return_valuation(doc)

	def test_d_diverging_return_rate_blocked(self):
		src_row = _Row(
			idx=5,
			item_code="20100094",
			batch_no="6163",
			qty=4,
			s_warehouse="Retain",
			basic_rate=31_293_771,
			valuation_rate=31_293_771,
			amount=125_175_084,
		)
		source = _Doc(name="MAT-STE-SRC", docstatus=1, purpose="Material Issue", items=[src_row])
		ret_row = _Row(
			idx=5,
			item_code="20100094",
			batch_no="6163",
			qty=4,
			t_warehouse="Quarantine",
			basic_rate=4_562_305,
			valuation_rate=4_562_305,
			amount=18_249_220,
		)
		doc = _Doc(
			purpose="Material Receipt",
			custom_is_returned=1,
			custom_reference_no="MAT-STE-SRC",
			items=[ret_row],
		)
		with _throwing(), mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}.frappe.db.exists", return_value=True
		), mock.patch(f"{PREV}.frappe.get_doc", return_value=source), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			with self.assertRaises(Exception) as ctx:
				assert_source_linked_return_valuation(doc)
		self.assertIn("does not match source", str(ctx.exception))

	def test_d_ordinary_receipt_untouched(self):
		doc = _Doc(
			purpose="Material Receipt",
			custom_is_returned=0,
			items=[_fg("X", 1, "W", 1, rate=100)],
		)
		with mock.patch(f"{PREV}.is_irr_company", return_value=True):
			assert_source_linked_return_valuation(doc)

	def test_d_missing_source_blocked(self):
		doc = _Doc(
			purpose="Material Receipt",
			custom_is_returned=1,
			custom_reference_no="MISSING",
			items=[_fg("X", 1, "W", 1, rate=100)],
		)
		with _throwing(), mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}.frappe.db.exists", return_value=False
		):
			with self.assertRaises(Exception):
				assert_source_linked_return_valuation(doc)


class TestRegressionHooksV5544(unittest.TestCase):
	"""Group E — orchestrators and historical skip."""

	def test_e_before_submit_orchestrator_calls_guards(self):
		doc = _Doc(purpose="Material Issue", items=[])
		with mock.patch(f"{PREV}.assert_batch_valuation_identity_before_submit") as a, mock.patch(
			f"{PREV}.assert_source_linked_return_valuation"
		) as b, mock.patch(f"{PREV}.assert_no_unapproved_indivisible_ma_remainder") as c:
			run_before_submit_prevention_guards(doc)
			a.assert_called_once_with(doc)
			b.assert_called_once_with(doc)
			c.assert_called_once_with(doc)

	def test_e_riv_skips_indivisible_guard(self):
		row = _Row(idx=1, item_code="X", qty=300, s_warehouse="W", amount=1)
		doc = _Doc(purpose="Material Issue", items=[row])
		with mock.patch(f"{PREV}.is_irr_company", return_value=True), mock.patch(
			f"{PREV}._through_riv", return_value=True
		):
			assert_no_unapproved_indivisible_ma_remainder(doc)


if __name__ == "__main__":
	unittest.main()
