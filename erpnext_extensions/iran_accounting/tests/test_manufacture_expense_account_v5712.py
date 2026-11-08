# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.12 — Normalized Manufacture GL: residual-only Stock Adjustment."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.manufacture_expense_account import (
	SA_RESIDUAL_REMARK_TOKEN,
	append_single_sa_residual,
	assert_sa_is_residual_only,
	build_normalized_manufacture_gl_entries,
	collapse_sa_entries_to_single_residual,
	is_manufacture_expense_purpose,
	manufacture_expected_sa_residual_net,
	summarize_gl_by_account,
	uses_normalized_manufacture_gl,
)
from erpnext_extensions.iran_accounting.domain.manufacture_gl_reclass_campaign import (
	HRC00012_MANUFACTURE_VOUCHERS,
	PACKAGE_ID,
)

SA = "621301 - تعدیلات موجودی کالا - E"
WIP = "111701 - کالای درجریان ساخت - E"
FG = "111625 - موجودی محصولات - E"
SFG = "111605 - موجودی نیمه ساخته - E"
SCRAP = "111608 - موجودی ضایعات - E"
OH = "511101 - سربار - E"
COGM = "511009 - قیمت تمام شده کالای ساخته شده - E"
COMPANY = "اسپاد فارمد دارو"


class _FakeSE:
	def __init__(self, purpose="Manufacture", company=COMPANY):
		self.doctype = "Stock Entry"
		self.name = "MAT-STE-TEST"
		self.company = company
		self.purpose = purpose
		self.total_additional_costs = 0
		self.total_incoming_value = 0
		self.total_outgoing_value = 0
		self.cost_center = "Main - E"
		self.remarks = None
		self.is_opening = "No"
		self.additional_costs = []
		self.items = []
		self._sle_map = {}
		self._inv_map = {}

	def get(self, key, default=None):
		return getattr(self, key, default)

	def get_gl_dict(self, args, *a, **kw):
		entry = frappe._dict(args)
		entry.setdefault("debit", 0)
		entry.setdefault("credit", 0)
		entry.setdefault("remarks", args.get("remarks") or "")
		return entry

	def get_debit_field_precision(self):
		return 0

	def get_inventory_account_map(self):
		return self._inv_map

	def get_stock_ledger_details(self):
		return self._sle_map

	def get_inventory_account_dict(self, sle, inventory_account_map, warehouse_field=None):
		acct = inventory_account_map.get(sle.warehouse) or {}
		return {"account": acct.get("account"), "account_currency": "IRR"}

	def set_gl_entries_for_landed_cost_voucher(self, gl_list, inventory_account_map):
		return None


def _sle(warehouse, svd, detail="d1"):
	return frappe._dict(
		{
			"warehouse": warehouse,
			"stock_value_difference": svd,
			"project": None,
			"voucher_detail_no": detail,
		}
	)


