# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	_align_po_pi_si_row,
	align_purchase_invoice_item_amounts,
	align_purchase_order_item_amounts,
	align_sales_invoice_item_amounts,
)


def _row(**kwargs):
	defaults = {
		"qty": 231.0,
		"rate": 1045455.0,
		"amount": 241500105.0,
		"net_rate": 1045455.0,
		"net_amount": 241500000.0,
		"base_rate": 1045455.0,
		"base_amount": 241500105.0,
		"base_net_rate": 1045455.0,
		"base_net_amount": 241500000.0,
		"distributed_discount_amount": 105.0,
		"discount_percentage": 0.0,
		"discount_amount": 184545.0,
	}
	defaults.update(kwargs)

	def _get(key, default=None):
		return getattr(ns, key, default)

	def _set(key, value):
		setattr(ns, key, value)

	ns = SimpleNamespace(**defaults)
	ns.get = _get
	ns.set = _set
	return ns


class _Doc:
	def __init__(self, items, *, doctype="Purchase Invoice", company="IRR-CO", currency="IRR"):
		self.doctype = doctype
		self.company = company
		self.currency = currency
		self.items = items

	def get(self, key, default=None):
		if key == "items":
			return self.items
		return getattr(self, key, default)


class TestInvoiceDistributedDiscountAlign(unittest.TestCase):
	"""Regression: ACC-PINV-2026-00327 pattern (distributed_discount_amount=105)."""

	def test_exact_production_pattern_preserves_net_amount(self):
		row = _row()
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.net_amount), 241500000.0)
		self.assertNotEqual(flt(row.net_amount), 241500105.0)
		self.assertEqual(flt(row.qty) * flt(row.net_rate), 241500105.0)
		# Gross amount remains rate-first.
		self.assertEqual(flt(row.amount), 241500105.0)

	def test_base_net_amount_preserved_with_distributed_discount(self):
		row = _row()
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.base_net_amount), 241500000.0)
		self.assertEqual(flt(row.base_amount), 241500105.0)

	def test_no_distributed_discount_rate_first_unchanged(self):
		row = _row(
			distributed_discount_amount=0.0,
			net_amount=241500000.0,
			base_net_amount=241500000.0,
		)
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.net_amount), 241500105.0)
		self.assertEqual(flt(row.base_net_amount), 241500105.0)

	def test_none_distributed_discount_behaves_as_old_path(self):
		row = _row(
			distributed_discount_amount=None,
			net_amount=241500000.0,
			base_net_amount=241500000.0,
		)
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.net_amount), 241500105.0)

	def test_multiple_rows_with_distributed_discount(self):
		rows = [
			_row(
				qty=10,
				rate=1000,
				amount=10000,
				net_rate=1000,
				net_amount=9667,
				base_rate=1000,
				base_amount=10000,
				base_net_rate=1000,
				base_net_amount=9667,
				distributed_discount_amount=333,
			),
			_row(
				qty=20,
				rate=1000,
				amount=20000,
				net_rate=1000,
				net_amount=19333,
				base_rate=1000,
				base_amount=20000,
				base_net_rate=1000,
				base_net_amount=19333,
				distributed_discount_amount=667,
			),
		]
		for row in rows:
			_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(rows[0].net_amount), 9667)
		self.assertEqual(flt(rows[1].net_amount), 19333)
		self.assertEqual(flt(rows[0].base_net_amount), 9667)
		self.assertEqual(flt(rows[1].base_net_amount), 19333)

	def test_fractional_distributed_discount_preserved(self):
		import erpnext_extensions.iran_accounting.domain.currency as currency_mod

		row = _row(
			qty=3,
			rate=10.0,
			amount=30.0,
			net_rate=10.0,
			net_amount=26.67,
			base_rate=10.0,
			base_amount=30.0,
			base_net_rate=10.0,
			base_net_amount=26.67,
			distributed_discount_amount=3.33,
		)
		with patch.object(currency_mod, "get_currency_precision", return_value=2):
			_align_po_pi_si_row(row, "USD", "USD")
		self.assertEqual(row.net_amount, 26.67)
		self.assertEqual(row.base_net_amount, 26.67)

	def test_foreign_currency_preserves_base_net_amount(self):
		"""Must not rebuild base_net_amount from qty × base_net_rate when discount allocated."""
		import erpnext_extensions.iran_accounting.domain.currency as currency_mod

		row = _row(
			qty=10,
			rate=100,  # USD
			amount=1000,
			net_rate=100,
			net_amount=900,  # USD net after $100 discount
			base_rate=4200000,  # IRR equiv (illustrative)
			base_amount=42000000,
			base_net_rate=4200000,
			base_net_amount=37800000,  # allocated; ≠ 10 × 4200000
			distributed_discount_amount=100,
		)
		with patch.object(
			currency_mod,
			"get_currency_precision",
			side_effect=lambda ccy: 0 if ccy == "IRR" else 2,
		):
			_align_po_pi_si_row(row, "IRR", "USD")
		self.assertEqual(flt(row.net_amount), 900)
		self.assertEqual(flt(row.base_net_amount), 37800000)
		self.assertNotEqual(flt(row.base_net_amount), flt(row.qty) * flt(row.base_net_rate))

	def test_align_wrappers_pi_si_po(self):
		row_pi = _row()
		row_si = _row()
		row_po = _row()
		with patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		):
			align_purchase_invoice_item_amounts(_Doc([row_pi], doctype="Purchase Invoice"))
			align_sales_invoice_item_amounts(_Doc([row_si], doctype="Sales Invoice"))
			align_purchase_order_item_amounts(_Doc([row_po], doctype="Purchase Order"))
		self.assertEqual(flt(row_pi.net_amount), 241500000.0)
		self.assertEqual(flt(row_si.net_amount), 241500000.0)
		self.assertEqual(flt(row_po.net_amount), 241500000.0)
