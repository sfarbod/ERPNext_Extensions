# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.43 — BULK_SCRAP intentional-zero contract (distinct from COMPONENT_SCRAP)."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_BULK_SCRAP,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	CLASS_OTHER_OUTPUT,
	apply_bulk_scrap_zero_valuation,
	apply_component_scrap_issued_rates,
	apply_iran_manufacture_output_contract,
	classify_manufacture_outputs,
	is_intentional_bulk_scrap_zero,
)

MODULE = "erpnext_extensions.iran_accounting.scrap_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("custom_output_class", None)
		self.__dict__.setdefault("valuation_type", None)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		setattr(self, key, value)


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("custom_manufacturing_costing_contract_version", "5.3.43")
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.setdefault("job_card", None)
		self.__dict__.setdefault("work_order", None)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		setattr(self, key, value)


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
		t_warehouse="ScrapWH",
		is_finished_item=is_fg,
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
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual._persisted_docstatus",
			return_value=0,
		),
	):
		yield


def _bulk_doc(*, bulk_rate=0.0, stamp=None):
	"""Manufacture with FG + Bulk Scrap (Manual zero) + unrelated consume."""
	bulk = _output(
		"30100101",
		110,
		secondary_item_type="Scrap",
		rate=bulk_rate,
		valuation_type="Manual",
		allow_zero_valuation_rate=1,
		t_warehouse="FOR OTHER PURPOSE",
		custom_output_class=stamp,
	)
	return _Doc(
		items=[
			_consumed("30100023", 2060, 358886),
			_output("30100022", 2052, is_fg=1, rate=360285, t_warehouse="Quarantine"),
			bulk,
		]
	)


class TestBulkScrapClassification(unittest.TestCase):
	def test_manual_zero_scrap_is_bulk_not_product_reject_or_component(self):
		doc = _bulk_doc()
		with _irr():
			classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_BULK_SCRAP]), 1)
		self.assertEqual(classified[CLASS_BULK_SCRAP][0].item_code, "30100101")
		self.assertEqual(len(classified[CLASS_COMPONENT_SCRAP]), 0)
		self.assertEqual(len(classified[CLASS_MAIN_PRODUCT_REJECT]), 0)
		self.assertEqual(len(classified[CLASS_OTHER_OUTPUT]), 0)
		self.assertTrue(is_intentional_bulk_scrap_zero(doc.items[2]))

	def test_component_scrap_still_issued_rate_not_bulk(self):
		doc = _Doc(
			items=[
				_consumed("13200256", 100, 35297),
				_output("20100102", 90, is_fg=1, rate=1000, t_warehouse="FG"),
				_output("13200256", 7, secondary_item_type="Scrap", rate=0),
			]
		)
		with _irr():
			classified = classify_manufacture_outputs(doc)
			applied = apply_component_scrap_issued_rates(doc)
		self.assertTrue(applied)
		self.assertEqual(len(classified[CLASS_COMPONENT_SCRAP]), 1)
		self.assertEqual(len(classified[CLASS_BULK_SCRAP]), 0)
		self.assertEqual(flt(doc.items[2].basic_rate), 35297)

	def test_explicit_stamp_wins(self):
		doc = _bulk_doc(stamp=CLASS_BULK_SCRAP)
		# Even without Manual flags after stamp
		doc.items[2].valuation_type = None
		doc.items[2].allow_zero_valuation_rate = 0
		with _irr():
			classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_BULK_SCRAP]), 1)


class TestBulkScrapZeroContract(unittest.TestCase):
	def test_bulk_scrap_remains_rate_zero(self):
		doc = _bulk_doc()
		with _irr():
			apply_bulk_scrap_zero_valuation(doc)
		row = doc.items[2]
		self.assertEqual(flt(row.basic_rate), 0)
		self.assertEqual(flt(row.basic_amount), 0)
		self.assertEqual(flt(row.valuation_rate), 0)
		self.assertEqual(row.custom_output_class, CLASS_BULK_SCRAP)
		self.assertEqual(cint_allow(row), 1)

	def test_component_path_does_not_price_bulk(self):
		doc = _bulk_doc()
		with _irr():
			apply_component_scrap_issued_rates(doc)
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertEqual(doc.items[2].custom_output_class, CLASS_BULK_SCRAP)

	def test_not_in_stage_allocation_pool(self):
		from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
			allocate_stage_output_cost,
		)

		doc = _bulk_doc()
		# Stage allocator needs co-product to enter; with only bulk scrap it
		# should restore FG residual / return without pricing bulk.
		with _irr():
			classify_manufacture_outputs(doc)
			allocate_stage_output_cost(doc)
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertNotEqual(doc.items[2].custom_output_class, "STAGE_EQUIVALENT")

	def test_iran_contract_preserves_zero_and_qty(self):
		doc = _bulk_doc()
		qty_before = [(r.item_code, flt(r.qty)) for r in doc.items]
		with _irr():
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual([(r.item_code, flt(r.qty)) for r in doc.items], qty_before)
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertEqual(doc.items[2].custom_output_class, CLASS_BULK_SCRAP)