class TestManufactureNormalizedGLV5712(unittest.TestCase):
	def test_purpose_gate(self):
		self.assertTrue(is_manufacture_expense_purpose("Manufacture"))
		self.assertTrue(is_manufacture_expense_purpose("Repack"))
		self.assertFalse(is_manufacture_expense_purpose("Material Transfer"))

	@patch(
		"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.is_irr_company",
		return_value=True,
	)
	def test_uses_normalized_for_irr_manufacture(self, _irr):
		doc = _FakeSE()
		self.assertTrue(uses_normalized_manufacture_gl(doc))
		doc.purpose = "Material Issue"
		self.assertFalse(uses_normalized_manufacture_gl(doc))

	@patch(
		"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.round_currency",
		side_effect=lambda v, ccy: round(float(v)),
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.get_company_currency",
		return_value="IRR",
	)
	def test_expected_residual_formula(self, *_m):
		doc = _FakeSE()
		doc.total_incoming_value = 1100
		doc.total_outgoing_value = 1000
		doc.total_additional_costs = 100
		self.assertEqual(manufacture_expected_sa_residual_net(doc), 0.0)
		doc.total_incoming_value = 1105
		self.assertEqual(manufacture_expected_sa_residual_net(doc), -5.0)

	def test_append_single_sa_never_both_sides(self):
		doc = _FakeSE()
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
			gl = []
			append_single_sa_residual(doc, gl, residual_net=-1081)
			append_single_sa_residual(doc, gl, residual_net=0)
			self.assertEqual(len(gl), 1)
			self.assertEqual(gl[0].account, SA)
			self.assertEqual(gl[0].credit, 1081)
			self.assertEqual(gl[0].debit, 0)
			self.assertIn(SA_RESIDUAL_REMARK_TOKEN, gl[0].remarks)

	def test_collapse_nets_debit_and_credit_sa(self):
		doc = _FakeSE()
		gl = [
			frappe._dict({"account": WIP, "debit": 0, "credit": 100}),
			frappe._dict({"account": SA, "debit": 50, "credit": 0}),
			frappe._dict({"account": SA, "debit": 0, "credit": 80}),
			frappe._dict({"account": FG, "debit": 130, "credit": 0}),
		]
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
			out = collapse_sa_entries_to_single_residual(doc, gl)
		check = assert_sa_is_residual_only(out, COMPANY)
		# SA net = 50-80 = -30 → one credit 30
		self.assertTrue(check["residual_only"])
		self.assertTrue(check["single_sided_or_empty"])
		self.assertAlmostEqual(check["net"], -30.0, places=2)
		self.assertAlmostEqual(check["credit"], 30.0, places=2)
		self.assertAlmostEqual(check["debit"], 0.0, places=2)
		# No COGM clearing invented
		self.assertNotIn(COGM, summarize_gl_by_account(out))

	@patch(
		"erpnext.accounts.general_ledger.process_gl_map",
		side_effect=lambda gl_map, from_repost=False: gl_map,
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.frappe.db.exists",
		return_value=False,
	)
	def test_normalized_builder_inventory_oh_residual(self, _exists, _pgm):
		"""Scenario A/C shape: consume WIP, receive FG+scrap, OH, residual 0."""
		doc = _FakeSE()
		doc.total_additional_costs = 100
		doc.additional_costs = [frappe._dict({"expense_account": OH, "base_amount": 100, "amount": 100})]
		doc.items = [
			frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}),
			frappe._dict({"name": "fg", "cost_center": "Main - E", "is_finished_item": 1}),
			frappe._dict({"name": "scrap", "cost_center": "Main - E", "is_finished_item": 0}),
		]
		doc._inv_map = {
			"WIP-WH": {"account": WIP},
			"FG-WH": {"account": FG},
			"SCRAP-WH": {"account": SCRAP},
		}
		doc._sle_map = {
			"out": [_sle("WIP-WH", -1000, "out")],
			"fg": [_sle("FG-WH", 1050, "fg")],
			"scrap": [_sle("SCRAP-WH", 50, "scrap")],
		}
		# sle_net = 100, add = 100 → residual 0
		doc.total_incoming_value = 1100
		doc.total_outgoing_value = 1000

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
				return_value=0.0,
			),
		):
			gl = build_normalized_manufacture_gl_entries(doc)

		summary = summarize_gl_by_account(gl)
		self.assertAlmostEqual(summary[WIP]["net"], -1000.0, places=2)
		self.assertAlmostEqual(summary[FG]["net"], 1050.0, places=2)
		self.assertAlmostEqual(summary[SCRAP]["net"], 50.0, places=2)
		self.assertAlmostEqual(summary[OH]["net"], -100.0, places=2)
		self.assertNotIn(SA, summary)  # residual 0 → no SA row
		self.assertNotIn(COGM, summary)
		tot_dr = sum(v["debit"] for v in summary.values())
		tot_cr = sum(v["credit"] for v in summary.values())
		self.assertAlmostEqual(tot_dr, tot_cr, places=2)

	@patch(
		"erpnext.accounts.general_ledger.process_gl_map",
		side_effect=lambda gl_map, from_repost=False: gl_map,
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.frappe.db.exists",
		return_value=False,
	)
	def test_normalized_builder_single_sa_credit_residual(self, _exists, _pgm):
		doc = _FakeSE()
		doc.total_additional_costs = 100
		doc.additional_costs = [frappe._dict({"expense_account": OH, "base_amount": 100, "amount": 100})]
		doc.items = [
			frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}),
			frappe._dict({"name": "fg", "cost_center": "Main - E", "is_finished_item": 1}),
		]
		doc._inv_map = {"WIP-WH": {"account": WIP}, "FG-WH": {"account": FG}}
		# sle_net = 105 → residual = -(105-100) = -5
		doc._sle_map = {
			"out": [_sle("WIP-WH", -1000, "out")],
			"fg": [_sle("FG-WH", 1105, "fg")],
		}
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
				return_value=-5.0,
			),
		):
			gl = build_normalized_manufacture_gl_entries(doc)

		check = assert_sa_is_residual_only(gl, COMPANY)
		self.assertTrue(check["single_sided_or_empty"])
		self.assertAlmostEqual(check["net"], -5.0, places=2)
		self.assertAlmostEqual(check["credit"], 5.0, places=2)
		self.assertEqual(check["debit"], 0.0)
		self.assertNotIn(COGM, summarize_gl_by_account(gl))

	def test_sfg_and_reject_use_inventory_not_clearing(self):
		"""Scenario F/B: SFG + product reject inventory accounts, no COGM."""
		gl = [
			{"account": WIP, "debit": 0, "credit": 1000},
			{"account": SFG, "debit": 900, "credit": 0},
			{"account": SFG, "debit": 100, "credit": 0},  # reject same inventory acct
			{"account": OH, "debit": 0, "credit": 0},
		]
		s = summarize_gl_by_account(gl)
		self.assertIn(SFG, s)
		self.assertNotIn(COGM, s)
		self.assertNotIn(SA, s)

	def test_hrc00012_allowlist_exact_six(self):
		self.assertEqual(PACKAGE_ID, "HRC-00012")
		self.assertEqual(len(HRC00012_MANUFACTURE_VOUCHERS), 6)

	def test_no_duplicate_additional_cost_debit_to_clearing(self):
		"""OH credit exists; SA/COGM must not receive a matching full OH debit."""
		doc = _FakeSE()
		gl = []
		doc.additional_costs = [frappe._dict({"expense_account": OH, "base_amount": 351288})]
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
			from erpnext_extensions.iran_accounting.domain.manufacture_expense_account import (
				_append_additional_cost_credits,
			)

			_append_additional_cost_credits(doc, gl, against=SA, precision=0)
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(s[OH]["credit"], 351288.0, places=2)
		self.assertNotIn(SA, s)
		self.assertNotIn(COGM, s)


