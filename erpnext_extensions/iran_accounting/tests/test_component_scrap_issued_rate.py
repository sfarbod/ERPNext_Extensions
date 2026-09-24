# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.3 Gap 1 — Component Scrap issued-rate contract."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	_component_scrap_matches_issued_rate,
	_erpnext_manufacture_state_is_valid,
	_issued_rate,
	_issued_rate_for_component,
	allocate_scrap_absorbed_cost,
	apply_iran_manufacture_output_contract,
	classify_manufacture_outputs,
	permit_scrap_zero_valuation,
)

MODULE = "erpnext_extensions.iran_accounting.scrap_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
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
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _consumed(item_code, qty, rate, batch_no=None, s_warehouse="WIP"):
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
		batch_no=batch_no,
	)


def _output(item_code, qty, is_fg=0, rate=0.0, secondary_item_type=None, batch_no=None, **extra):
	fields = dict(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse="Reject",
		is_finished_item=is_fg,
		batch_no=batch_no,
	)
	if secondary_item_type is not None:
		fields["secondary_item_type"] = secondary_item_type
	fields.update(extra)
	return _Row(**fields)


@contextmanager
def _irr(main_item_codes=None):
	lookup = main_item_codes or {}

	def _get_value(doctype, name, field):
		if doctype == "Item" and field == "custom_main_item_code":
			return lookup.get(name)
		return None

	with (
		mock.patch(f"{MODULE}.is_irr_company", return_value=True),
		mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{MODULE}.get_currency_precision", return_value=0),
		mock.patch(f"{MODULE}.frappe.db.get_value", side_effect=_get_value),
	):
		yield


class TestClassifyManufactureOutputs(unittest.TestCase):
	def test_component_scrap_and_fg(self):
		doc = _Doc(
			items=[
				_consumed("13200256", 100, 35297),
				_output("20100102", 90, is_fg=1, rate=1000, t_warehouse="FG"),
				_output("13200256", 7, secondary_item_type="Scrap"),
			]
		)
		with _irr():
			classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_MAIN_FG]), 1)
		self.assertEqual(len(classified[CLASS_COMPONENT_SCRAP]), 1)
		self.assertEqual(len(classified[CLASS_MAIN_PRODUCT_REJECT]), 0)

	def test_product_reject_is_not_component_scrap(self):
		doc = _Doc(
			items=[
				_consumed("13100023", 100, 1000),
				_output("30100033", 90, is_fg=1, t_warehouse="FG"),
				_output("30100033", 3, secondary_item_type="Scrap"),
			]
		)
		with _irr():
			classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_MAIN_PRODUCT_REJECT]), 1)
		self.assertEqual(len(classified[CLASS_COMPONENT_SCRAP]), 0)


class TestIssuedRateHelpers(unittest.TestCase):
	def test_weighted_average_multiple_consume_rows(self):
		doc = _Doc(
			items=[
				_consumed("A", 10, 100),
				_consumed("A", 30, 200),
			]
		)
		self.assertEqual(_issued_rate(doc, "A"), (10 * 100 + 30 * 200) / 40)

	def test_batch_match_wins_when_deterministic(self):
		doc = _Doc(
			items=[
				_consumed("A", 10, 100, batch_no="B1"),
				_consumed("A", 10, 500, batch_no="B2"),
			]
		)
		scrap = _output("A", 2, secondary_item_type="Scrap", batch_no="B2")
		self.assertEqual(_issued_rate_for_component(doc, scrap), 500)

	def test_weighted_fallback_when_batch_missing(self):
		doc = _Doc(
			items=[
				_consumed("A", 10, 100, batch_no="B1"),
				_consumed("A", 10, 300, batch_no="B2"),
			]
		)
		scrap = _output("A", 2, secondary_item_type="Scrap", batch_no="B9")
		self.assertEqual(_issued_rate_for_component(doc, scrap), 200)

	def test_multiple_source_warehouses(self):
		doc = _Doc(
			items=[
				_consumed("A", 5, 100, s_warehouse="WIP-A"),
				_consumed("A", 5, 300, s_warehouse="WIP-B"),
			]
		)
		self.assertEqual(_issued_rate(doc, "A"), 200)

	def test_transfer_rows_excluded(self):
		transfer = _consumed("A", 5, 999)
		transfer.t_warehouse = "Approved"
		doc = _Doc(items=[transfer, _consumed("A", 10, 100)])
		self.assertEqual(_issued_rate(doc, "A"), 100)

	def test_z_code_fallback(self):
		doc = _Doc(
			items=[
				_consumed("13100023", 10, 3505),
				_output("Z13100023", 2, secondary_item_type="Scrap"),
			]
		)
		with _irr(main_item_codes={"Z13100023": "13100023"}):
			rate = _issued_rate_for_component(doc, doc.items[1])
		self.assertEqual(rate, 3505)


