# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit / drift tests for stock repost DECIMAL(30,9) runtime reliability (v5.1.9)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions import stock_repost_decimal_precision_v518 as v518
from erpnext_extensions import stock_repost_decimal_precision_v519 as v519


class TestStockRepostDecimalPrecisionV519(unittest.TestCase):
	def test_reuses_v518_registry_not_duplicate(self):
		self.assertIs(v519.REPOST_MONETARY_FIELDS_BY_DOCTYPE, v518.REPOST_MONETARY_FIELDS_BY_DOCTYPE)
		self.assertIn("basic_amount", v519.REPOST_MONETARY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])

	def test_critical_fields_include_basic_amount(self):
		self.assertEqual(
			v519.CRITICAL_STOCK_ENTRY_DETAIL_FIELDS,
			("basic_amount", "amount", "basic_rate", "valuation_rate"),
		)

	def test_status_helper_shape(self):
		status = v519.get_stock_repost_precision_status()
		self.assertEqual(status["version_layer"], "5.1.9")
		self.assertGreater(status["total_registered"], 80)
		self.assertIn("basic_amount", status["critical_stock_entry_detail"])
		self.assertEqual(status["incorrect"], 0)
		self.assertEqual(status["missing"], 0)

	def test_assert_passes_when_schema_correct(self):
		v519.assert_stock_repost_decimal_schema()

	@patch.object(v519, "apply_decimal_schema_targets")
	@patch.object(v519, "verify_and_set_metadata")
	@patch.object(v519, "assert_stock_repost_decimal_schema")
	@patch.object(v519, "get_stock_repost_precision_status")
	def test_repair_raises_on_errors(self, mock_status, mock_assert, mock_meta, mock_schema):
		mock_status.return_value = {"incorrect": 1, "correct": 0, "missing": 0, "total_registered": 1}
		mock_meta.return_value = []
		mock_schema.return_value = [
			{"table": "tabStock Entry Detail", "field": "basic_amount", "status": "error", "doctype": "Stock Entry Detail"}
		]
		with self.assertRaises(RuntimeError):
			v519.repair_stock_repost_decimal_schema()
