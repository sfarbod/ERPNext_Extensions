# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Unit tests for stock measures and Item Group hierarchy helpers (no DB stock fixtures)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.account_explorer.stock_measures import (
	finalize_stock_measures,
	row_has_stock_activity,
	stock_row_from_buckets,
	sum_stock_measure_rows,
)


class TestStockMeasures(unittest.TestCase):
	def test_opening_rolled_into_inward_balance(self):
		# Opening 1000 + period in 400 - out 250 → Inward 1400, Balance 1150 → Debit 1150
		row = stock_row_from_buckets(
			opening_value=1000,
			inward_value=400,
			outward_value=250,
			include_qty=False,
		)
		self.assertNotIn("opening_value", row)
		self.assertEqual(row["inward_value"], 1400.0)
		self.assertEqual(row["outward_value"], 250.0)
		self.assertEqual(row["balance_value"], 1150.0)
		self.assertEqual(row["debit_balance"], 1150.0)
		self.assertEqual(row["credit_balance"], 0.0)

	def test_opening_rolled_into_in_qty_balance(self):
		row = stock_row_from_buckets(
			opening_qty=10,
			in_qty=5,
			out_qty=3,
			opening_value=0,
			inward_value=0,
			outward_value=0,
			include_qty=True,
		)
		self.assertNotIn("opening_qty", row)
		self.assertEqual(row["in_qty"], 15.0)
		self.assertEqual(row["out_qty"], 3.0)
		self.assertEqual(row["balance_qty"], 12.0)

	def test_example_contract_receipt_and_issue(self):
		row = stock_row_from_buckets(
			opening_qty=100,
			in_qty=20,
			out_qty=30,
			opening_value=1_000_000,
			inward_value=200_000,
			outward_value=300_000,
			include_qty=True,
		)
		self.assertEqual(row["in_qty"], 120.0)
		self.assertEqual(row["out_qty"], 30.0)
		self.assertEqual(row["balance_qty"], 90.0)
		self.assertEqual(row["inward_value"], 1_200_000.0)
		self.assertEqual(row["outward_value"], 300_000.0)
		self.assertEqual(row["balance_value"], 900_000.0)
		self.assertEqual(row["debit_balance"], 900_000.0)

	def test_opening_only_display(self):
		row = stock_row_from_buckets(
			opening_qty=100,
			opening_value=1_000_000,
			include_qty=True,
		)
		self.assertEqual(row["in_qty"], 100.0)
		self.assertEqual(row["balance_value"], 1_000_000.0)
		self.assertEqual(row["debit_balance"], 1_000_000.0)
		self.assertTrue(row_has_stock_activity(row))

	def test_zero_row_exclusion(self):
		empty = stock_row_from_buckets(include_qty=True)
		self.assertFalse(row_has_stock_activity(empty))
		active = stock_row_from_buckets(in_qty=1, include_qty=True)
		self.assertTrue(row_has_stock_activity(active))

	def test_negative_closing_credit_balance(self):
		row = stock_row_from_buckets(inward_value=10, outward_value=40, include_qty=False)
		self.assertEqual(row["balance_value"], -30.0)
		self.assertEqual(row["debit_balance"], 0.0)
		self.assertEqual(row["credit_balance"], 30.0)

	def test_zero_closing_both_sides_zero(self):
		row = stock_row_from_buckets(inward_value=10, outward_value=10, include_qty=False)
		self.assertEqual(row["balance_value"], 0.0)
		self.assertEqual(row["debit_balance"], 0.0)
		self.assertEqual(row["credit_balance"], 0.0)

	def test_footer_side_nets_offsetting_leaves(self):
		# Leaf +100 and Leaf -30 → signed 70 → Debit 70 / Credit 0 (not Debit 100 / Credit 30)
		rows = [
			stock_row_from_buckets(inward_value=100, outward_value=0, include_qty=False),
			stock_row_from_buckets(inward_value=0, outward_value=30, include_qty=False),
		]
		total = sum_stock_measure_rows(rows, include_qty=False)
		self.assertEqual(total["balance_value"], 70.0)
		self.assertEqual(total["debit_balance"], 70.0)
		self.assertEqual(total["credit_balance"], 0.0)

	def test_sum_stock_rows(self):
		rows = [
			stock_row_from_buckets(opening_value=10, inward_value=5, outward_value=2, include_qty=False),
			stock_row_from_buckets(opening_value=20, inward_value=1, outward_value=4, include_qty=False),
		]
		total = sum_stock_measure_rows(rows, include_qty=False)
		self.assertNotIn("opening_value", total)
		self.assertEqual(total["inward_value"], 36.0)  # 15 + 21
		self.assertEqual(total["outward_value"], 6.0)
		self.assertEqual(total["balance_value"], 30.0)
		self.assertEqual(total["debit_balance"], 30.0)
		self.assertEqual(total["credit_balance"], 0.0)
		self.assertNotIn("period_debit", total)

	def test_item_footer_qty_and_value_contract(self):
		rows = [
			stock_row_from_buckets(
				opening_qty=10,
				in_qty=5,
				out_qty=2,
				opening_value=100,
				inward_value=50,
				outward_value=20,
				include_qty=True,
			),
			stock_row_from_buckets(
				opening_qty=0,
				in_qty=3,
				out_qty=1,
				opening_value=0,
				inward_value=30,
				outward_value=10,
				include_qty=True,
			),
		]
		totals = sum_stock_measure_rows(rows, include_qty=True)
		self.assertEqual(totals["in_qty"], 18.0)
		self.assertEqual(totals["out_qty"], 3.0)
		self.assertEqual(totals["balance_qty"], 15.0)
		self.assertEqual(totals["inward_value"], 180.0)
		self.assertEqual(totals["outward_value"], 30.0)
		self.assertEqual(totals["balance_value"], 150.0)
		self.assertEqual(totals["debit_balance"], 150.0)
		self.assertEqual(totals["credit_balance"], 0.0)

	def test_finalize_idempotent_on_display_fields(self):
		row = {"inward_value": 3, "outward_value": 1}
		finalize_stock_measures(row, include_qty=False)
		finalize_stock_measures(row, include_qty=False)
		self.assertEqual(row["balance_value"], 2.0)
		self.assertNotIn("opening_value", row)


class TestItemGroupHierarchyHelpers(unittest.TestCase):
	def test_map_leaf_to_presentation_group(self):
		from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
			map_leaf_to_presentation_group,
		)

		parents = [{"name": "Parent", "lft": 1, "rgt": 10, "is_group": 1}]
		self.assertEqual(map_leaf_to_presentation_group("Parent", parents), "Parent")
		self.assertIsNone(map_leaf_to_presentation_group("", parents))
		self.assertIsNone(map_leaf_to_presentation_group("Unknown", []))
