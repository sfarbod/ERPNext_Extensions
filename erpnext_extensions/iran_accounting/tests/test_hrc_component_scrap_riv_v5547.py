# Copyright (c) 2026, ERPNext Extensions contributors
"""Component Scrap mid-RIV fallback + failed-identity registry regressions."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.scrap_costing import (
	_weighted_issued_rate,
	apply_component_scrap_issued_rates,
)
from erpnext_extensions.iran_accounting.historical_rebuild import failure_registry as fr


class TestWeightedIssuedRateFallback(unittest.TestCase):
	def test_uses_basic_rate_when_amount_wiped(self):
		row = mock.Mock()
		row.get = lambda k, d=None: {
			"basic_amount": 0,
			"basic_rate": 181400,
			"qty": 10,
			"transfer_qty": 10,
			"name": "sed-1",
		}.get(k, d)
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._row_qty", return_value=10.0
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._persisted_consume_rate",
			return_value=0.0,
		):
			rate = _weighted_issued_rate([row])
		self.assertEqual(flt(rate), 181400.0)

	def test_persisted_fallback_when_memory_zero(self):
		row = mock.Mock()
		row.get = lambda k, d=None: {
			"basic_amount": 0,
			"basic_rate": 0,
			"qty": 5,
			"transfer_qty": 5,
			"name": "sed-2",
		}.get(k, d)
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._row_qty", return_value=5.0
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._persisted_consume_rate",
			return_value=493000.0,
		):
			rate = _weighted_issued_rate([row])
		self.assertEqual(flt(rate), 493000.0)


class TestComponentScrapThroughRivSkip(unittest.TestCase):
	def test_through_riv_skips_throw_when_consume_exists(self):
		doc = mock.Mock()
		doc.company = "C"
		doc.doctype = "Stock Entry"
		doc.purpose = "Manufacture"
		scrap = mock.Mock()
		scrap.get = lambda k, d=None: {"item_code": "13200190", "basic_rate": 0, "name": "s1"}.get(k, d)
		scrap.item_code = "13200190"
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
			return_value={"COMPONENT_SCRAP": [scrap], "BULK_SCRAP": [], "OTHER_OUTPUT": [],
				"MAIN_FG": [], "MAIN_PRODUCT_REJECT": [], "CO_PRODUCT": [], "CO_PRODUCT_REJECT": []},
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._issued_rate_for_component",
			return_value=0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._persisted_consume_rate",
			return_value=0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._consume_rows_for_item",
			return_value=[mock.Mock()],
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.apply_bulk_scrap_zero_valuation",
			return_value=False,
		):
			frappe.flags.through_repost_item_valuation = True
			try:
				# Must not throw
				apply_component_scrap_issued_rates(doc)
			finally:
				frappe.flags.through_repost_item_valuation = False


class TestFailureRegistry(unittest.TestCase):
	def test_max_outstanding_constant(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
			MAX_OUTSTANDING_RIV,
		)
		self.assertEqual(MAX_OUTSTANDING_RIV, 8)

	def test_categorize_component_scrap(self):
		self.assertEqual(fr._categorize("Component Scrap Row #23"), "component_scrap")
		self.assertEqual(fr._categorize("Stock valuation integrity (I3)."), "valuation_integrity_i3")
		self.assertEqual(fr._categorize("No such file or directory: repost_item_valuation-x.json.gz"), "missing_repost_file")

	def test_is_blocked_cleared_by_completed(self):
		with mock.patch.object(fr.frappe, "db") as db:
			db.exists.side_effect = lambda *a, **k: (
				True if (isinstance(a[1], dict) and a[1].get("status") == "Completed") else False
			)
			self.assertFalse(fr.is_blocked_identity("X", "WH"))
