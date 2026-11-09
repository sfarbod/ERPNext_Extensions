# Copyright (c) 2026, ERPNext Extensions contributors
"""5.7.18 — Fixed Asset PR GL includes PI amount_difference (no double-count)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.integration import fa_pr_gl_amount_difference as mod
from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.asset_impact import (
	decide_asset_rebuild,
	expected_pi_aligned_acquisition_amount,
)
from erpnext_extensions.iran_accounting.financial_correction_campaign import (
	DENYLIST_JOURNAL_ENTRIES,
)


def _fa_item(**kwargs):
	defaults = dict(
		name="row1",
		is_fixed_asset=1,
		expense_account="121006 - FA - E",
		base_net_amount=1_290_000_000,
		item_tax_amount=0,
		landed_cost_voucher_amount=0,
		amount_difference_with_purchase_invoice=-64_500_000,
		qty=10,
		stock_qty=10,
		valuation_rate=122_550_000,
	)
	defaults.update(kwargs)
	item = SimpleNamespace(**defaults)
	item.precision = lambda field: 2  # noqa: ARG005
	return item


class TestFaPrGlAmountDifferenceHelpers(unittest.TestCase):
	def test_core_debit_excludes_amt_diff(self):
		item = _fa_item(item_tax_amount=1000, landed_cost_voucher_amount=2000)
		self.assertEqual(mod.core_fa_stock_value_diff(item), 1_290_003_000)

	def test_corrected_debit_includes_amt_diff_once(self):
		item = _fa_item(
			item_tax_amount=5_000_000,
			landed_cost_voucher_amount=10_000_000,
			amount_difference_with_purchase_invoice=-64_500_000,
		)
		core = mod.core_fa_stock_value_diff(item)
		corrected = mod.corrected_fa_stock_value_diff(item, include_amount_difference=True)
		self.assertEqual(core, 1_305_000_000)
		self.assertEqual(corrected, 1_240_500_000)
		# Not double-counted: corrected == core + amt_diff exactly once
		self.assertEqual(corrected, core + item.amount_difference_with_purchase_invoice)

	def test_negative_pi_difference_concrete_case(self):
		item = _fa_item()
		self.assertEqual(
			mod.corrected_fa_stock_value_diff(item, include_amount_difference=True),
			1_225_500_000,
		)

	def test_positive_pi_difference(self):
		item = _fa_item(amount_difference_with_purchase_invoice=50_000_000)
		self.assertEqual(
			mod.corrected_fa_stock_value_diff(item, include_amount_difference=True),
			1_340_000_000,
		)

	def test_zero_difference_noop(self):
		item = _fa_item(amount_difference_with_purchase_invoice=0)
		self.assertEqual(
			mod.corrected_fa_stock_value_diff(item, include_amount_difference=True),
			mod.core_fa_stock_value_diff(item),
		)

	def test_tax_plus_lcv_plus_amt_diff(self):
		item = _fa_item(
			item_tax_amount=1_000_000,
			landed_cost_voucher_amount=2_000_000,
			amount_difference_with_purchase_invoice=-500_000,
		)
		self.assertEqual(
			mod.corrected_fa_stock_value_diff(item, include_amount_difference=True),
			1_292_500_000,
		)

	@patch.object(mod, "invoice_based_valuation_enabled", return_value=False)
	def test_setting_disabled_skips_adjust(self, _mock):
		item = _fa_item()
		self.assertFalse(mod.should_adjust_fa_item(item))
		gl = [
			{
				"account": item.expense_account,
				"debit": 1_290_000_000,
				"credit": 0,
				"debit_in_account_currency": 1_290_000_000,
			}
		]
		n = mod.adjust_fixed_asset_amount_difference_debits(
			SimpleNamespace(items=[item], name="PR"), gl, 0
		)
		self.assertEqual(n, 0)
		self.assertEqual(gl[0]["debit"], 1_290_000_000)

	@patch.object(mod, "invoice_based_valuation_enabled", return_value=True)
	def test_stock_item_not_adjusted(self, _mock):
		item = _fa_item(is_fixed_asset=0)
		self.assertFalse(mod.should_adjust_fa_item(item))

	@patch.object(mod, "invoice_based_valuation_enabled", return_value=True)
	def test_adjust_fa_debit_and_grni_net_balance(self, _mock):
		"""Simulate core raw map then adjust FA debit — debit/credit equality."""
		item = _fa_item()
		# Core raw entries (before process_gl_map sign flip)
		gl = [
			{"account": item.expense_account, "debit": 1_290_000_000.0, "credit": 0.0},
			{"account": "211230 - GRNI - E", "debit": -1_290_000_000.0, "credit": 0.0},
			{
				"account": "211230 - GRNI - E",
				"debit": 0.0,
				"credit": -64_500_000.0,
				"remarks": "Adjustment based on Purchase Invoice rate",
			},
		]
		n = mod.adjust_fixed_asset_amount_difference_debits(
			SimpleNamespace(items=[item], name="MAT-PRE-2026-00747"), gl, 0
		)
		self.assertEqual(n, 1)
		self.assertEqual(gl[0]["debit"], 1_225_500_000.0)
		# Net after process-style aggregation
		fa_debit = flt(gl[0]["debit"])
		grni_credit = abs(flt(gl[1]["debit"])) + flt(gl[2]["credit"])  # 1.29B + (-64.5M)
		# credit field negative means reducing credit → net credit = 1.29B - 64.5M
		grni_net_credit = abs(flt(gl[1]["debit"])) + flt(gl[2]["credit"])
		self.assertEqual(fa_debit, 1_225_500_000.0)
		self.assertEqual(grni_net_credit, 1_225_500_000.0)
		self.assertEqual(fa_debit, grni_net_credit)

	@patch.object(mod, "invoice_based_valuation_enabled", return_value=True)
	def test_no_duplicate_adjustment_on_second_pass(self, _mock):
		item = _fa_item()
		gl = [{"account": item.expense_account, "debit": 1_290_000_000.0, "credit": 0.0}]
		doc = SimpleNamespace(items=[item], name="PR")
		self.assertEqual(mod.adjust_fixed_asset_amount_difference_debits(doc, gl, 0), 1)
		self.assertEqual(mod.adjust_fixed_asset_amount_difference_debits(doc, gl, 0), 0)
		self.assertEqual(gl[0]["debit"], 1_225_500_000.0)

	def test_patch_install_idempotent(self):
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt

		mod.uninstall_fa_pr_gl_amount_difference_patch()
		r1 = mod.install_fa_pr_gl_amount_difference_patch()
		r2 = mod.install_fa_pr_gl_amount_difference_patch()
		self.assertIn(r1["status"], ("installed", "already_installed"))
		self.assertEqual(r2["status"], "already_installed")
		self.assertTrue(mod.is_fa_pr_gl_amount_difference_patch_installed())
		self.assertTrue(getattr(PurchaseReceipt.make_item_gl_entries, mod._PATCH_ATTR, False))
		# Leave installed for site runtime consistency
		mod.install_fa_pr_gl_amount_difference_patch()

	def test_denylist_contains_finance_je(self):
		self.assertIn("ACC-JV-1405-169046", DENYLIST_JOURNAL_ENTRIES)


class TestAssetImpactDecisions(unittest.TestCase):
	def test_expected_acquisition_formula(self):
		row = {
			"base_net_amount": 1_290_000_000,
			"item_tax_amount": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": -64_500_000,
		}
		self.assertEqual(expected_pi_aligned_acquisition_amount(row), 1_225_500_000)

	def test_case_a_no_asset_handled_by_caller_flag(self):
		# decide_* is per-asset; Case A is absence
		pass

	def test_case_b_draft(self):
		d = decide_asset_rebuild(
			asset={"docstatus": 0, "gross_purchase_amount": 0},
			expected_acquisition=1_225_500_000,
			posted_depr_count=0,
			posted_depr_amount=0,
		)
		self.assertEqual(d["case"], "B_draft")
		self.assertFalse(d["rebuild_required"])
		self.assertTrue(d["update_draft_purchase_amount_recommended"])

	def test_case_d_submitted_unchanged(self):
		d = decide_asset_rebuild(
			asset={"docstatus": 1, "gross_purchase_amount": 1_225_500_000},
			expected_acquisition=1_225_500_000,
			posted_depr_count=0,
			posted_depr_amount=0,
		)
		self.assertEqual(d["case"], "D_submitted_cost_unchanged")
		self.assertFalse(d["rebuild_required"])

	def test_case_c_submitted_changed_no_posted(self):
		d = decide_asset_rebuild(
			asset={"docstatus": 1, "gross_purchase_amount": 1_290_000_000},
			expected_acquisition=1_225_500_000,
			posted_depr_count=0,
			posted_depr_amount=0,
		)
		self.assertEqual(d["case"], "C_submitted_cost_changed_no_posted_depr")
		self.assertTrue(d["rebuild_required"])
		self.assertTrue(d["safe_standard_rebuild"])

	def test_case_e_submitted_changed_with_posted(self):
		d = decide_asset_rebuild(
			asset={"docstatus": 1, "gross_purchase_amount": 1_290_000_000},
			expected_acquisition=1_225_500_000,
			posted_depr_count=3,
			posted_depr_amount=100_000,
		)
		self.assertEqual(d["case"], "E_submitted_cost_changed_with_posted_depr")
		self.assertTrue(d["rebuild_required"])
		self.assertFalse(d["safe_standard_rebuild"])
		self.assertTrue(d["historical_depr_preserve"])


class TestIrrIntegerVrUnaffected(unittest.TestCase):
	def test_integer_vr_from_amount_still_works(self):
		from erpnext_extensions.iran_accounting.domain.currency import (
			integer_valuation_rate_from_amount,
		)

		# PI-aligned amount / qty → integer IRR rate
		rate = integer_valuation_rate_from_amount(1_225_500_000, 10, "IRR")
		self.assertEqual(flt(rate), 122_550_000.0)

	def test_non_irr_round_monetary_unchanged_path(self):
		from erpnext_extensions.iran_accounting.domain import currency as currency_mod

		with patch.object(currency_mod, "get_currency_precision", return_value=4):
			out = currency_mod.round_monetary_rate(12.3456, "USD")
		self.assertAlmostEqual(flt(out), 12.3456, places=4)
		self.assertNotEqual(flt(out), 12.0)  # not forced to integer IRR policy


if __name__ == "__main__":
	unittest.main()
