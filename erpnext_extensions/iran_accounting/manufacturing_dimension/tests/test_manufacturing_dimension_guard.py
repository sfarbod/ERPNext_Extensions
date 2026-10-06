# Copyright (c) 2026, ERPNext Extensions contributors
"""G1–G8: Manufacturing Dimension Uniformity Guard."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import frappe

from erpnext_extensions.iran_accounting.manufacturing_dimension.guard import (
	validate_manufacturing_dimension_uniformity,
)


def _row(idx, item_code, department="", cost_center=""):
	return SimpleNamespace(
		idx=idx,
		item_code=item_code,
		department=department,
		cost_center=cost_center,
		name=f"row-{idx}",
	)


def _doc(purpose, rows):
	return SimpleNamespace(purpose=purpose, items=rows, company="Test Co")


class TestManufacturingDimensionGuard(unittest.TestCase):
	def test_g1_mtfm_uniform_passes(self):
		doc = _doc(
			"Material Transfer for Manufacture",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept A", "CC-1"),
			],
		)
		validate_manufacturing_dimension_uniformity(doc)  # no throw

	def test_g2_manufacture_different_department_blocks(self):
		doc = _doc(
			"Manufacture",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept B", "CC-1"),
			],
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_manufacturing_dimension_uniformity(doc)
		self.assertIn("Manufacturing Dimension Mismatch", str(ctx.exception))

	def test_g3_same_cc_different_department_blocks(self):
		doc = _doc(
			"Material Transfer for Manufacture",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept B", "CC-1"),
			],
		)
		with self.assertRaises(frappe.ValidationError):
			validate_manufacturing_dimension_uniformity(doc)

	def test_g4_same_department_different_cc_blocks(self):
		doc = _doc(
			"Manufacture",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept A", "CC-2"),
			],
		)
		with self.assertRaises(frappe.ValidationError):
			validate_manufacturing_dimension_uniformity(doc)

	def test_g5_populated_plus_blank_department_blocks(self):
		doc = _doc(
			"Manufacture",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "", "CC-1"),
			],
		)
		with self.assertRaises(frappe.ValidationError):
			validate_manufacturing_dimension_uniformity(doc)

	def test_g6_populated_plus_blank_cost_center_blocks(self):
		doc = _doc(
			"Material Transfer for Manufacture",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept A", ""),
			],
		)
		with self.assertRaises(frappe.ValidationError):
			validate_manufacturing_dimension_uniformity(doc)

	def test_g7_material_issue_mixed_not_blocked(self):
		doc = _doc(
			"Material Issue",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept B", "CC-2"),
			],
		)
		validate_manufacturing_dimension_uniformity(doc)  # no throw

	def test_g8_ordinary_material_transfer_mixed_not_blocked(self):
		doc = _doc(
			"Material Transfer",
			[
				_row(1, "ITEM-A", "Dept A", "CC-1"),
				_row(2, "ITEM-B", "Dept B", "CC-2"),
			],
		)
		validate_manufacturing_dimension_uniformity(doc)  # no throw
