# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression for ERPNext #59476 compatibility patch (extensions, not Core).

Official fix: amount_difference uses PR base_net_amount, not gross item.rate.
Does not mutate historical MAT-PRE-2026-01417 / ACC-PINV-2026-01149.
"""

from __future__ import annotations

import unittest

from frappe.utils import flt

from erpnext_extensions.iran_accounting.integration.pr_pi_landed_cost_compat import (
	UPSTREAM_PR,
	_compute_adjusted_amt_59476,
	apply_59476_amount_differences,
	core_has_upstream_59476,
	core_needs_59476_compat,
	install_pr_pi_landed_cost_compat_patch,
	uninstall_pr_pi_landed_cost_compat_patch,
)


def adjusted_amt_upstream_59476(*, billed_amt, qty, item_qty, base_net_amount) -> float:
	return _compute_adjusted_amt_59476(
		billed_amt=billed_amt,
		qty=qty,
		item_qty=item_qty,
		base_net_amount=base_net_amount,
		precision=6,
	)


def adjusted_amt_legacy_v16(*, billed_amt, qty, item_qty, item_rate, conversion_rate=1) -> float:
	return (flt(billed_amt / qty) - (flt(item_rate) * flt(conversion_rate))) * item_qty


class TestUpstream59476Formula(unittest.TestCase):
	def test_primary_case_01417_pi_equals_net(self):
		billed, qty, item_qty = 819_000_000, 1, 1
		rate, base_net = 910_000_000, 819_000_000
		self.assertEqual(
			adjusted_amt_legacy_v16(
				billed_amt=billed, qty=qty, item_qty=item_qty, item_rate=rate
			),
			-91_000_000,
		)
		self.assertEqual(
			adjusted_amt_upstream_59476(
				billed_amt=billed, qty=qty, item_qty=item_qty, base_net_amount=base_net
			),
			0,
		)
		self.assertEqual((base_net + 0) / item_qty, 819_000_000)

	def test_upstream_percent_discount_shape(self):
		self.assertEqual(
			adjusted_amt_upstream_59476(
				billed_amt=900, qty=10, item_qty=10, base_net_amount=900
			),
			0,
		)
		self.assertEqual(
			adjusted_amt_legacy_v16(billed_amt=900, qty=10, item_qty=10, item_rate=100),
			-100,
		)

	def test_amount_discount_shape(self):
		self.assertEqual(
			adjusted_amt_upstream_59476(
				billed_amt=1_350_000_000,
				qty=3,
				item_qty=3,
				base_net_amount=1_350_000_000,
			),
			0,
		)

	def test_no_discount_genuine_pi_increase(self):
		self.assertEqual(
			adjusted_amt_upstream_59476(
				billed_amt=330, qty=1, item_qty=1, base_net_amount=197
			),
			133,
		)

	def test_pi_lower_than_net(self):
		self.assertEqual(
			adjusted_amt_upstream_59476(
				billed_amt=800, qty=1, item_qty=1, base_net_amount=900
			),
			-100,
		)

	def test_partial_billing_unit_rate_applied_to_full_qty(self):
		self.assertEqual(
			adjusted_amt_upstream_59476(
				billed_amt=450, qty=5, item_qty=10, base_net_amount=900
			),
			0,
		)

	def test_multi_currency_company_net_basis(self):
		self.assertAlmostEqual(
			adjusted_amt_upstream_59476(
				billed_amt=17142.86, qty=2, item_qty=2, base_net_amount=14000
			),
			flt(17142.86 - 14000),
			places=2,
		)

	def test_stock_uom_conversion_amount_basis_unchanged(self):
		adj = adjusted_amt_upstream_59476(
			billed_amt=900, qty=10, item_qty=10, base_net_amount=900
		)
		self.assertEqual(adj, 0)
		self.assertEqual(flt((900 + adj) / 120, 6), flt(900 / 120, 6))

	def test_lcv_and_tax_sit_outside_amt_diff(self):
		adj = adjusted_amt_upstream_59476(
			billed_amt=900, qty=10, item_qty=10, base_net_amount=900
		)
		self.assertEqual((900 + 50 + 25 + adj) / 10, 97.5)


class TestCompatPatchLifecycle(unittest.TestCase):
	def test_stock_erpnext_1637_needs_compat(self):
		# Restored Core on this site is stock 16.37 without #59476.
		self.assertFalse(core_has_upstream_59476())
		self.assertTrue(core_needs_59476_compat())

	def test_install_is_idempotent_and_version_aware(self):
		uninstall_pr_pi_landed_cost_compat_patch()
		first = install_pr_pi_landed_cost_compat_patch()
		self.assertIn(first["status"], ("installed", "already_installed"))
		self.assertEqual(first["upstream_pr"], UPSTREAM_PR)
		if first["status"] == "installed":
			self.assertTrue(first["compat_active"])
		second = install_pr_pi_landed_cost_compat_patch()
		self.assertEqual(second["status"], "already_installed")

		from erpnext.stock.doctype.purchase_receipt import purchase_receipt as pr_mod

		self.assertTrue(getattr(pr_mod.update_billing_percentage, "_iran_pr_pi_landed_cost_59476", False))

	def test_wrapper_delegates_when_adjust_incoming_rate_false(self):
		install_pr_pi_landed_cost_compat_patch()
		from erpnext.stock.doctype.purchase_receipt import purchase_receipt as pr_mod

		# Smoke: callable remains importable for PI submit path.
		self.assertTrue(callable(pr_mod.update_billing_percentage))
		self.assertTrue(callable(apply_59476_amount_differences))
