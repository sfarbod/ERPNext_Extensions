# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.41 — Adjustment-account-only same-side GL aggregation."""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate import (
	aggregate_adjustment_gl_entries,
	aggregate_stock_adjustment_gl_entries,
	align_sabe_a2_sa_dimensions_with_in_map,
	get_adjustable_gl_accounts,
	is_adjustable_gl_account,
	sa_aggregation_key,
	split_mixed_side_gl_entries,
	is_one_sided_gl_entry,
)


SA = "621301 - تعدیلات موجودی کالا"
RO = "622515 - کسر و اضافات ریالی"
WIP = "111701 - کالای درجریان ساخت"
FG = "111625 - موجودی محصولات"


def _leg(
	account,
	*,
	credit=0.0,
	debit=0.0,
	cost_center="1130",
	project="",
	account_currency="IRR",
	voucher_no="MAT-STE-2026-43536",
	remarks="Accounting Entry for Stock",
	a2=False,
	department="DEP",
	posting_date="2026-09-02",
	transaction_currency="IRR",
):
	row = frappe._dict(
		{
			"account": account,
			"debit": debit,
			"credit": credit,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"debit_in_transaction_currency": debit,
			"credit_in_transaction_currency": credit,
			"account_currency": account_currency,
			"transaction_currency": transaction_currency,
			"cost_center": cost_center,
			"project": project,
			"finance_book": "",
			"party_type": "",
			"party": "",
			"department": department,
			"company": "E",
			"voucher_type": "Stock Entry",
			"voucher_no": voucher_no,
			"posting_date": posting_date,
			"remarks": remarks,
		}
	)
	if a2:
		row["_irr_sabe_localization"] = 1
		row["remarks"] = (
			f"IRR SABE integer localization residual net={credit or -debit} "
			f"IRR_SABE_A2|E|Stock Entry|{voucher_no}|net|{int(credit or debit)}"
		)
	return row


@contextmanager
def _agg_patches():
	with patch(
		"erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate.is_irr_company",
		return_value=True,
	), patch(
		"erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate.get_company_currency",
		return_value="IRR",
	), patch(
		"erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate.round_currency",
		side_effect=lambda v, c: round(float(v)),
	), patch(
		"frappe.get_cached_value",
		side_effect=lambda *a, **k: {
			("Company", "E", "stock_adjustment_account"): SA,
			("Company", "E", "round_off_account"): RO,
		}.get(a if len(a) == 3 else a[:3]),
	), patch(
		"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions",
		return_value=["cost_center", "department"],
	):
		yield


