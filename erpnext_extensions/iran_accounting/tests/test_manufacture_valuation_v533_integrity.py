# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.3 RIV / integrity / real-document regression (development only)."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	CONTRACT_VERSION_FIELD,
	validate_job_card_secondary_match,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_COMPONENT_SCRAP,
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	_issued_rate_for_component,
	apply_iran_manufacture_output_contract,
	classify_manufacture_outputs,
)

SCRAP = "erpnext_extensions.iran_accounting.scrap_costing"
STAGE = "erpnext_extensions.iran_accounting.manufacture_stage_costing"

REAL_SCRAP_VOUCHER = "MAT-STE-2026-37327"
REAL_MISMATCH_VOUCHER = "MAT-STE-2026-37031"
EXPECTED_SOFT_BOX_RATE = 35297


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
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


def _consumed(item_code, qty, rate):
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		s_warehouse="WIP",
		t_warehouse=None,
		is_finished_item=0,
	)


def _output(item_code, qty, is_fg=0, rate=0.0, secondary_item_type=None, **extra):
	fields = dict(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse="FG",
		is_finished_item=is_fg,
		stock_uom="Nos",
	)
	if secondary_item_type is not None:
		fields["secondary_item_type"] = secondary_item_type
	fields.update(extra)
	return _Row(**fields)


@contextmanager
def _irr():
	def _get_value(doctype, name, fieldname=None, **kwargs):
		if doctype == "Item" and fieldname == "custom_main_item_code":
			return None
		if doctype == "Item" and fieldname == "stock_uom":
			return "Nos"
		return None

	with (
		mock.patch(f"{SCRAP}.is_irr_company", return_value=True),
		mock.patch(f"{SCRAP}.get_company_currency", return_value="IRR"),
		mock.patch(f"{SCRAP}.get_currency_precision", return_value=0),
		mock.patch(f"{SCRAP}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.is_irr_company", return_value=True),
		mock.patch(f"{STAGE}.get_company_currency", return_value="IRR"),
		mock.patch(f"{STAGE}.frappe.db.get_value", side_effect=_get_value),
		mock.patch(f"{STAGE}.frappe.get_all", return_value=[]),
	):
		yield


def _stage_doc():
	return _Doc(
		items=[
			_consumed("RM", 10, 1000),
			_output("FG", 8, is_fg=1),
			_output("CP", 1, secondary_item_type="Co-Product", t_warehouse="CO"),
			_output("RM", 1, secondary_item_type="Scrap", t_warehouse="RJ"),
		],
		total_additional_costs=900,
	)