def _run_normalized(doc, residual):
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
			side_effect=lambda gl_map, from_repost=False: gl_map,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.frappe.db.exists",
			return_value=False,
		),
	):
		return build_normalized_manufacture_gl_entries(doc)


def _build_multi(outputs, consume_svd, add=0, residual=0):
	"""outputs: list of (detail, warehouse, account, svd, is_fg)."""
	doc = _FakeSE()
	doc.total_additional_costs = add
	if add:
		doc.additional_costs = [
			frappe._dict({"expense_account": OH, "base_amount": add, "amount": add})
		]
	doc.items = []
	doc._inv_map = {"WIP-WH": {"account": WIP}}
	doc._sle_map = {"out": [_sle("WIP-WH", consume_svd, "out")]}
	doc.items.append(frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}))
	for detail, wh, acct, svd, is_fg in outputs:
		doc._inv_map[wh] = {"account": acct}
		doc._sle_map[detail] = [_sle(wh, svd, detail)]
		doc.items.append(
			frappe._dict({"name": detail, "cost_center": "Main - E", "is_finished_item": is_fg})
		)
	return _run_normalized(doc, residual)


class TestManufactureNormalizedMultiOutputV5712(unittest.TestCase):
	"""Regression matrix: residual-only SA across multi-output topologies."""

	def _assert_residual_shape(self, gl, *, residual_net):
		check = assert_sa_is_residual_only(gl, COMPANY)
		self.assertTrue(check["ok"], check)
		self.assertTrue(check["at_most_one_row"], check)
		self.assertAlmostEqual(check["net"], float(residual_net), places=2)
		if abs(residual_net) < 0.5:
			self.assertEqual(check["row_count"], 0)
		else:
			self.assertEqual(check["row_count"], 1)
		self.assertNotIn(COGM, summarize_gl_by_account(gl))
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(
			sum(v["debit"] for v in s.values()), sum(v["credit"] for v in s.values()), places=2
		)

	def test_zero_residual_no_sa_row(self):
		gl = _build_multi(
			[("fg", "FG-WH", FG, 1100, 1)], consume_svd=-1000, add=100, residual=0
		)
		self._assert_residual_shape(gl, residual_net=0)

	def test_debit_residual_one_debit_row(self):
		gl = _build_multi(
			[("fg", "FG-WH", FG, 1050, 1)], consume_svd=-1000, add=100, residual=50
		)
		self._assert_residual_shape(gl, residual_net=50)
		check = assert_sa_is_residual_only(gl, COMPANY)
		self.assertAlmostEqual(check["debit"], 50.0, places=2)
		self.assertEqual(check["credit"], 0.0)

	def test_credit_residual_one_credit_row(self):
		gl = _build_multi(
			[("fg", "FG-WH", FG, 1105, 1)], consume_svd=-1000, add=100, residual=-5
		)
		self._assert_residual_shape(gl, residual_net=-5)
		check = assert_sa_is_residual_only(gl, COMPANY)
		self.assertEqual(check["debit"], 0.0)
		self.assertAlmostEqual(check["credit"], 5.0, places=2)

	def test_fg_plus_product_reject_sfg(self):
		gl = _build_multi(
			[
				("fg", "SFG-Q", SFG, 900, 1),
				("rej", "SFG-R", SFG, 100, 0),
			],
			consume_svd=-900,
			add=100,
			residual=0,
		)
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(s[SFG]["debit"], 1000.0, places=2)
		self._assert_residual_shape(gl, residual_net=0)

	def test_fg_plus_component_scrap(self):
		gl = _build_multi(
			[
				("fg", "FG-WH", FG, 1050, 1),
				("scrap", "SCRAP-WH", SCRAP, 50, 0),
			],
			consume_svd=-1000,
			add=100,
			residual=0,
		)
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(s[SCRAP]["net"], 50.0, places=2)
		self._assert_residual_shape(gl, residual_net=0)

	def test_fg_plus_by_product_stage_co_shape(self):
		BY = "111625 - موجودی محصولات - E"
		gl = _build_multi(
			[
				("fg", "FG-WH", FG, 980, 1),
				("by", "BY-WH", BY, 20, 0),
			],
			consume_svd=-900,
			add=100,
			residual=0,
		)
		self._assert_residual_shape(gl, residual_net=0)

	def test_fg_plus_co_product_shape(self):
		gl = _build_multi(
			[
				("fg", "FG-WH", FG, 970, 1),
				("co", "CO-WH", FG, 30, 0),
			],
			consume_svd=-900,
			add=100,
			residual=0,
		)
		self._assert_residual_shape(gl, residual_net=0)

	def test_mixed_multi_output(self):
		gl = _build_multi(
			[
				("fg", "FG-WH", FG, 900, 1),
				("by", "BY-WH", FG, 50, 0),
				("scrap", "SCRAP-WH", SCRAP, 50, 0),
			],
			consume_svd=-900,
			add=100,
			residual=0,
		)
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(s[WIP]["net"], -900.0, places=2)
		self._assert_residual_shape(gl, residual_net=0)

	def test_negative_additional_costs(self):
		"""Negative OH posts as debit on OH; residual identity still single-sided SA."""
		doc = _FakeSE()
		doc.total_additional_costs = -50
		doc.additional_costs = [
			frappe._dict({"expense_account": OH, "base_amount": -50, "amount": -50})
		]
		doc.items = [
			frappe._dict({"name": "out", "cost_center": "Main - E", "is_finished_item": 0}),
			frappe._dict({"name": "fg", "cost_center": "Main - E", "is_finished_item": 1}),
		]
		doc._inv_map = {"WIP-WH": {"account": WIP}, "FG-WH": {"account": FG}}
		doc._sle_map = {
			"out": [_sle("WIP-WH", -1000, "out")],
			"fg": [_sle("FG-WH", 950, "fg")],
		}
		# sle_net=-50, add=-50 → residual 0
		gl = _run_normalized(doc, 0)
		s = summarize_gl_by_account(gl)
		self.assertAlmostEqual(s[OH]["debit"], 50.0, places=2)
		self._assert_residual_shape(gl, residual_net=0)

	def test_collapse_idempotent_no_duplicate_sa(self):
		doc = _FakeSE()
		gl = [
			frappe._dict({"account": SA, "debit": 0, "credit": 1081, "against": WIP}),
			frappe._dict({"account": WIP, "debit": 0, "credit": 100}),
		]
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
			once = collapse_sa_entries_to_single_residual(doc, gl)
			twice = collapse_sa_entries_to_single_residual(doc, once)
		self.assertEqual(assert_sa_is_residual_only(once, COMPANY)["row_count"], 1)
		self.assertEqual(assert_sa_is_residual_only(twice, COMPANY)["row_count"], 1)
		self.assertAlmostEqual(assert_sa_is_residual_only(twice, COMPANY)["net"], -1081.0, places=2)

	def test_six_voucher_expected_residuals_table(self):
		"""Validation targets from Finance — formula sign (debit−credit)."""
		expected = {
			"MAT-STE-2026-40332": 0,
			"MAT-STE-2026-40149": -1081,  # 1,081 credit
			"MAT-STE-2026-36826": 0,
			"MAT-STE-2026-36928": -128,  # 128 credit
			"MAT-STE-2026-40902": 160,  # 160 debit
			"MAT-STE-2026-43589": 0,
		}
		self.assertEqual(set(expected), set(HRC00012_MANUFACTURE_VOUCHERS))
		self.assertEqual(sum(expected.values()), -1049)


if __name__ == "__main__":
	unittest.main()
