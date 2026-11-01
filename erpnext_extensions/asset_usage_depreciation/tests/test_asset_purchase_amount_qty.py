# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Contract tests for Asset purchase-amount × quantity recalculation.

The fix lives in::

    asset_usage_depreciation/public/js/asset_purchase_amount_qty.js

These tests mirror the pure JS helpers (unit rate from purchase-values response,
amount = unit_rate × asset_quantity). They do **not** drive the Desk form.

Manual UI verification (development.localhost)::

1. New Asset → Item 14200048 → PR MAT-PRE-2026-01997
   (core may fill qty 10 / amount 200M — unchanged by this fix).
2. Set asset_quantity 1 → expect net_purchase_amount 20,000,000.
3. Set asset_quantity 3 → expect 60,000,000.
4. Set asset_quantity 5 → expect 100,000,000.
5. Clear purchase receipt → changing qty must not invent an amount.
6. Save draft with qty 1 / 20M → purchase_amount matches; row link retained.

Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_asset_purchase_amount_qty \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest

from frappe.utils import flt


def unit_rate_from_purchase_values(net_purchase_amount, remaining_qty):
	"""Mirror of JS ``unit_rate_from_purchase_values``."""
	rem = flt(remaining_qty)
	if not rem > 0:
		return None
	return flt(net_purchase_amount) / rem


def amount_for_quantity(unit_rate, asset_quantity, precision=None):
	"""Mirror of JS ``amount_for_quantity``."""
	qty = flt(asset_quantity)
	rate = flt(unit_rate)
	if not qty > 0 or not rate > 0:
		return None
	if precision is None:
		return flt(rate * qty)
	return flt(rate * qty, precision)


class TestAssetPurchaseAmountQtyFormula(unittest.TestCase):
	def test_unit_rate_from_remaining_row_total(self):
		unit = unit_rate_from_purchase_values(200_000_000, 10)
		self.assertEqual(unit, 20_000_000)

	def test_conversion_factor_baked_into_response_total(self):
		# Server already applies valuation_rate × conversion_factor × remaining.
		# Client divides net by remaining qty only — must not divide by CF again.
		net = 20_000_000 * 2 * 10  # rate 20M, CF 2, remaining 10
		unit = unit_rate_from_purchase_values(net, 10)
		self.assertEqual(unit, 40_000_000)
		self.assertEqual(amount_for_quantity(unit, 1), 40_000_000)

	def test_qty_change_matrix_rate_20m(self):
		unit = unit_rate_from_purchase_values(200_000_000, 10)
		self.assertEqual(amount_for_quantity(unit, 1), 20_000_000)
		self.assertEqual(amount_for_quantity(unit, 3), 60_000_000)
		self.assertEqual(amount_for_quantity(unit, 5), 100_000_000)
		self.assertEqual(amount_for_quantity(unit, 10), 200_000_000)

	def test_invalid_qty_or_rate_returns_none(self):
		self.assertIsNone(unit_rate_from_purchase_values(200_000_000, 0))
		self.assertIsNone(amount_for_quantity(20_000_000, 0))
		self.assertIsNone(amount_for_quantity(0, 5))
		self.assertIsNone(amount_for_quantity(None, 5))

	def test_grouped_asset_qty_change_still_uses_unit_rate(self):
		# Grouped defaults remain core's remaining qty; subsequent edits rescale.
		unit = unit_rate_from_purchase_values(200_000_000, 10)
		self.assertEqual(amount_for_quantity(unit, 10), 200_000_000)
		self.assertEqual(amount_for_quantity(unit, 5), 100_000_000)


class TestAssetPurchaseAmountQtyHooks(unittest.TestCase):
	def test_asset_doctype_js_hook_registered(self):
		import frappe

		hooks = frappe.get_hooks("doctype_js") or {}
		asset_js = hooks.get("Asset") or []
		if isinstance(asset_js, str):
			asset_js = [asset_js]
		self.assertTrue(
			any("asset_purchase_amount_qty.js" in path for path in asset_js),
			f"Asset doctype_js missing purchase-amount script: {asset_js}",
		)

	def test_no_whitelist_override_for_get_values(self):
		import frappe

		overrides = frappe.get_hooks("override_whitelisted_methods") or {}
		key = "erpnext.assets.doctype.asset.asset.get_values_from_purchase_doc"
		self.assertNotIn(key, overrides)