class TestComponentScrapIssuedRateContract(unittest.TestCase):
	def _doc_zero_scrap(self, scrap_rate=0.0, stale=False):
		scrap = _output(
			"13200256",
			7,
			secondary_item_type="Scrap",
			rate=246100 if stale else scrap_rate,
			batch_no="737",
		)
		if scrap_rate == 0 and not stale:
			scrap.allow_zero_valuation_rate = 1
		fg = _output("20100102", 745, is_fg=1, rate=8070557, t_warehouse="FG")
		fg.basic_amount = 745 * 8070557
		fg.amount = fg.basic_amount
		fg.valuation_rate = 8070557
		return _Doc(
			items=[
				_consumed("13200256", 749, 35297, batch_no="736"),
				_consumed("13200256", 7, 35297, batch_no="737"),
				fg,
				scrap,
			]
		)

	def test_issued_rate_applied(self):
		doc = self._doc_zero_scrap()
		with _irr():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(doc.items[3].basic_rate), 35297)
		self.assertEqual(flt(doc.items[3].basic_amount), 7 * 35297)
		self.assertEqual(doc.items[3].allow_zero_valuation_rate, 0)

	def test_target_warehouse_without_history_still_issued(self):
		doc = self._doc_zero_scrap(scrap_rate=0)
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[3].basic_rate), 35297)

	def test_stale_target_warehouse_rate_overwritten(self):
		doc = self._doc_zero_scrap(stale=True)
		self.assertEqual(flt(doc.items[3].basic_rate), 246100)
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[3].basic_rate), 35297)

	def test_allow_zero_temporary_then_cleared(self):
		doc = self._doc_zero_scrap()
		with _irr():
			permit_scrap_zero_valuation(doc)
			self.assertEqual(doc.items[3].allow_zero_valuation_rate, 1)
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(doc.items[3].allow_zero_valuation_rate, 0)
		self.assertGreater(flt(doc.items[3].basic_rate), 0)

	def test_second_validate_does_not_wipe_rate(self):
		doc = self._doc_zero_scrap()
		with _irr():
			permit_scrap_zero_valuation(doc)
			apply_iran_manufacture_output_contract(doc)
			# ERPNext would zero the rate only while the flag is still set.
			self.assertEqual(doc.items[3].allow_zero_valuation_rate, 0)
			permit_scrap_zero_valuation(doc)
			self.assertEqual(doc.items[3].allow_zero_valuation_rate, 0)
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[3].basic_rate), 35297)
		self.assertEqual(doc.items[3].allow_zero_valuation_rate, 0)

	def test_healthy_gate_rejects_zero_scrap(self):
		doc = self._doc_zero_scrap()
		with _irr():
			self.assertFalse(_component_scrap_matches_issued_rate(doc))
			self.assertFalse(_erpnext_manufacture_state_is_valid(doc))
			apply_iran_manufacture_output_contract(doc)
			self.assertTrue(_component_scrap_matches_issued_rate(doc))

	def test_fg_residual_balanced(self):
		doc = self._doc_zero_scrap()
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		outgoing = sum(flt(r.basic_amount) for r in doc.items if r.get("s_warehouse"))
		incoming = sum(flt(r.basic_amount) for r in doc.items if r.get("t_warehouse"))
		self.assertEqual(incoming, outgoing)
		self.assertEqual(flt(doc.items[3].basic_amount), 247079)
		self.assertEqual(flt(doc.items[2].basic_amount), outgoing - 247079)

	def test_no_consume_match_raises(self):
		doc = _Doc(
			items=[
				_consumed("OTHER", 10, 100),
				_output("FG", 8, is_fg=1, t_warehouse="FG"),
				_output("ORPHAN", 1, secondary_item_type="Scrap"),
			]
		)
		with _irr():
			with self.assertRaises(frappe.ValidationError):
				apply_iran_manufacture_output_contract(doc)

	def test_main_fg_only_unchanged(self):
		fg = _output("FG", 10, is_fg=1, rate=500, t_warehouse="FG")
		doc = _Doc(items=[_consumed("RM", 10, 500), fg])
		with _irr():
			self.assertFalse(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(fg.basic_rate), 500)

	def test_product_reject_unchanged(self):
		doc = _Doc(
			items=[
				_consumed("RM", 100, 1000),
				_output("FG", 90, is_fg=1, t_warehouse="FG"),
				_output("FG", 10, secondary_item_type="Scrap"),
			]
		)
		with _irr():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertAlmostEqual(flt(doc.items[1].basic_rate), flt(doc.items[2].basic_rate), delta=2)
		self.assertEqual(flt(doc.items[1].basic_amount) + flt(doc.items[2].basic_amount), 100000)

	def test_same_document_multiple_scrap_rows(self):
		fg = _output("20100102", 10, is_fg=1, rate=1, t_warehouse="FG")
		doc = _Doc(
			items=[
				_consumed("13200256", 20, 35297),
				_consumed("13200473", 50, 33136),
				fg,
				_output("13200256", 7, secondary_item_type="Scrap"),
				_output("13200473", 38, secondary_item_type="Scrap"),
			]
		)
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[3].basic_rate), 35297)
		self.assertEqual(flt(doc.items[4].basic_rate), 33136)
		self.assertEqual(flt(doc.items[3].basic_amount), 7 * 35297)
		self.assertEqual(flt(doc.items[4].basic_amount), 38 * 33136)

	def test_mat_ste_2026_37327_fixture(self):
		"""Shape of MAT-STE-2026-37327: Soft Box scrap 7 @ issued 35,297."""
		fg = _output("20100102", 745, is_fg=1, rate=8070557, t_warehouse="FG")
		fg.basic_amount = 6012564965
		fg.amount = 6012564965
		fg.valuation_rate = 8070557
		doc = _Doc(
			items=[
				_consumed("13200256", 749, 35297, batch_no="736-13200256"),
				_consumed("13200473", 784, 33136, batch_no="670-13200473"),
				_consumed("13200023", 193, 3505, batch_no="167-13200023"),
				_consumed("13200066", 1495, 14640, batch_no="5723-13200066"),
				_consumed("13200256", 7, 35297, batch_no="737-13200256"),
				fg,
				_output(
					"13200256",
					7,
					secondary_item_type="Scrap",
					batch_no="737-13200256",
					allow_zero_valuation_rate=1,
				),
				_output(
					"13200473",
					38,
					secondary_item_type="Scrap",
					batch_no="670-13200473",
					allow_zero_valuation_rate=1,
				),
				_output("13200023", 5, secondary_item_type="Scrap", rate=3505, batch_no="167-13200023"),
				_output("13200066", 4, secondary_item_type="Scrap", rate=14640, batch_no="5723-13200066"),
			]
		)
		with _irr():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		soft = doc.items[6]
		bag = doc.items[7]
		self.assertEqual(flt(soft.basic_rate), 35297)
		self.assertEqual(flt(soft.basic_amount), 247079)
		self.assertEqual(soft.allow_zero_valuation_rate, 0)
		self.assertEqual(flt(bag.basic_rate), 33136)
		self.assertEqual(flt(bag.basic_amount), 38 * 33136)
		outgoing = sum(flt(r.basic_amount) for r in doc.items if r.get("s_warehouse"))
		incoming = sum(flt(r.basic_amount) for r in doc.items if r.get("t_warehouse"))
		self.assertEqual(incoming, outgoing)

	def test_allocate_still_prices_component_when_called_directly(self):
		doc = self._doc_zero_scrap()
		with _irr():
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		self.assertEqual(flt(doc.items[3].basic_rate), 35297)


