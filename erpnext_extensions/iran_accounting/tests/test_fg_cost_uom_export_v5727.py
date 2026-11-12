# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: Costing Manual Override UOM + export helpers (v5.7.27)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing import (
	COSTING_EXPORT_DOCTYPES,
	_csv_bytes,
	_filename,
	_matrix,
	_safe_filename_part,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import apply_row_factors


class TestCostingManualOverrideUOM(unittest.TestCase):
	def test_manual_override_wins_when_item_master_missing_primary(self):
		"""BOX(2Vial)→BOX(1pfs) with no Item UOM Conversion Detail uses Manual Override."""
		row = MagicMock()
		row.item = "20100213"
		row.primary_uom = "BOX(1pfs)"
		row.stock_uom = "BOX(2Vial)"
		row.manual_override_factor = 2.0
		row.override_reason = "Pack: 1 BOX(2Vial) contains 2 BOX(1pfs)-equivalent units"
		row.remarks = ""

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.resolve_conversion",
			return_value=(None, "Missing", "No UOM Conversion Detail for BOX(1pfs)"),
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="BOX(2Vial)",
		):
			apply_row_factors(row)

		self.assertEqual(row.conversion_status, "Manual Override")
		self.assertEqual(row.conversion_source, "Manual Override")
		self.assertEqual(flt_or(row.conversion_factor), 2.0)
		self.assertEqual(flt_or(row.source_conversion_factor), 0.0)

	def test_normalized_qty_uses_applied_factor(self):
		actual = Decimal("25")
		factor = Decimal("2")
		self.assertEqual(actual * factor, Decimal("50"))


class TestCostingExportHelpers(unittest.TestCase):
	def test_supported_doctypes_include_recon_and_exceptions(self):
		self.assertIn("MFG Cost Reconciliation Check", COSTING_EXPORT_DOCTYPES)
		self.assertIn("MFG Cost Exception", COSTING_EXPORT_DOCTYPES)
		self.assertIn("MFG Cost Batch Expense Allocation", COSTING_EXPORT_DOCTYPES)

	def test_filename_includes_period(self):
		name = _filename("MFG Cost Reconciliation Check", "MFGCC-1", "csv", period="1405-01")
		self.assertEqual(name, "Costing_Reconciliation_1405-01.csv")

	def test_csv_utf8_bom_and_row_count(self):
		matrix = [["A", "B"], ["1", "۲"]]
		raw = _csv_bytes(matrix)
		self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
		self.assertIn("۲".encode("utf-8"), raw)

	def test_matrix_includes_id_for_standalone(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing.frappe"
		) as mock_frappe:
			meta = MagicMock()
			meta.istable = False
			meta.fields = [
				MagicMock(fieldname="check_name", label="Check", fieldtype="Data"),
				MagicMock(fieldname="status", label="Status", fieldtype="Data"),
			]
			mock_frappe.get_meta.return_value = meta
			mock_frappe.model.no_value_fields = ("Section Break", "Column Break", "HTML", "Table")
			matrix = _matrix(
				"MFG Cost Reconciliation Check",
				[{"name": "r1", "check_name": "gl_lines_to_pool", "status": "Passed"}],
			)
			self.assertEqual(matrix[0][0], "ID")
			self.assertEqual(matrix[1][0], "r1")

	def test_safe_filename(self):
		self.assertEqual(_safe_filename_part("1405-01"), "1405-01")
		self.assertTrue(_safe_filename_part("a/b c").replace("_", ""))


def flt_or(v):
	try:
		return float(v)
	except Exception:
		return v


if __name__ == "__main__":
	unittest.main()
