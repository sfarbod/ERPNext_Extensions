# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.25 — #59476 compat control flow must not activate over-billing when adjusting."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.integration import pr_pi_landed_cost_compat as compat


class TestCompatAdjustPathSkipsOverBilling(unittest.TestCase):
	def setUp(self):
		compat.uninstall_pr_pi_landed_cost_compat_patch()
		compat.install_pr_pi_landed_cost_compat_patch()

	def tearDown(self):
		# Leave patch installed for site workers; reinstall clean.
		compat.uninstall_pr_pi_landed_cost_compat_patch()
		compat.install_pr_pi_landed_cost_compat_patch()

	def test_adjust_true_does_not_call_orig_with_false(self):
		"""Regression: orig(..., False) triggered Over Billing on large PI vs PR."""
		calls = []

		def fake_orig(pr_doc, update_modified=True, adjust_incoming_rate=False):
			calls.append({"adjust": adjust_incoming_rate, "update_modified": update_modified})
			if not adjust_incoming_rate:
				raise AssertionError("over-billing path must not run when adjusting")

		pr = SimpleNamespace(
			items=[
				SimpleNamespace(
					name="row1",
					amount=1_000_000_000,
					billed_amt=26_900_332_000,
					rate=1_000_000,
					qty=1000,
					rejected_qty=0,
					precision=lambda *_a, **_k: 2,
					purchase_order_item="poi",
					base_net_amount=1_000_000_000,
					db_set=MagicMock(),
				)
			],
			conversion_rate=1,
			is_return=0,
			db_set=MagicMock(),
			set_status=MagicMock(),
			notify_update=MagicMock(),
			get=lambda k, d=None: getattr(pr, k, d),
		)

		with (
			patch.object(compat, "_original_update_billing_percentage", fake_orig),
			patch.object(compat, "core_needs_59476_compat", return_value=True),
			patch.object(compat, "apply_59476_amount_differences", return_value=25_900_332_000.0) as apply_mock,
			patch(
				"erpnext.stock.doctype.purchase_receipt.purchase_receipt.adjust_incoming_rate_for_pr"
			) as adj_mock,
			patch.object(compat, "_update_billing_status_without_rate_adjust", return_value=100.0) as bill_mock,
		):
			compat._wrapped_update_billing_percentage(pr, update_modified=True, adjust_incoming_rate=True)

		self.assertEqual(calls, [], "Core orig must not be invoked on adjust=True compat path")
		apply_mock.assert_called_once_with(pr)
		bill_mock.assert_called_once()
		adj_mock.assert_called_once_with(pr)

	def test_adjust_false_delegates_to_core(self):
		seen = {}

		def fake_orig(pr_doc, update_modified=True, adjust_incoming_rate=False):
			seen["adjust"] = adjust_incoming_rate
			seen["update_modified"] = update_modified

		with (
			patch.object(compat, "_original_update_billing_percentage", fake_orig),
			patch.object(compat, "core_needs_59476_compat", return_value=True),
		):
			compat._wrapped_update_billing_percentage(
				SimpleNamespace(), update_modified=False, adjust_incoming_rate=False
			)
		self.assertEqual(seen, {"adjust": False, "update_modified": False})

	def test_upstream_core_passthrough(self):
		seen = {}

		def fake_orig(pr_doc, update_modified=True, adjust_incoming_rate=False):
			seen["adjust"] = adjust_incoming_rate

		with (
			patch.object(compat, "_original_update_billing_percentage", fake_orig),
			patch.object(compat, "core_needs_59476_compat", return_value=False),
		):
			compat._wrapped_update_billing_percentage(
				SimpleNamespace(), update_modified=True, adjust_incoming_rate=True
			)
		self.assertEqual(seen["adjust"], True)

	def test_59476_large_positive_price_difference(self):
		adj = compat._compute_adjusted_amt_59476(
			billed_amt=26_900_332_000,
			qty=1000,
			item_qty=1000,
			base_net_amount=1_000_000_000,
			precision=2,
		)
		self.assertEqual(flt(adj), 25_900_332_000.0)

	def test_59476_equal_rates_zero_diff(self):
		adj = compat._compute_adjusted_amt_59476(
			billed_amt=800_000_000,
			qty=800,
			item_qty=800,
			base_net_amount=800_000_000,
			precision=2,
		)
		self.assertEqual(flt(adj), 0.0)

	def test_59476_lower_pi_negative_diff(self):
		adj = compat._compute_adjusted_amt_59476(
			billed_amt=900,
			qty=1,
			item_qty=1,
			base_net_amount=1000,
			precision=2,
		)
		self.assertEqual(flt(adj), -100.0)

	def test_install_idempotent(self):
		compat.uninstall_pr_pi_landed_cost_compat_patch()
		a = compat.install_pr_pi_landed_cost_compat_patch()
		b = compat.install_pr_pi_landed_cost_compat_patch()
		self.assertIn(a["status"], ("installed", "already_installed", "skipped_core_has_upstream"))
		self.assertEqual(b["status"], "already_installed")


class TestBillingStatusHelper(unittest.TestCase):
	def test_per_billed_full_invoice(self):
		item = SimpleNamespace(
			name="r1",
			amount=1_000_000_000,
			billed_amt=1_000_000_000,
			rate=1_000_000,
			qty=1000,
			rejected_qty=0,
			precision=lambda *_a, **_k: 2,
		)
		pr = SimpleNamespace(
			items=[item],
			is_return=0,
			db_set=MagicMock(),
			set_status=MagicMock(),
			notify_update=MagicMock(),
			get=lambda k, d=None: 0 if k == "is_return" else d,
		)
		with patch(
			"erpnext.stock.doctype.purchase_receipt.purchase_receipt.get_item_wise_returned_qty",
			return_value={},
		), patch("frappe.get_single") as gs:
			gs.return_value = SimpleNamespace(bill_for_rejected_quantity_in_purchase_invoice=0)
			pct = compat._update_billing_status_without_rate_adjust(pr, update_modified=True, pi_landed_cost_amount=0)
		self.assertEqual(pct, 100.0)
		pr.db_set.assert_called()
		pr.set_status.assert_called_once_with(update=True)


if __name__ == "__main__":
	unittest.main()
