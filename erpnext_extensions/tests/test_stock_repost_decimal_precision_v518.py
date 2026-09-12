# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for stock repost DECIMAL(30,9) v5.1.8."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions import stock_repost_decimal_precision_v518 as mod


class TestStockRepostDecimalPrecisionV518(unittest.TestCase):
	def test_basic_amount_in_allowlist(self):
		self.assertIn("basic_amount", mod.REPOST_MONETARY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])
		self.assertIn("amount", mod.REPOST_MONETARY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])
		self.assertIn("valuation_rate", mod.REPOST_MONETARY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])
		self.assertIn("basic_rate", mod.REPOST_MONETARY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])

	def test_sle_monetary_fields(self):
		self.assertEqual(
			set(mod.REPOST_MONETARY_FIELDS_BY_DOCTYPE["Stock Ledger Entry"]),
			{"stock_value", "stock_value_difference", "incoming_rate", "outgoing_rate", "valuation_rate"},
		)

	def test_qty_fields_excluded(self):
		self.assertIn("qty", mod.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])
		self.assertIn("actual_qty", mod.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["Stock Ledger Entry"])
		self.assertNotIn("basic_amount", mod.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["Stock Entry Detail"])

	def test_valuation_rates_classified_as_monetary_rate(self):
		self.assertIn(("Stock Entry Detail", "basic_rate"), mod.REPOST_MONETARY_RATE_FIELDS)
		self.assertIn(("Stock Ledger Entry", "valuation_rate"), mod.REPOST_MONETARY_RATE_FIELDS)

	def test_allowlist_non_empty(self):
		self.assertGreater(len(mod.repost_field_targets()), 80)

	def test_decide_decimal_action_matrix(self):
		from erpnext_extensions.approved_decimal_precision import (
			ALTER_TO_DECIMAL_30_9,
			SKIP_ALREADY_CORRECT,
			decide_decimal_action,
		)

		self.assertEqual(
			decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 9}),
			ALTER_TO_DECIMAL_30_9,
		)
		self.assertEqual(
			decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 30, "NUMERIC_SCALE": 9}),
			SKIP_ALREADY_CORRECT,
		)

	@patch.object(mod, "alter_decimal_column")
	@patch.object(mod, "read_column_schema")
	@patch.object(mod, "table_exists", return_value=True)
	@patch("erpnext_extensions.stock_repost_decimal_precision_v518.frappe.get_meta")
	@patch("erpnext_extensions.stock_repost_decimal_precision_v518.frappe.db.exists", return_value=True)
	def test_schema_patch_idempotent(self, mock_exists, mock_meta, mock_table, mock_read, mock_alter):
		logger = MagicMock()
		mock_meta.return_value.get_field.return_value = type("DF", (), {"fieldtype": "Currency"})()
		col = {
			"DATA_TYPE": "decimal",
			"COLUMN_TYPE": "decimal(30,9)",
			"NUMERIC_PRECISION": 30,
			"NUMERIC_SCALE": 9,
			"IS_NULLABLE": "YES",
			"COLUMN_DEFAULT": None,
		}
		mock_read.side_effect = [col, col]
		with patch.object(
			mod,
			"repost_field_targets",
			return_value=(mod.RepostFieldTarget("Stock Entry Detail", "basic_amount"),),
		):
			rows = mod.apply_decimal_schema_targets(logger)
		self.assertEqual(rows[0]["action"], "SKIP_ALREADY_CORRECT")
		mock_alter.assert_not_called()

	def test_completeness_guard_passes(self):
		mod.assert_repost_field_classification_completeness()