class TestSourceSideNotDestination(unittest.TestCase):
	"""Destination Reject warehouse valuation must have zero influence."""

	SOURCE_RATE = 35297
	DEST_RATES = (0, 1, 246100, 9_999_999)

	def _doc(self, dest_rate: float, dest_warehouse="Reject-WH"):
		consume = _consumed("13200256", 7, self.SOURCE_RATE, batch_no="737", s_warehouse="WIP-SOURCE")
		scrap = _output(
			"13200256",
			7,
			secondary_item_type="Scrap",
			rate=dest_rate,
			batch_no="737",
			t_warehouse=dest_warehouse,
		)
		fg = _output("20100102", 1, is_fg=1, rate=1, t_warehouse="FG")
		return _Doc(items=[consume, fg, scrap]), consume, scrap

	def test_destination_rates_never_change_issued_rate(self):
		for dest_rate in self.DEST_RATES:
			with self.subTest(dest_rate=dest_rate):
				doc, consume, scrap = self._doc(dest_rate)
				self.assertNotEqual(dest_rate, self.SOURCE_RATE)
				self.assertEqual(flt(consume.basic_amount) / flt(consume.transfer_qty), self.SOURCE_RATE)
				self.assertEqual(consume.s_warehouse, "WIP-SOURCE")
				self.assertIsNone(consume.t_warehouse)
				self.assertEqual(scrap.t_warehouse, "Reject-WH")
				self.assertIsNone(scrap.s_warehouse)
				with _irr():
					apply_iran_manufacture_output_contract(doc)
				self.assertEqual(flt(scrap.basic_rate), self.SOURCE_RATE)
				self.assertEqual(flt(scrap.basic_amount), 7 * self.SOURCE_RATE)
				self.assertNotEqual(flt(scrap.basic_rate), dest_rate)

	def test_destination_batch_history_ignored(self):
		doc, consume, scrap = self._doc(246100)
		scrap.batch_no = "737"
		# Destination batch history would be 246100; source consume of the same
		# batch is 35,297. Source must win.
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(consume.basic_rate), self.SOURCE_RATE)
		self.assertEqual(flt(scrap.basic_rate), self.SOURCE_RATE)
		self.assertNotEqual(246100, self.SOURCE_RATE)

	def test_get_valuation_rate_never_consulted(self):
		doc, _consume, scrap = self._doc(246100)
		with (
			_irr(),
			mock.patch("erpnext.stock.stock_ledger.get_valuation_rate") as dest_spy,
		):
			apply_iran_manufacture_output_contract(doc)
		dest_spy.assert_not_called()
		self.assertEqual(flt(scrap.basic_rate), self.SOURCE_RATE)

	def test_riv_recalculate_keeps_source_rate_after_dest_poison(self):
		doc, _consume, scrap = self._doc(0)
		with _irr():
			apply_iran_manufacture_output_contract(doc)
			self.assertEqual(flt(scrap.basic_rate), self.SOURCE_RATE)
			# Simulate ERPNext RIV calculate() rewriting scrap from destination.
			scrap.basic_rate = 246100
			scrap.basic_amount = 7 * 246100
			scrap.amount = scrap.basic_amount
			scrap.valuation_rate = 246100
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(scrap.basic_rate), self.SOURCE_RATE)
		self.assertEqual(flt(scrap.basic_amount), 247079)

	def test_multiple_source_warehouses_weighted_not_destination(self):
		doc = _Doc(
			items=[
				_consumed("A", 5, 100, s_warehouse="WIP-A"),
				_consumed("A", 5, 300, s_warehouse="WIP-B"),
				_output("FG", 8, is_fg=1, t_warehouse="FG"),
				_output("A", 2, secondary_item_type="Scrap", rate=999_999, t_warehouse="Reject"),
			]
		)
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[3].basic_rate), 200)
		self.assertNotEqual(999_999, 200)

	def test_multiple_source_batches_match_then_destination_ignored(self):
		doc = _Doc(
			items=[
				_consumed("A", 10, 100, batch_no="B1", s_warehouse="WIP"),
				_consumed("A", 10, 500, batch_no="B2", s_warehouse="WIP"),
				_output("FG", 18, is_fg=1, t_warehouse="FG"),
				_output("A", 2, secondary_item_type="Scrap", rate=246100, batch_no="B2", t_warehouse="Reject"),
			]
		)
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[3].basic_rate), 500)
		self.assertNotEqual(246100, 500)


if __name__ == "__main__":
	unittest.main()