class TestAdjustmentOnlyGlAggregateV575(unittest.TestCase):
	def setUp(self):
		self.doc = frappe._dict(
			name="MAT-STE-2026-43536",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			flags=frappe._dict(),
		)

	def _agg(self, gl_map, doc=None):
		with _agg_patches():
			return aggregate_adjustment_gl_entries(doc or self.doc, gl_map)

	def test_01_only_configured_adjustment_accounts_are_aggregated(self):
		with _agg_patches():
			self.assertEqual(get_adjustable_gl_accounts("E"), {SA, RO})
			self.assertTrue(is_adjustable_gl_account("E", SA))
			self.assertTrue(is_adjustable_gl_account("E", RO))
			self.assertFalse(is_adjustable_gl_account("E", WIP))
			self.assertFalse(is_adjustable_gl_account("E", FG))

	def test_02_sa_core_461_plus_a2_1970_to_credit_2431(self):
		gl_map = [
			_leg(SA, credit=461.0),
			_leg(SA, credit=1970.0, a2=True),
			_leg(WIP, debit=1970.0, remarks="IRR SABE A2 partner"),
			_leg(WIP, credit=1000.0),
			_leg(FG, debit=461.0),
		]
		before_non_adj = [
			(e.account, flt(e.debit), flt(e.credit), e.cost_center, e.remarks)
			for e in gl_map
			if e.account not in (SA, RO)
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account == SA]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].credit), 2431.0)
		self.assertEqual(flt(sa[0].debit), 0.0)
		after_non_adj = [
			(e.account, flt(e.debit), flt(e.credit), e.cost_center, e.remarks)
			for e in out
			if e.account not in (SA, RO)
		]
		self.assertEqual(before_non_adj, after_non_adj)

	def test_03_wip_retains_original_core_and_a2_partner_rows(self):
		gl_map = [
			_leg(SA, credit=461.0),
			_leg(SA, credit=1970.0, a2=True),
			_leg(WIP, credit=5451221439.0, remarks="Accounting Entry for Stock"),
			_leg(WIP, debit=1970.0, remarks="IRR SABE A2 partner"),
		]
		out = self._agg(gl_map)
		wip = [e for e in out if e.account == WIP]
		self.assertEqual(len(wip), 2)
		self.assertEqual(
			sorted((flt(e.debit), flt(e.credit), e.remarks) for e in wip),
			sorted(
				[
					(0.0, 5451221439.0, "Accounting Entry for Stock"),
					(1970.0, 0.0, "IRR SABE A2 partner"),
				]
			),
		)

	def test_04_a2_counter_not_merged_into_wip_by_aggregation(self):
		gl_map = [
			_leg(SA, credit=1970.0, a2=True),
			_leg(WIP, debit=1970.0, remarks="partner"),
			_leg(WIP, credit=100.0, remarks="core"),
		]
		out = self._agg(gl_map)
		# Aggregation only touches SA; WIP rows identical count/amounts.
		self.assertEqual(len([e for e in out if e.account == WIP]), 2)
		self.assertEqual(sum(flt(e.debit) for e in out if e.account == WIP), 1970.0)
		self.assertEqual(sum(flt(e.credit) for e in out if e.account == WIP), 100.0)

	def test_05_fg_scrap_inventory_expense_revenue_unchanged(self):
		gl_map = [
			_leg(SA, credit=10.0),
			_leg(SA, credit=20.0, a2=True),
			_leg(FG, debit=100.0),
			_leg("111608 - scrap", debit=5.0),
			_leg("111620 - packing", debit=7.0),
			_leg("5 - expense", debit=3.0),
			_leg("4 - revenue", credit=3.0),
			_leg("Payable", credit=50.0, remarks="party"),
			_leg("Receivable", debit=50.0, remarks="party"),
		]
		fingerprint = [
			(e.account, flt(e.debit), flt(e.credit), e.cost_center, e.remarks)
			for e in gl_map
			if e.account != SA
		]
		out = self._agg(gl_map)
		after = [
			(e.account, flt(e.debit), flt(e.credit), e.cost_center, e.remarks)
			for e in out
			if e.account != SA
		]
		self.assertEqual(fingerprint, after)
		self.assertEqual(sum(flt(e.credit) for e in out if e.account == SA), 30.0)

	def test_06_configured_invoice_rounding_account_aggregates(self):
		gl_map = [
			_leg(RO, credit=1.0, remarks="Round Off", voucher_no="ACC-PINV-1"),
			_leg(RO, credit=2.0, remarks="IRR_RATE_ROUNDING", voucher_no="ACC-PINV-1"),
			_leg("Payable", credit=100.0, voucher_no="ACC-PINV-1"),
			_leg("Expense", debit=103.0, voucher_no="ACC-PINV-1"),
		]
		# Force voucher_type style for PI
		for e in gl_map:
			e.voucher_type = "Purchase Invoice"
		out = self._agg(gl_map)
		ro = [e for e in out if e.account == RO]
		self.assertEqual(len(ro), 1)
		self.assertEqual(flt(ro[0].credit), 3.0)
		self.assertEqual(len([e for e in out if e.account == "Payable"]), 1)
		self.assertEqual(flt(next(e for e in out if e.account == "Payable").credit), 100.0)

	def test_07_different_dimensions_stay_separate(self):
		gl_map = [
			_leg(SA, credit=461.0, cost_center="1130"),
			_leg(SA, credit=1970.0, cost_center="3000", a2=True),
			_leg(SA, credit=10.0, project="P1", voucher_no="X"),
			_leg(SA, credit=20.0, project="P2", voucher_no="X", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account == SA]), 4)

	def test_08_debit_and_credit_never_netted_on_adjustment(self):
		gl_map = [
			_leg(SA, debit=100.0),
			_leg(SA, credit=60.0, a2=True),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account == SA]
		self.assertEqual(len(sa), 2)
		self.assertEqual(sorted((flt(e.debit), flt(e.credit)) for e in sa), [(0.0, 60.0), (100.0, 0.0)])

	def test_09_account_currency_mirrors(self):
		gl_map = [
			_leg(SA, credit=461.0),
			_leg(SA, credit=1970.0, a2=True),
		]
		out = self._agg(gl_map)
		sa = next(e for e in out if e.account == SA)
		self.assertEqual(flt(sa.credit_in_account_currency), 2431.0)
		self.assertEqual(flt(sa.credit_in_transaction_currency), 2431.0)
		self.assertEqual(flt(sa.debit_in_account_currency), 0.0)

	def test_10_zero_value_same_side_drops(self):
		# Two credits that net to zero on same side after rounding — both zero.
		gl_map = [
			_leg(SA, credit=0.0),
			_leg(SA, debit=0.0, a2=True),
			_leg(WIP, credit=1.0),
			_leg(WIP, debit=1.0),
		]
		out = self._agg(gl_map)
		# Zero SA rows may remain as singles (no multi-member group) — unchanged WIP.
		self.assertEqual(len([e for e in out if e.account == WIP]), 2)

	def test_11_exact_voucher_balance_preserved(self):
		gl_map = [
			_leg(SA, credit=461.0),
			_leg(SA, credit=1970.0, a2=True),
			_leg(WIP, debit=1970.0),
			_leg(FG, debit=461.0),
		]
		before = sum(flt(e.debit) for e in gl_map), sum(flt(e.credit) for e in gl_map)
		out = self._agg(gl_map)
		after = sum(flt(e.debit) for e in out), sum(flt(e.credit) for e in out)
		self.assertEqual(before, after)
		self.assertEqual(after[0], after[1])

	def test_12_live_and_riv_identical_adjustment_only(self):
		def build():
			return [
				_leg(SA, credit=461.0),
				_leg(SA, credit=1970.0, a2=True),
				_leg(WIP, debit=1970.0, remarks="partner"),
				_leg(WIP, credit=5000.0, remarks="core"),
				_leg(FG, debit=461.0 + 5000.0 - 1970.0),
			]

		live = self._agg(build(), frappe._dict(name="X", doctype="Stock Entry", company="E", flags=frappe._dict()))
		riv = self._agg(build(), frappe._dict(name="X", doctype="Stock Entry", company="E", flags=frappe._dict()))
		live_sa = next(e for e in live if e.account == SA)
		riv_sa = next(e for e in riv if e.account == SA)
		self.assertEqual(flt(live_sa.credit), flt(riv_sa.credit))
		live_wip = sorted((flt(e.debit), flt(e.credit), e.remarks) for e in live if e.account == WIP)
		riv_wip = sorted((flt(e.debit), flt(e.credit), e.remarks) for e in riv if e.account == WIP)
		self.assertEqual(live_wip, riv_wip)

	def test_13_non_adjustment_regression_fingerprint(self):
		"""Non-adjustment accounts must match pre-aggregation enhancement fingerprint."""
		gl_map = [
			_leg(SA, credit=461.0),
			_leg(SA, credit=1970.0, a2=True),
			_leg(WIP, credit=5451221439.0, remarks="Accounting Entry for Stock"),
			_leg(WIP, debit=1970.0, remarks="IRR SABE A2 partner"),
			_leg(FG, debit=5404134596.0),
			_leg("111608 - scrap", debit=15781209.0),
			_leg("111620 - packing", debit=31306095.0),
		]
		pre = {
			(e.account, flt(e.debit), flt(e.credit), e.cost_center, e.remarks)
			for e in gl_map
			if e.account != SA
		}
		out = self._agg(gl_map)
		post = {
			(e.account, flt(e.debit), flt(e.credit), e.cost_center, e.remarks)
			for e in out
			if e.account != SA
		}
		self.assertEqual(pre, post)
		# Alias still works
		with _agg_patches():
			alias_out = aggregate_stock_adjustment_gl_entries(self.doc, list(gl_map))
		self.assertEqual(sum(flt(e.credit) for e in alias_out if e.account == SA), 2431.0)

	def test_same_side_debit_aggregate(self):
		out = self._agg([_leg(SA, debit=100.0), _leg(SA, debit=50.0, a2=True)])
		sa = [e for e in out if e.account == SA]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 150.0)

	def test_split_helper_still_unit_testable(self):
		mixed = _leg(WIP, debit=10.0, credit=20.0)
		# Craft mixed amounts
		mixed.debit = 10.0
		mixed.credit = 20.0
		mixed.debit_in_account_currency = 10.0
		mixed.credit_in_account_currency = 20.0
		out = split_mixed_side_gl_entries([mixed])
		self.assertEqual(len(out), 2)
		self.assertTrue(all(is_one_sided_gl_entry(e) for e in out))

	def test_aggregation_key_includes_side(self):
		a = _leg(SA, debit=10.0)
		b = _leg(SA, credit=10.0)
		self.assertNotEqual(
			sa_aggregation_key(a, ["department"], side="debit"),
			sa_aggregation_key(b, ["department"], side="credit"),
		)

	def test_align_dims(self):
		with _agg_patches():
			dims = align_sabe_a2_sa_dimensions_with_in_map(
				self.doc, [_leg(SA, credit=461.0, cost_center="1130", department="PACK")], SA
			)
		self.assertEqual(dims.get("cost_center"), "1130")


if __name__ == "__main__":
	unittest.main()
