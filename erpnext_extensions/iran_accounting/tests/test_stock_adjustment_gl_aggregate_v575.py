# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.39+ — Same-side Stock Adjustment aggregation + one-sided GL rows."""

from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate import (
	aggregate_stock_adjustment_gl_entries,
	align_sabe_a2_sa_dimensions_with_in_map,
	ensure_one_sided_gl_entries,
	is_one_sided_gl_entry,
	sa_aggregation_key,
	split_mixed_side_gl_entries,
)


def _sa_leg(
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
	finance_book="",
	party_type="",
	party="",
	posting_date="2026-09-02",
	transaction_currency="IRR",
):
	row = frappe._dict(
		{
			"account": "621301 - تعدیلات موجودی کالا",
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
			"finance_book": finance_book,
			"party_type": party_type,
			"party": party,
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


def _wip(debit=0.0, credit=0.0, cost_center="1130", remarks="WIP", a2_partner=False):
	row = frappe._dict(
		{
			"account": "111701 - کالای درجریان ساخت",
			"debit": debit,
			"credit": credit,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"debit_in_transaction_currency": debit,
			"credit_in_transaction_currency": credit,
			"account_currency": "IRR",
			"transaction_currency": "IRR",
			"cost_center": cost_center,
			"project": "",
			"finance_book": "",
			"party_type": "",
			"party": "",
			"department": "DEP",
			"company": "E",
			"voucher_type": "Stock Entry",
			"voucher_no": "MAT-STE-2026-43536",
			"posting_date": "2026-09-02",
			"remarks": remarks,
		}
	)
	if a2_partner:
		row["_irr_sabe_localization_partner"] = 1
		row["_skip_merge"] = 1
		row["remarks"] = (
			"IRR SABE integer localization residual net=1970.0 IRR_SABE_A2|partner"
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
			("Company", "E", "stock_adjustment_account"): "621301 - تعدیلات موجودی کالا",
		}.get(a if len(a) == 3 else a[:3]),
	), patch(
		"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions",
		return_value=["cost_center", "department"],
	):
		yield


class TestStockAdjustmentGlAggregateV575(unittest.TestCase):
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
			return aggregate_stock_adjustment_gl_entries(doc or self.doc, gl_map)

	def _assert_all_one_sided(self, gl_map):
		for e in gl_map:
			self.assertTrue(
				is_one_sided_gl_entry(e),
				msg=f"mixed-side row: acct={e.account} dr={e.debit} cr={e.credit}",
			)

	def test_01_debit_100_and_credit_60_remain_separate(self):
		gl_map = [
			_sa_leg(debit=100.0),
			_sa_leg(credit=60.0, a2=True),
			_wip(credit=40.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 2)
		self.assertEqual(sorted((flt(e.debit), flt(e.credit)) for e in sa), [(0.0, 60.0), (100.0, 0.0)])
		self._assert_all_one_sided(out)

	def test_02_debit_100_and_debit_50_aggregate_to_150(self):
		gl_map = [
			_sa_leg(debit=100.0),
			_sa_leg(debit=50.0, a2=True),
			_wip(credit=150.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 150.0)
		self.assertEqual(flt(sa[0].credit), 0.0)
		self._assert_all_one_sided(out)

	def test_03_credit_100_and_credit_50_aggregate_to_150(self):
		gl_map = [
			_sa_leg(credit=100.0),
			_sa_leg(credit=50.0, a2=True),
			_wip(debit=150.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 0.0)
		self.assertEqual(flt(sa[0].credit), 150.0)
		self._assert_all_one_sided(out)

	def test_04_different_cost_centers_stay_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, cost_center="1130"),
			_sa_leg(credit=1970.0, cost_center="3000", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account.startswith("621301")]), 2)

	def test_05_different_projects_and_dimensions_stay_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, project="P1"),
			_sa_leg(credit=1970.0, project="P2", a2=True),
			_sa_leg(credit=10.0, department="D1", voucher_no="MAT-STE-DIM-A"),
			_sa_leg(credit=20.0, department="D2", voucher_no="MAT-STE-DIM-A", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account.startswith("621301")]), 4)

	def test_06_core_461_plus_a2_1970_aggregates_to_credit_2431(self):
		gl_map = [
			_sa_leg(credit=461.0, remarks="Accounting Entry for Stock"),
			_sa_leg(credit=1970.0, a2=True),
			_wip(debit=1970.0, a2_partner=True),
			frappe._dict(
				{
					"account": "FG",
					"debit": 461.0,
					"credit": 0.0,
					"debit_in_account_currency": 461.0,
					"credit_in_account_currency": 0.0,
					"debit_in_transaction_currency": 461.0,
					"credit_in_transaction_currency": 0.0,
					"account_currency": "IRR",
					"transaction_currency": "IRR",
					"cost_center": "1130",
					"project": "",
					"finance_book": "",
					"party_type": "",
					"party": "",
					"department": "DEP",
					"company": "E",
					"voucher_type": "Stock Entry",
					"voucher_no": "MAT-STE-2026-43536",
					"posting_date": "2026-09-02",
				}
			),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 0.0)
		self.assertEqual(flt(sa[0].credit), 2431.0)
		comps = sa[0].get("_irr_sa_source_components") or []
		by_src = {c["source"]: c["signed_credit_minus_debit"] for c in comps}
		self.assertEqual(by_src["core_stock_adjustment"], 461.0)
		self.assertEqual(by_src["iran_a2"], 1970.0)
		self.assertIn("IRR_SA_AGG|", sa[0].remarks)
		self.assertEqual(
			sum(flt(e.debit) for e in out),
			sum(flt(e.credit) for e in out),
		)
		self._assert_all_one_sided(out)

	def test_07_a2_wip_debit_1970_remains_separate_debit_only(self):
		gl_map = [
			_sa_leg(credit=461.0),
			_sa_leg(credit=1970.0, a2=True),
			_wip(debit=1970.0, a2_partner=True),
			_wip(credit=5451221439.0, remarks="Accounting Entry for Stock"),
		]
		out = self._agg(gl_map)
		wip = [e for e in out if e.account.startswith("111701")]
		self.assertEqual(len(wip), 2)
		debit_rows = [e for e in wip if flt(e.debit) > 0]
		credit_rows = [e for e in wip if flt(e.credit) > 0]
		self.assertEqual(len(debit_rows), 1)
		self.assertEqual(flt(debit_rows[0].debit), 1970.0)
		self.assertEqual(flt(debit_rows[0].credit), 0.0)
		self.assertEqual(len(credit_rows), 1)
		self.assertEqual(flt(credit_rows[0].credit), 5451221439.0)
		self.assertEqual(flt(credit_rows[0].debit), 0.0)
		self._assert_all_one_sided(out)

	def test_08_no_company_currency_mixed_side_after_split(self):
		mixed = _wip(debit=1970.0, credit=5451221439.0)
		out = split_mixed_side_gl_entries([mixed])
		self.assertEqual(len(out), 2)
		self._assert_all_one_sided(out)
		self.assertEqual(flt(out[0].debit) + flt(out[1].debit), 1970.0)
		self.assertEqual(flt(out[0].credit) + flt(out[1].credit), 5451221439.0)

	def test_09_no_account_currency_mixed_side(self):
		mixed = frappe._dict(
			{
				"account": "111701",
				"debit": 10.0,
				"credit": 20.0,
				"debit_in_account_currency": 10.0,
				"credit_in_account_currency": 20.0,
				"debit_in_transaction_currency": 10.0,
				"credit_in_transaction_currency": 20.0,
				"account_currency": "IRR",
			}
		)
		out = ensure_one_sided_gl_entries([mixed])
		for e in out:
			self.assertFalse(flt(e.debit) > 0 and flt(e.credit) > 0)
			self.assertFalse(
				flt(e.debit_in_account_currency) > 0 and flt(e.credit_in_account_currency) > 0
			)

	def test_10_legitimate_pm1_adjustments_intact(self):
		gl_map = [
			_sa_leg(credit=1.0, remarks="IRR stock-valuation currency-precision residual"),
			_wip(debit=1.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].credit), 1.0)
		self.assertFalse(sa[0].get("_irr_sa_aggregated"))

	def test_11_zero_value_filtering_semantics(self):
		# Aggregation of equal same-side zeros should not invent rows; empty
		# company-currency zeros stay droppable by merge filter separately.
		gl_map = [
			_sa_leg(credit=0.0, debit=0.0),
			_wip(debit=0.0, credit=0.0),
		]
		with _agg_patches():
			out = ensure_one_sided_gl_entries(gl_map)
		self._assert_all_one_sided(out)

	def test_12_live_and_riv_remake_identical(self):
		def build():
			return [
				_sa_leg(credit=461.0),
				_sa_leg(credit=1970.0, a2=True),
				_wip(debit=1970.0, a2_partner=True),
				_wip(credit=461.0),
			]

		live = self._agg(build(), frappe._dict(name="X", doctype="Stock Entry", company="E", flags=frappe._dict()))
		riv = self._agg(build(), frappe._dict(name="X", doctype="Stock Entry", company="E", flags=frappe._dict()))
		live_sa = next(e for e in live if e.account.startswith("621301"))
		riv_sa = next(e for e in riv if e.account.startswith("621301"))
		self.assertEqual(flt(live_sa.credit), flt(riv_sa.credit))
		self.assertEqual(
			[(c["source"], c["signed_credit_minus_debit"]) for c in live_sa._irr_sa_source_components],
			[(c["source"], c["signed_credit_minus_debit"]) for c in riv_sa._irr_sa_source_components],
		)
		self._assert_all_one_sided(live)
		self._assert_all_one_sided(riv)

	def test_13_exact_voucher_balance_preserved(self):
		gl_map = [
			_wip(credit=5000.0),
			_sa_leg(credit=461.0),
			_sa_leg(credit=1970.0, a2=True),
			_wip(debit=1970.0, a2_partner=True),
			frappe._dict(
				{
					"account": "FG",
					"debit": 5461.0,
					"credit": 0.0,
					"debit_in_account_currency": 5461.0,
					"credit_in_account_currency": 0.0,
					"debit_in_transaction_currency": 5461.0,
					"credit_in_transaction_currency": 0.0,
					"account_currency": "IRR",
					"transaction_currency": "IRR",
					"cost_center": "1130",
					"project": "",
					"finance_book": "",
					"party_type": "",
					"party": "",
					"department": "DEP",
					"company": "E",
					"voucher_type": "Stock Entry",
					"voucher_no": "MAT-STE-2026-43536",
					"posting_date": "2026-09-02",
				}
			),
		]
		before_dr = sum(flt(e.debit) for e in gl_map)
		before_cr = sum(flt(e.credit) for e in gl_map)
		out = self._agg(gl_map)
		self.assertEqual(before_dr, before_cr)
		self.assertEqual(
			sum(flt(e.debit) for e in out),
			sum(flt(e.credit) for e in out),
		)
		self.assertEqual(sum(flt(e.debit) for e in out), before_dr)
		self._assert_all_one_sided(out)

	def test_source_payload_and_align_dims(self):
		gl_map = [
			_sa_leg(credit=461.0),
			_sa_leg(credit=1970.0, a2=True),
		]
		out = self._agg(gl_map)
		sa = next(e for e in out if e.account.startswith("621301"))
		payload = json.loads(sa.remarks.split("IRR_SA_AGG|", 1)[1])
		self.assertEqual(payload["side"], "credit")
		self.assertEqual(payload["agg_credit"], 2431.0)

		with _agg_patches():
			dims = align_sabe_a2_sa_dimensions_with_in_map(
				self.doc,
				[_sa_leg(credit=461.0, cost_center="1130", department="PACK")],
				"621301 - تعدیلات موجودی کالا",
			)
		self.assertEqual(dims.get("cost_center"), "1130")

	def test_aggregation_key_includes_side(self):
		a = _sa_leg(debit=10.0)
		b = _sa_leg(credit=10.0)
		self.assertNotEqual(
			sa_aggregation_key(a, ["department"], side="debit"),
			sa_aggregation_key(b, ["department"], side="credit"),
		)


if __name__ == "__main__":
	unittest.main()
