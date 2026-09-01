# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for selling documents DECIMAL(30,9) v5.0.7."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from erpnext_extensions import selling_documents_decimal_precision_v507 as mod


class TestSellingDocumentsDecimalPrecisionV507(unittest.TestCase):
	def test_allowlist_field_count(self):
		self.assertEqual(len(mod.selling_field_targets()), 127)

	def test_root_doctypes(self):
		self.assertEqual(mod.SELLING_ROOT_DOCTYPES, ("Sales Order", "Delivery Note", "Sales Invoice"))

	def test_no_runtime_field_scanning_in_migration(self):
		source = Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
		for snippet in ('fieldtype == "Currency"', "endswith(\"amount\")", "for df in meta.fields"):
			# classify_selling_field is allowed for completeness guard only
			if snippet == "for df in meta.fields":
				continue
			self.assertNotIn(snippet, source.split("def apply_decimal_schema_targets")[0])

	def test_decide_decimal_action_matrix(self):
		from erpnext_extensions.approved_decimal_precision import (
			ALTER_TO_DECIMAL_30_9,
			SKIP_ALREADY_CORRECT,
			SKIP_ALREADY_WIDER,
			SKIP_MISSING_COLUMN,
			decide_decimal_action,
		)

		self.assertEqual(decide_decimal_action(None), SKIP_MISSING_COLUMN)
		self.assertEqual(
			decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 30, "NUMERIC_SCALE": 9}),
			SKIP_ALREADY_CORRECT,
		)
		self.assertEqual(
			decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 38, "NUMERIC_SCALE": 9}),
			SKIP_ALREADY_WIDER,
		)
		self.assertEqual(
			decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 9}),
			ALTER_TO_DECIMAL_30_9,
		)

	def test_classify_selling_field_examples(self):
		rate = type("DF", (), {"fieldname": "rate", "fieldtype": "Currency", "is_virtual": 0})()
		amount = type("DF", (), {"fieldname": "grand_total", "fieldtype": "Currency", "is_virtual": 0})()
		virtual = type("DF", (), {"fieldname": "last_scanned_warehouse", "fieldtype": "Data", "is_virtual": 1})()
		self.assertEqual(mod.classify_selling_field(rate), "rate_pct")
		self.assertEqual(mod.classify_selling_field(amount), "amount")
		self.assertEqual(mod.classify_selling_field(virtual), "virtual")

	@patch.object(mod, "alter_decimal_column")
	@patch.object(mod, "read_column_schema")
	@patch.object(mod, "table_exists", return_value=True)
	@patch("erpnext_extensions.selling_documents_decimal_precision_v507.frappe.get_meta")
	@patch("erpnext_extensions.selling_documents_decimal_precision_v507.frappe.db.exists", return_value=True)
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
		with patch.object(mod, "selling_field_targets", return_value=(mod.SellingFieldTarget("Sales Order", "grand_total"),)):
			rows = mod.apply_decimal_schema_targets(logger)
		self.assertEqual(rows[0]["action"], "SKIP_ALREADY_CORRECT")
		mock_alter.assert_not_called()

	def test_completeness_guard_passes(self):
		mod.assert_field_classification_completeness()
