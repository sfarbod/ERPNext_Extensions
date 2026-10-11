# Copyright (c) 2026, ERPNext Extensions contributors
"""Option B: Manufacturing Dimension Uniformity runs at before_submit only."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.manufacturing_dimension.guard import (
	validate_manufacturing_dimension_uniformity,
)
from erpnext_extensions.iran_accounting.stock_entry import (
	before_submit_stock_entry,
	validate_stock_entry,
)


def _row(idx, item_code, department="", cost_center=""):
	return SimpleNamespace(
		idx=idx,
		item_code=item_code,
		department=department,
		cost_center=cost_center,
		name=f"row-{idx}",
	)


def _mismatched_manufacture():
	return SimpleNamespace(
		doctype="Stock Entry",
		purpose="Manufacture",
		company="Test Co",
		items=[
			_row(1, "ITEM-FG", "Dept A", "CC-1130"),
			_row(2, "ITEM-SCRAP", "Dept A", "CC-2100"),
		],
	)


class TestDimensionGuardTiming(unittest.TestCase):
	"""Draft Save must not run the guard; before_submit must."""

	def test_validate_stock_entry_does_not_block_dimension_mismatch(self):
		doc = _mismatched_manufacture()
		with (
			patch(
				"erpnext_extensions.iran_accounting.e2e_bootstrap.apply_stock_entry_site_defaults"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
				return_value=False,
			),
			patch(
				"erpnext_extensions.iran_accounting.manufacturing_dimension.guard."
				"validate_manufacturing_dimension_uniformity"
			) as guard,
		):
			validate_stock_entry(doc)
			guard.assert_not_called()

	def test_validate_stock_entry_allows_mismatch_when_irr_checks_are_stubbed(self):
		"""Even for IRR companies, validate must not invoke the dim guard."""
		doc = _mismatched_manufacture()
		with (
			patch(
				"erpnext_extensions.iran_accounting.e2e_bootstrap.apply_stock_entry_site_defaults"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
				return_value=True,
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.align_stock_entry_item_amounts"
			),
			patch("erpnext_extensions.iran_accounting.stock_entry.round_stock_entry_totals"),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.apply_iran_manufacture_output_contract"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.align_manufacture_finished_good_residual"
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.runtime_guard."
				"assert_outgoing_rates_not_silently_zeroed"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.assert_stock_entry_valuation_integrity"
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual."
				"assert_round_off_ready_if_needed"
			),
			patch(
				"erpnext_extensions.iran_accounting.manufacturing_dimension.guard."
				"validate_manufacturing_dimension_uniformity"
			) as guard,
		):
			validate_stock_entry(doc)
			guard.assert_not_called()

	def test_before_submit_blocks_dimension_mismatch(self):
		doc = _mismatched_manufacture()
		with self.assertRaises(frappe.ValidationError) as ctx:
			before_submit_stock_entry(doc)
		self.assertIn("Manufacturing Dimension Mismatch", str(ctx.exception))

	def test_before_submit_passes_uniform_dimensions(self):
		doc = SimpleNamespace(
			doctype="Stock Entry",
			purpose="Manufacture",
			company="Test Co",
			items=[
				_row(1, "ITEM-FG", "Dept A", "CC-1130"),
				_row(2, "ITEM-SCRAP", "Dept A", "CC-1130"),
			],
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.prevention."
				"apply_production_posting_order"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.validate_stock_entry"
			) as nested_validate,
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.runtime_guard."
				"assert_outgoing_rates_not_silently_zeroed"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
				return_value=False,
			),
		):
			before_submit_stock_entry(doc)
			nested_validate.assert_called_once()

	def test_before_submit_nested_validate_does_not_reinvoke_dim_guard(self):
		"""Nested validate_stock_entry after Option B must not call the dim guard again."""
		doc = SimpleNamespace(
			doctype="Stock Entry",
			purpose="Manufacture",
			company="Test Co",
			items=[
				_row(1, "ITEM-FG", "Dept A", "CC-1130"),
				_row(2, "ITEM-SCRAP", "Dept A", "CC-1130"),
			],
		)
		call_count = {"n": 0}
		real_guard = validate_manufacturing_dimension_uniformity

		def counting_guard(d, method=None):
			call_count["n"] += 1
			return real_guard(d, method)

		with (
			patch(
				"erpnext_extensions.iran_accounting.manufacturing_dimension.guard."
				"validate_manufacturing_dimension_uniformity",
				side_effect=counting_guard,
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_posting_order.prevention."
				"apply_production_posting_order"
			),
			patch(
				"erpnext_extensions.iran_accounting.e2e_bootstrap.apply_stock_entry_site_defaults"
			),
			patch(
				"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
				return_value=False,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.runtime_guard."
				"assert_outgoing_rates_not_silently_zeroed"
			),
		):
			before_submit_stock_entry(doc)
		# Authoritative call once in before_submit; nested validate must not add another.
		self.assertEqual(call_count["n"], 1)

	def test_non_guard_purposes_still_skip_at_before_submit(self):
		for purpose in (
			"Material Issue",
			"Material Receipt",
			"Material Transfer",
			"Repack",
		):
			doc = SimpleNamespace(
				doctype="Stock Entry",
				purpose=purpose,
				company="Test Co",
				items=[
					_row(1, "ITEM-A", "Dept A", "CC-1"),
					_row(2, "ITEM-B", "Dept B", "CC-2"),
				],
			)
			with (
				patch(
					"erpnext_extensions.iran_accounting.stock_posting_order.prevention."
					"apply_production_posting_order"
				),
				patch(
					"erpnext_extensions.iran_accounting.stock_entry.validate_stock_entry"
				),
				patch(
					"erpnext_extensions.iran_accounting.historical_stock.runtime_guard."
					"assert_outgoing_rates_not_silently_zeroed"
				),
				patch(
					"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
					return_value=False,
				),
			):
				before_submit_stock_entry(doc)  # no throw