class TestBulkScrapHistoricalStamps(unittest.TestCase):
	def test_stamp_already_healthy_marks_legitimate_zero(self):
		from erpnext_extensions.iran_accounting.historical_stock.manufacture_native_historical import (
			ALREADY_HEALTHY,
			stamp_wrong_rate_row_from_iran_native,
		)

		row = stamp_wrong_rate_row_from_iran_native(
			{"voucher": "MAT-STE-X", "item": "30100101"},
			evidence={
				"classification": ALREADY_HEALTHY,
				"families": ["BULK_SCRAP", "PRODUCT_REJECT"],
				"delta_n": 0,
			},
		)
		self.assertEqual(row["status"], "NO_ACTION_REQUIRED")
		self.assertFalse(row["eligible"])
		self.assertFalse(row["actionable"])
		self.assertEqual(row["zero_class"], "Z0_LEGITIMATE_ZERO")
		self.assertEqual(row["classification"], "LEGITIMATE_ZERO")
		self.assertEqual(row["business_question"], "RESOLVED")
		self.assertEqual(row["output_class"], CLASS_BULK_SCRAP)

	def test_families_present_bulk_not_component(self):
		from erpnext_extensions.iran_accounting.historical_stock.manufacture_native_historical import (
			_families_present,
		)

		doc = _bulk_doc()
		fams = _families_present(doc)
		self.assertIn("BULK_SCRAP", fams)
		self.assertNotIn("COMPONENT_SCRAP", fams)


class TestBulkScrapRIVAndRepostPreserveZero(unittest.TestCase):
	"""RIV1 / RIV2 / native-repost must not invent a positive Bulk Scrap rate."""

	def test_riv1_preserves_rate_zero(self):
		doc = _bulk_doc()
		with _irr():
			apply_iran_manufacture_output_contract(doc)  # RIV1
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertEqual(doc.items[2].custom_output_class, CLASS_BULK_SCRAP)

	def test_riv2_idempotent_preserves_rate_zero(self):
		doc = _bulk_doc()
		with _irr():
			apply_iran_manufacture_output_contract(doc)  # RIV1
			apply_iran_manufacture_output_contract(doc)  # RIV2
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertEqual(flt(doc.items[2].basic_amount), 0)
		self.assertEqual(cint_allow(doc.items[2]), 1)

	def test_full_native_repost_path_preserves_zero(self):
		"""Native contract is the Manufacture authority during full repost."""
		doc = _bulk_doc()
		qty_before = [(r.item_code, flt(r.qty)) for r in doc.items]
		with _irr():
			# Simulate repost recalculation of Manufacture outputs.
			apply_component_scrap_issued_rates(doc)
			apply_bulk_scrap_zero_valuation(doc)
			apply_iran_manufacture_output_contract(doc)
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertEqual([(r.item_code, flt(r.qty)) for r in doc.items], qty_before)

	def test_no_wrong_rate_or_zero_rate_actionable_finding(self):
		from erpnext_extensions.iran_accounting.historical_stock.manufacture_native_historical import (
			ALREADY_HEALTHY,
			stamp_wrong_rate_row_from_iran_native,
		)

		row = stamp_wrong_rate_row_from_iran_native(
			{
				"voucher": "MAT-STE-X",
				"item": "30100101",
				"current_rate": 0,
				"purpose": "Manufacture",
			},
			evidence={
				"classification": ALREADY_HEALTHY,
				"families": ["BULK_SCRAP"],
				"delta_n": 0,
			},
		)
		self.assertEqual(row["status"], "NO_ACTION_REQUIRED")
		self.assertFalse(row.get("actionable"))
		self.assertFalse(row.get("eligible"))
		self.assertEqual(row["kpi_bucket"], "NO_ACTION")
		self.assertNotIn(row.get("wrong_reason"), ("WRONG_RATE", "ZERO_RATE", "LOST_VALUATION"))


class TestBulkScrapDoesNotCorrupt(unittest.TestCase):
	def test_forcing_positive_bulk_rate_is_reset_to_zero(self):
		doc = _bulk_doc(bulk_rate=999999)
		# Positive rate with Manual is NOT intentional-zero — classify as OTHER.
		# Stamp explicitly to prove enforce-zero path.
		doc.items[2].custom_output_class = CLASS_BULK_SCRAP
		doc.items[2].basic_rate = 999999
		doc.items[2].basic_amount = 999999 * 110
		with _irr():
			apply_bulk_scrap_zero_valuation(doc)
		self.assertEqual(flt(doc.items[2].basic_rate), 0)
		self.assertEqual(flt(doc.items[2].basic_amount), 0)


def cint_allow(row):
	return int(row.get("allow_zero_valuation_rate") or 0)


if __name__ == "__main__":
	unittest.main()
