# Copyright (c) 2026, ERPNext Extensions contributors
"""Commit 3: ERPNext-first Stock Entry GL ownership."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting import zero_value_transfer as zvt
from erpnext_extensions.iran_accounting.integration import monkey_patches


class TestStockEntryGLDelegation(unittest.TestCase):
	def test_normal_manufacture_delegates_to_original(self):
		calls = []

		def original(self, inventory_account_map=None):
			calls.append(("original", self.purpose, inventory_account_map))
			return [{"account": "Stock", "debit": 1, "credit": 0}]

		class Doc:
			doctype = "Stock Entry"
			purpose = "Manufacture"
			company = "Test"
			name = "STE-MFG"
			total_incoming_value = 200
			total_outgoing_value = 100
			value_difference = 100

			def get_debit_field_precision(self):
				return 0

			def set_total_incoming_outgoing_value(self):
				pass

			def get(self, k, default=None):
				return getattr(self, k, default)

		doc = Doc()
		with (
			mock.patch.object(zvt, "_should_force_balanced_transfer_gl", return_value=False),
			mock.patch.object(zvt, "resolve_original_stock_entry_get_gl_entries", return_value=original),
			mock.patch.object(zvt, "build_iran_balanced_transfer_gl") as balanced,
		):
			out = zvt.iran_stock_entry_get_gl_entries(doc, {"x": 1})
		self.assertEqual(len(calls), 1)
		balanced.assert_not_called()
		self.assertEqual(out[0]["account"], "Stock")

	def test_repack_and_material_receipt_delegate(self):
		for purpose in ("Repack", "Material Receipt"):
			calls = []

			def original(self, inventory_account_map=None):
				calls.append(self.purpose)
				return []

			class Doc:
				doctype = "Stock Entry"
				company = "Test"
				name = "STE"
				total_incoming_value = 10
				total_outgoing_value = 0
				value_difference = 10

				def __init__(self, purpose):
					self.purpose = purpose

				def get_debit_field_precision(self):
					return 0

				def set_total_incoming_outgoing_value(self):
					pass

				def get(self, k, default=None):
					return getattr(self, k, default)

			doc = Doc(purpose)
			with (
				mock.patch.object(zvt, "_should_force_balanced_transfer_gl", return_value=False),
				mock.patch.object(zvt, "resolve_original_stock_entry_get_gl_entries", return_value=original),
				mock.patch.object(zvt, "build_iran_balanced_transfer_gl") as balanced,
			):
				zvt.iran_stock_entry_get_gl_entries(doc)
			self.assertEqual(calls, [purpose])
			balanced.assert_not_called()

	def test_zero_value_transfer_uses_iran_branch(self):
		class Doc:
			doctype = "Stock Entry"
			purpose = "Material Transfer"
			company = "Test"
			name = "STE-MT"
			total_incoming_value = 100
			total_outgoing_value = 100
			value_difference = 0

			def get_debit_field_precision(self):
				return 0

			def set_total_incoming_outgoing_value(self):
				pass

			def get(self, k, default=None):
				return getattr(self, k, default)

		doc = Doc()
		sentinel = [{"account": "Balanced"}]
		with (
			mock.patch.object(zvt, "_should_force_balanced_transfer_gl", return_value=True),
			mock.patch.object(zvt, "build_iran_balanced_transfer_gl", return_value=sentinel) as balanced,
			mock.patch.object(zvt, "resolve_original_stock_entry_get_gl_entries") as resolve_orig,
		):
			out = zvt.iran_stock_entry_get_gl_entries(doc)
		self.assertEqual(out, sentinel)
		balanced.assert_called_once()
		resolve_orig.assert_not_called()

	def test_original_called_exactly_once_for_normal(self):
		calls = {"n": 0}

		def original(self, inventory_account_map=None):
			calls["n"] += 1
			return []

		class Doc:
			doctype = "Stock Entry"
			purpose = "Manufacture"
			company = "Test"
			name = "STE"
			total_incoming_value = 5
			total_outgoing_value = 5
			value_difference = 0

			def get_debit_field_precision(self):
				return 0

			def set_total_incoming_outgoing_value(self):
				pass

			def get(self, k, default=None):
				return getattr(self, k, default)

		with (
			mock.patch.object(zvt, "_should_force_balanced_transfer_gl", return_value=False),
			mock.patch.object(zvt, "resolve_original_stock_entry_get_gl_entries", return_value=original),
		):
			zvt.iran_stock_entry_get_gl_entries(Doc())
			zvt.iran_stock_entry_get_gl_entries(Doc())
		self.assertEqual(calls["n"], 2)

	def test_preview_gl_columns_passes_currency_when_required(self):
		calls = []

		def two_arg(raw_columns, fields):
			calls.append(("two", raw_columns, fields))
			return ["ok2"]

		def three_arg(raw_columns, fields, currency):
			calls.append(("three", raw_columns, fields, currency))
			return ["ok3"]

		filters = type("F", (), {"company": "ESPAD"})()
		self.assertEqual(
			monkey_patches._preview_gl_columns(two_arg, ["c"], ["f"], filters),
			["ok2"],
		)
		with mock.patch.object(
			monkey_patches.erpnext, "get_company_currency", return_value="IRR"
		):
			self.assertEqual(
				monkey_patches._preview_gl_columns(three_arg, ["c"], ["f"], filters),
				["ok3"],
			)
		self.assertEqual(calls[0][0], "two")
		self.assertEqual(calls[1], ("three", ["c"], ["f"], "IRR"))

	def test_patch_installation_idempotent(self):
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		# Ensure patches applied
		monkey_patches.apply_monkey_patches()
		original = StockEntry._iran_original_stock_entry_get_gl_entries
		self.assertTrue(callable(original))
		self.assertFalse(getattr(original, "_iran_stock_entry_gl_wrapper", False))
		wrapper = StockEntry.get_gl_entries
		self.assertTrue(getattr(wrapper, "_iran_stock_entry_gl_wrapper", False))

		# Second apply must not overwrite original with wrapper
		monkey_patches.apply_monkey_patches()
		self.assertIs(StockEntry._iran_original_stock_entry_get_gl_entries, original)
		self.assertTrue(getattr(StockEntry.get_gl_entries, "_iran_stock_entry_gl_wrapper", False))

	def test_resolve_rejects_wrapper_as_original(self):
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		def fake_wrapper(*args, **kwargs):
			return []

		fake_wrapper._iran_stock_entry_gl_wrapper = True
		prev = getattr(StockEntry, "_iran_original_stock_entry_get_gl_entries", None)
		try:
			StockEntry._iran_original_stock_entry_get_gl_entries = fake_wrapper
			with self.assertRaises(Exception):
				zvt.resolve_original_stock_entry_get_gl_entries()
		finally:
			if prev is not None:
				StockEntry._iran_original_stock_entry_get_gl_entries = prev


if __name__ == "__main__":
	unittest.main()
