# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import unittest
from types import SimpleNamespace

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	_align_po_pi_si_row,
	align_purchase_invoice_item_amounts,
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


class TestInvoiceDistributedDiscountAlign(unittest.TestCase):
	"""Regression: ACC-PINV-2026-00327 pattern (distributed_discount_amount=105)."""

	def test_align_preserves_discounted_net_amount(self):
		row = _row()
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.net_amount), 241500000.0)
		self.assertEqual(flt(row.base_net_amount), 241500000.0)
		# Gross amount remains rate-first.
		self.assertEqual(flt(row.amount), 241500105.0)
		self.assertEqual(flt(row.qty) * flt(row.net_rate) - flt(row.net_amount), 105.0)

	def test_align_without_distributed_discount_is_rate_first(self):
		row = _row(
			distributed_discount_amount=0.0,
			net_amount=241500000.0,  # stale / inconsistent on purpose
			base_net_amount=241500000.0,
		)
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.net_amount), 241500105.0)
		self.assertEqual(flt(row.base_net_amount), 241500105.0)

	def test_align_purchase_invoice_doc_wrapper(self):
		row = _row()

		class _Doc:
			doctype = "Purchase Invoice"
			company = "IRR-CO"
			currency = "IRR"

			def __init__(self, items):
				self.items = items

			def get(self, key, default=None):
				if key == "items":
					return self.items
				return getattr(self, key, default)

		doc = _Doc([row])
		from unittest.mock import patch

		with patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		):
			align_purchase_invoice_item_amounts(doc)
		self.assertEqual(flt(row.net_amount), 241500000.0)
