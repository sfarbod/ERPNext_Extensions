# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.15 — Same-account Repack must not leave a single debit=credit GL row."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.manufacture_expense_account import (
	assert_gl_map_safe_for_make_gl_entries,
	assert_sa_is_residual_only,
	build_normalized_manufacture_gl_entries,
	drop_net_zero_merged_gl_rows,
	finalize_normalized_manufacture_gl_map,
	summarize_gl_by_account,
)
from erpnext_extensions.iran_accounting.tests.test_manufacture_expense_account_v5712 import (
	COMPANY,
	FG,
	OH,
	SA,
	WIP,
	_FakeSE,
	_sle,
)
from erpnext_extensions.iran_accounting.zero_value_transfer import (
	_should_skip_balanced_transfer_gl_pair,
)

INV = "111610 - موجودی مشترک مواد،بسته بندی و ... - E"


def _merge_by_account(gl_map, from_repost=False):
	"""Simulate Core merge_similar_entries collapsing same-account Dr+Cr onto one row."""
	_ = from_repost
	by: dict = {}
	for entry in gl_map or []:
		key = entry.get("account") or ""
		if key not in by:
			row = frappe._dict(entry)
			row.debit = 0.0
			row.credit = 0.0
			by[key] = row
		by[key].debit = flt(by[key].debit) + flt(entry.get("debit"))
		by[key].credit = flt(by[key].credit) + flt(entry.get("credit"))
	return list(by.values())


def _run_with_merge(doc, residual: float):
	with (
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.stock_adjustment_account",
			return_value=SA,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.round_currency",
			side_effect=lambda v, ccy: round(float(v)),
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.get_company_currency",
			return_value="IRR",
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.manufacture_expected_sa_residual_net",
			return_value=float(residual),
		),
		patch(
			"erpnext.accounts.general_ledger.process_gl_map",
			side_effect=_merge_by_account,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.frappe.db.exists",
			return_value=False,
		),
	):
		return build_normalized_manufacture_gl_entries(doc)


def _same_account_repack(*, out_svd: float, in_svd: float, residual: float, purpose="Repack"):
	doc = _FakeSE(purpose=purpose)
	doc.items = [
		frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}),
		frappe._dict({"name": "inn", "cost_center": "Main - E", "is_finished_item": 1}),
	]
	doc._inv_map = {"WH-A": {"account": INV}}
	doc._sle_map = {
		"out": [_sle("WH-A", out_svd, "out")],
		"inn": [_sle("WH-A", in_svd, "inn")],
	}
	doc.total_additional_costs = 0
	return _run_with_merge(doc, residual)


class TestDropNetZeroMergedGLRowsV5715(unittest.TestCase):
	def test_drop_equal_debit_credit_row(self):
		doc = _FakeSE(purpose="Repack")
		gl = [frappe._dict({"account": INV, "debit": 12825988, "credit": 12825988})]
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.round_currency",
				side_effect=lambda v, ccy: round(float(v)),
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.get_company_currency",
				return_value="IRR",
			),
		):
			out = drop_net_zero_merged_gl_rows(doc, gl)
		self.assertEqual(out, [])
		assert_gl_map_safe_for_make_gl_entries(out)

	def test_preserve_one_sided_residual(self):
		doc = _FakeSE()
		gl = [
			frappe._dict({"account": WIP, "debit": 0, "credit": 1000}),
			frappe._dict({"account": FG, "debit": 1005, "credit": 0}),
			frappe._dict({"account": SA, "debit": 0, "credit": 5}),
		]
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.round_currency",
				side_effect=lambda v, ccy: round(float(v)),
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.get_company_currency",
				return_value="IRR",
			),
		):
			out = drop_net_zero_merged_gl_rows(doc, gl)
		self.assertEqual(len(out), 3)
		assert_gl_map_safe_for_make_gl_entries(out)

	def test_assert_rejects_single_row(self):
		with self.assertRaises(frappe.ValidationError):
			assert_gl_map_safe_for_make_gl_entries(
				[frappe._dict({"account": INV, "debit": 1, "credit": 1})]
			)


