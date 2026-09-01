# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for Sales Order amount_eligible_for_commission DECIMAL(30,9) hotfix."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from erpnext_extensions import sales_order_commission_decimal_precision as mod


class TestSalesOrderCommissionDecimalPrecision(unittest.TestCase):
	def test_single_field_allowlist(self):
		target = mod.sales_order_commission_field_target()
		self.assertEqual(target.doctype, "Sales Order")
		self.assertEqual(target.fieldname, "amount_eligible_for_commission")
		self.assertEqual(target.table, "tabSales Order")

	def test_no_runtime_field_scanning(self):
		source = Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
		for snippet in (
			"keyword",
			"for df in meta.fields",
			'fieldtype == "Currency"',
			"endswith(\"amount\")",
		):
			self.assertNotIn(snippet, source)

	def test_decide_decimal_action_matrix(self):
		self.assertEqual(mod.decide_decimal_action(None), mod.SKIP_MISSING_COLUMN)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 30, "NUMERIC_SCALE": 9}),
			mod.SKIP_ALREADY_CORRECT,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 38, "NUMERIC_SCALE": 9}),
			mod.SKIP_ALREADY_WIDER,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 9}),
			mod.ALTER_TO_DECIMAL_30_9,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 2}),
			mod.SKIP_UNEXPECTED_SCALE,
		)

	@patch("erpnext_extensions.sales_order_commission_decimal_precision.frappe.clear_cache")
	@patch("erpnext_extensions.sales_order_commission_decimal_precision.make_property_setter")
	@patch("erpnext_extensions.sales_order_commission_decimal_precision.frappe.db.set_value")
	@patch("erpnext_extensions.sales_order_commission_decimal_precision.frappe.db.get_value")
	def test_property_setter_idempotent(self, mock_get_value, mock_set_value, mock_make_ps, mock_clear_cache):
		logger = MagicMock()
		mock_get_value.side_effect = [None]
		action, value = mod.ensure_length_property_setter(
			"Sales Order", "amount_eligible_for_commission", 30, logger
		)
		self.assertEqual((action, value), ("CREATE_METADATA_LENGTH", 30))

		mock_get_value.side_effect = ["Sales Order-amount_eligible_for_commission-length", "21"]
		action, value = mod.ensure_length_property_setter(
			"Sales Order", "amount_eligible_for_commission", 30, logger
		)
		self.assertEqual((action, value), ("UPDATE_METADATA_LENGTH", 30))

		mock_set_value.reset_mock()
		mock_get_value.side_effect = ["Sales Order-amount_eligible_for_commission-length", "38"]
		action, value = mod.ensure_length_property_setter(
			"Sales Order", "amount_eligible_for_commission", 30, logger
		)
		self.assertEqual((action, value), ("SKIP_METADATA_ALREADY_SET", 38))
		mock_set_value.assert_not_called()

	@patch.object(mod, "alter_decimal_column")
	@patch.object(mod, "read_column_schema")
	@patch.object(mod, "table_exists", return_value=True)
	@patch("erpnext_extensions.sales_order_commission_decimal_precision.frappe.get_meta")
	@patch("erpnext_extensions.sales_order_commission_decimal_precision.frappe.db.exists", return_value=True)
	def test_schema_patch_idempotent(self, mock_exists, mock_meta, mock_table_exists, mock_read, mock_alter):
		logger = MagicMock()
		mock_meta.return_value.get_field.return_value = type("DF", (), {"fieldtype": "Currency"})()
		mock_read.side_effect = [
			{
				"DATA_TYPE": "decimal",
				"COLUMN_TYPE": "decimal(30,9)",
				"NUMERIC_PRECISION": 30,
				"NUMERIC_SCALE": 9,
				"IS_NULLABLE": "NO",
				"COLUMN_DEFAULT": "0.000000000",
			},
			{
				"DATA_TYPE": "decimal",
				"COLUMN_TYPE": "decimal(30,9)",
				"NUMERIC_PRECISION": 30,
				"NUMERIC_SCALE": 9,
				"IS_NULLABLE": "NO",
				"COLUMN_DEFAULT": "0.000000000",
			},
		]
		rows = mod.apply_decimal_schema_target(logger)
		self.assertEqual(rows[0]["action"], mod.SKIP_ALREADY_CORRECT)
		mock_alter.assert_not_called()