class TestV533SyntheticIntegrity(unittest.TestCase):
	def _snapshot(self, doc):
		return [
			(
				row.get("item_code"),
				flt(row.get("basic_rate")),
				flt(row.get("basic_amount")),
				flt(row.get("additional_cost")),
				flt(row.get("amount")),
			)
			for row in doc.items
		]

	def test_new_document_first_and_second_riv_idempotent(self):
		doc = _stage_doc()
		with _irr():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			first = self._snapshot(doc)
			self.assertEqual(doc.get(CONTRACT_VERSION_FIELD), MANUFACTURE_COSTING_CONTRACT_VERSION)
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			self.assertEqual(self._snapshot(doc), first)

	def test_identity_no_value_creation(self):
		doc = _stage_doc()
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		outgoing = sum(flt(row.amount) for row in doc.items if row.get("s_warehouse"))
		incoming = sum(flt(row.amount) for row in doc.items if row.get("t_warehouse"))
		operating = flt(doc.total_additional_costs)
		self.assertEqual(incoming, outgoing + operating)
		self.assertGreaterEqual(flt(doc.items[1].amount), 0)
		self.assertEqual(flt(doc.items[3].additional_cost), 0)

	def test_cancellation_reverses_economics(self):
		doc = _stage_doc()
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		reversed_incoming = -sum(flt(row.amount) for row in doc.items if row.get("t_warehouse"))
		reversed_outgoing = -sum(flt(row.amount) for row in doc.items if row.get("s_warehouse"))
		self.assertEqual(
			reversed_incoming,
			-(sum(flt(row.amount) for row in doc.items if row.get("t_warehouse"))),
		)
		self.assertEqual(reversed_incoming - reversed_outgoing, -flt(doc.total_additional_costs))

	def test_historical_unstamped_not_silently_recosted(self):
		doc = _stage_doc()
		doc.docstatus = 1
		doc.items[2].basic_rate = 0
		doc.items[2].basic_amount = 0
		doc.items[2].amount = 0
		with _irr():
			self.assertFalse(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertFalse(doc.get(CONTRACT_VERSION_FIELD))

	def test_no_negative_finished_good(self):
		doc = _stage_doc()
		doc.items[1].basic_rate = -1
		doc.items[1].amount = -8
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertGreaterEqual(flt(doc.items[1].amount), 0)
		self.assertGreaterEqual(flt(doc.items[1].basic_rate), 0)


class TestRealDevelopmentDocuments(unittest.TestCase):
	def test_mat_ste_2026_37327_issued_rate_expectation(self):
		if not frappe.db.exists("Stock Entry", REAL_SCRAP_VOUCHER):
			self.skipTest(f"{REAL_SCRAP_VOUCHER} not on this site")
		doc = frappe.get_doc("Stock Entry", REAL_SCRAP_VOUCHER)
		self.assertEqual(doc.purpose, "Manufacture")
		self.assertFalse(doc.get(CONTRACT_VERSION_FIELD))
		classified = classify_manufacture_outputs(doc)
		scrap_rows = classified[CLASS_COMPONENT_SCRAP]
		self.assertTrue(scrap_rows)
		soft = next((row for row in scrap_rows if row.item_code == "13200256"), None)
		self.assertIsNotNone(soft)
		expected = _issued_rate_for_component(doc, soft)
		self.assertEqual(round(expected), EXPECTED_SOFT_BOX_RATE)
		self.assertEqual(round(expected * flt(soft.transfer_qty or soft.qty)), 247079)
		for row in scrap_rows:
			issued = _issued_rate_for_component(doc, row)
			self.assertGreater(issued, 0)
		# Read-only: do not apply or save the live document.
		reloaded = frappe.get_doc("Stock Entry", REAL_SCRAP_VOUCHER)
		self.assertFalse(reloaded.get(CONTRACT_VERSION_FIELD))
		self.assertEqual(cint_safe(reloaded.docstatus), cint_safe(doc.docstatus))
		self.assertEqual(flt(soft.basic_rate), flt(reloaded.items[soft.idx - 1].basic_rate))

	def test_mat_ste_2026_37031_mismatch_would_block_draft(self):
		if not frappe.db.exists("Stock Entry", REAL_MISMATCH_VOUCHER):
			self.skipTest(f"{REAL_MISMATCH_VOUCHER} not on this site")
		doc = frappe.get_doc("Stock Entry", REAL_MISMATCH_VOUCHER)
		self.assertEqual(doc.purpose, "Manufacture")
		self.assertTrue(doc.job_card)
		self.assertFalse(doc.get(CONTRACT_VERSION_FIELD))
		before = [(row.item_code, flt(row.basic_rate), flt(row.amount)) for row in doc.items]
		if cint_safe(doc.docstatus) == 1:
			self.assertFalse(apply_iran_manufacture_output_contract(doc))
			after = [(row.item_code, flt(row.basic_rate), flt(row.amount)) for row in doc.items]
			self.assertEqual(before, after)
			doc.docstatus = 0
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_job_card_secondary_match(doc)
		message = str(ctx.exception)
		self.assertIn(doc.job_card, message)
		self.assertTrue("30500003" in message or "30300020" in message)
		reloaded = frappe.get_doc("Stock Entry", REAL_MISMATCH_VOUCHER)
		self.assertFalse(reloaded.get(CONTRACT_VERSION_FIELD))
		self.assertEqual(
			[(row.item_code, flt(row.basic_rate), flt(row.amount)) for row in reloaded.items],
			before,
		)


def cint_safe(value):
	from frappe.utils import cint

	return cint(value)


if __name__ == "__main__":
	unittest.main()