class TestRepackSameAccountZeroGLV5715(unittest.TestCase):
	def test_same_item_batch_repack_equal_value_zero_gl(self):
		"""Case 1 — MAT-STE-2026-37328/37329 shape: equal SVD → empty GL."""
		gl = _same_account_repack(out_svd=-12825988, in_svd=12825988, residual=0)
		self.assertEqual(gl, [])
		assert_gl_map_safe_for_make_gl_entries(gl)

	def test_diff_item_same_account_equal_value_zero_gl(self):
		"""Case 2 — different items, same stock account, equal value → empty GL."""
		gl = _same_account_repack(out_svd=-137854445, in_svd=137854445, residual=0)
		self.assertEqual(gl, [])
		assert_gl_map_safe_for_make_gl_entries(gl)

	def test_diff_item_same_account_unequal_value_balanced_with_residual(self):
		"""Case 3 — unequal same-account: keep inventory net + single SA residual."""
		# out 1000, in 900 → merged inventory debit=900 credit=1000; SA residual +100
		# After drop of net-zero only — inventory net kept (debit!=credit) + SA.
		gl = _same_account_repack(out_svd=-1000, in_svd=900, residual=100)
		self.assertNotEqual(gl, [])
		self.assertNotEqual(len(gl), 1)
		assert_gl_map_safe_for_make_gl_entries(gl)
		check = assert_sa_is_residual_only(gl, COMPANY)
		self.assertTrue(check["ok"], check)
		self.assertAlmostEqual(check["net"], 100.0, places=2)
		s = summarize_gl_by_account(gl)
		self.assertIn(INV, s)
		self.assertAlmostEqual(s[INV]["debit"] - s[INV]["credit"], -100.0, places=2)
		self.assertNotIn("511009", "".join(s.keys()))

	def test_diff_stock_account_repack_multi_row(self):
		"""Case 4 — different stock accounts → balanced multi-row, no throw."""
		doc = _FakeSE(purpose="Repack")
		doc.items = [
			frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}),
			frappe._dict({"name": "inn", "cost_center": "Main - E", "is_finished_item": 1}),
		]
		doc._inv_map = {
			"WH-OUT": {"account": WIP},
			"WH-IN": {"account": FG},
		}
		doc._sle_map = {
			"out": [_sle("WH-OUT", -162432000, "out")],
			"inn": [_sle("WH-IN", 162432000, "inn")],
		}
		gl = _run_with_merge(doc, 0)
		self.assertGreaterEqual(len(gl), 2)
		assert_gl_map_safe_for_make_gl_entries(gl)
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(s[WIP]["credit"], 162432000.0, places=2)
		self.assertAlmostEqual(s[FG]["debit"], 162432000.0, places=2)

	def test_manufacture_residual_only_unchanged(self):
		"""v5.7.12 Manufacture residual-only shape still holds with finalize."""
		doc = _FakeSE(purpose="Manufacture")
		doc.total_additional_costs = 100
		doc.additional_costs = [frappe._dict({"expense_account": OH, "base_amount": 100, "amount": 100})]
		doc.items = [
			frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}),
			frappe._dict({"name": "fg", "cost_center": "Main - E", "is_finished_item": 1}),
		]
		doc._inv_map = {"WIP-WH": {"account": WIP}, "FG-WH": {"account": FG}}
		doc._sle_map = {
			"out": [_sle("WIP-WH", -1000, "out")],
			"fg": [_sle("FG-WH", 1105, "fg")],
		}
		gl = _run_with_merge(doc, -5)
		check = assert_sa_is_residual_only(gl, COMPANY)
		self.assertTrue(check["ok"], check)
		self.assertAlmostEqual(check["net"], -5.0, places=2)
		assert_gl_map_safe_for_make_gl_entries(gl)

	def test_finalize_after_collapse_drops_repack_pair(self):
		doc = _FakeSE(purpose="Repack")
		merged = [frappe._dict({"account": INV, "debit": 2025156, "credit": 2025156})]
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.stock_adjustment_account",
				return_value=SA,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.round_currency",
				side_effect=lambda v, ccy: round(float(v)),
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.get_company_currency",
				return_value="IRR",
			),
		):
			out = finalize_normalized_manufacture_gl_map(doc, merged)
		self.assertEqual(out, [])

	def test_material_transfer_skip_guard_untouched(self):
		"""Case 5 — same-account Material Transfer skip logic unchanged."""
		base = {
			"account": INV,
			"company": COMPANY,
			"voucher_type": "Stock Entry",
			"voucher_no": "STE-T",
			"cost_center": "Main - E",
			"project": None,
			"party": None,
			"party_type": None,
			"voucher_detail_no": None,
			"against_voucher": None,
			"against_voucher_type": None,
			"finance_book": None,
			"advance_voucher_type": None,
			"advance_voucher_no": None,
		}
		credit = frappe._dict({**base, "credit": 100, "debit": 0})
		debit = frappe._dict({**base, "credit": 0, "debit": 100})
		self.assertTrue(_should_skip_balanced_transfer_gl_pair(_FakeSE(), credit, debit, 0))


class TestRepackFromRepostConsistencyV5715(unittest.TestCase):
	def test_builder_stable_with_from_repost_flag(self):
		for flag in (False, True):
			frappe.flags.through_repost_item_valuation = flag
			try:
				gl = _same_account_repack(out_svd=-5000, in_svd=5000, residual=0)
			finally:
				frappe.flags.through_repost_item_valuation = False
			self.assertEqual(gl, [], msg=f"from_repost={flag}")
			assert_gl_map_safe_for_make_gl_entries(gl)


if __name__ == "__main__":
	unittest.main()
