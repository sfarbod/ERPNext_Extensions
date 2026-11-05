# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.39 — Aggregate Stock Adjustment GL legs that share a compatible key."""

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
	sa_aggregation_key,
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
			"cost_center": cost_center,
			"project": project,
			"finance_book": finance_book,
			"party_type": party_type,
			"party": party,
			"department": department,
			"company": "E",
			"voucher_type": "Stock Entry",
			"voucher_no": voucher_no,
			"posting_date": "2026-09-02",
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


def _wip(debit=0.0, credit=0.0, cost_center="1130"):
	return frappe._dict(
		{
			"account": "WIP",
			"debit": debit,
			"credit": credit,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"debit_in_transaction_currency": debit,
			"credit_in_transaction_currency": credit,
			"account_currency": "IRR",
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
			"remarks": "WIP",
		}
	)


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

	def test_01_core_461_plus_a2_1970_aggregates_to_credit_2431(self):
		gl_map = [
			_sa_leg(credit=461.0, remarks="Accounting Entry for Stock"),
			_sa_leg(credit=1970.0, a2=True),
			_wip(debit=1970.0),  # A2 WIP counter
			frappe._dict(
				{
					"account": "FG",
					"debit": 461.0,  # Core SA counter (simplified)
					"credit": 0.0,
					"debit_in_account_currency": 461.0,
					"credit_in_account_currency": 0.0,
					"debit_in_transaction_currency": 461.0,
					"credit_in_transaction_currency": 0.0,
					"account_currency": "IRR",
					"cost_center": "1130",
					"project": "",
					"finance_book": "",
					"party_type": "",
					"party": "",
					"department": "DEP",
					"company": "E",
					"voucher_type": "Stock Entry",
					"voucher_no": "MAT-STE-2026-43536",
				}
			),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 0.0)
		self.assertEqual(flt(sa[0].credit), 2431.0)
		self.assertEqual(flt(sa[0].credit_in_account_currency), 2431.0)
		self.assertEqual(flt(sa[0].debit_in_account_currency), 0.0)
		self.assertTrue(sa[0].get("_irr_sa_aggregated"))
		comps = sa[0].get("_irr_sa_source_components") or []
		by_src = {c["source"]: c["signed_credit_minus_debit"] for c in comps}
		self.assertEqual(by_src["core_stock_adjustment"], 461.0)
		self.assertEqual(by_src["iran_a2"], 1970.0)
		self.assertIn("IRR_SA_AGG|", sa[0].remarks)
		wip_debit = sum(flt(e.debit) for e in out if e.account == "WIP")
		self.assertEqual(wip_debit, 1970.0)
		self.assertEqual(
			sum(flt(e.debit) for e in out),
			sum(flt(e.credit) for e in out),
		)

	def test_02_debit_100_credit_60_nets_to_debit_40(self):
		gl_map = [
			_sa_leg(debit=100.0, credit=0.0),
			_sa_leg(debit=0.0, credit=60.0, a2=True),
			_wip(credit=40.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 40.0)
		self.assertEqual(flt(sa[0].credit), 0.0)

	def test_03_debit_60_credit_100_nets_to_credit_40(self):
		gl_map = [
			_sa_leg(debit=60.0),
			_sa_leg(credit=100.0, a2=True),
			_wip(debit=40.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].debit), 0.0)
		self.assertEqual(flt(sa[0].credit), 40.0)

	def test_04_equal_debit_credit_drops_zero_row(self):
		gl_map = [
			_sa_leg(debit=100.0),
			_sa_leg(credit=100.0, a2=True),
			_wip(credit=50.0),
			_wip(debit=50.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 0)
		self.assertEqual(
			sum(flt(e.debit) for e in out),
			sum(flt(e.credit) for e in out),
		)

	def test_05_different_cost_centers_stay_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, cost_center="1130"),
			_sa_leg(credit=1970.0, cost_center="3000", a2=True),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 2)

	def test_06_different_projects_stay_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, project="P1"),
			_sa_leg(credit=1970.0, project="P2", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account.startswith("621301")]), 2)

	def test_06b_different_department_dimension_stays_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, department="D1"),
			_sa_leg(credit=1970.0, department="D2", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account.startswith("621301")]), 2)

	def test_07_different_currencies_stay_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, account_currency="IRR"),
			_sa_leg(credit=1970.0, account_currency="USD", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account.startswith("621301")]), 2)

	def test_08_different_vouchers_stay_separate(self):
		gl_map = [
			_sa_leg(credit=461.0, voucher_no="MAT-STE-A"),
			_sa_leg(credit=1970.0, voucher_no="MAT-STE-B", a2=True),
		]
		out = self._agg(gl_map)
		self.assertEqual(len([e for e in out if e.account.startswith("621301")]), 2)

	def test_09_account_and_transaction_currency_mirrors(self):
		gl_map = [
			_sa_leg(credit=461.0),
			_sa_leg(credit=1970.0, a2=True),
		]
		out = self._agg(gl_map)
		sa = next(e for e in out if e.account.startswith("621301"))
		self.assertEqual(flt(sa.credit_in_account_currency), 2431.0)
		self.assertEqual(flt(sa.credit_in_transaction_currency), 2431.0)
		self.assertEqual(flt(sa.debit_in_account_currency), 0.0)
		self.assertEqual(flt(sa.debit_in_transaction_currency), 0.0)
		self.assertEqual(sa.account_currency, "IRR")

	def test_10_source_level_reconciliation_payload(self):
		gl_map = [
			_sa_leg(credit=461.0),
			_sa_leg(credit=1970.0, a2=True),
		]
		out = self._agg(gl_map)
		sa = next(e for e in out if e.account.startswith("621301"))
		payload_raw = sa.remarks.split("IRR_SA_AGG|", 1)[1]
		payload = json.loads(payload_raw)
		self.assertTrue(payload["iran_sa_aggregate"])
		self.assertEqual(payload["net_credit"], 2431.0)
		sources = {c["source"]: c["signed_credit_minus_debit"] for c in payload["components"]}
		self.assertEqual(sources["core_stock_adjustment"], 461.0)
		self.assertEqual(sources["iran_a2"], 1970.0)
		audit = self.doc.flags.iran_sa_aggregate
		self.assertEqual(audit["active_aggregated_rows"], 1)
		self.assertEqual(audit["groups"][0]["net_credit"], 2431.0)

	def test_11_exact_gl_balance_after_aggregation(self):
		# Credits: WIP 5000 + SA 2431 = 7431; Debits: WIP 1970 + FG 5461 = 7431
		gl_map = [
			_wip(credit=5000.0),
			_sa_leg(credit=461.0),
			_sa_leg(credit=1970.0, a2=True),
			_wip(debit=1970.0),
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
					"cost_center": "1130",
					"project": "",
					"finance_book": "",
					"party_type": "",
					"party": "",
					"department": "DEP",
					"company": "E",
					"voucher_type": "Stock Entry",
					"voucher_no": "MAT-STE-2026-43536",
				}
			),
		]
		out = self._agg(gl_map)
		self.assertEqual(
			sum(flt(e.debit) for e in out),
			sum(flt(e.credit) for e in out),
		)

	def test_12_live_and_riv_remake_identical_aggregation(self):
		def build():
			return [
				_sa_leg(credit=461.0),
				_sa_leg(credit=1970.0, a2=True),
				_wip(debit=1970.0),
				_wip(credit=461.0),
			]

		live_doc = frappe._dict(name="X", doctype="Stock Entry", company="E", flags=frappe._dict())
		riv_doc = frappe._dict(name="X", doctype="Stock Entry", company="E", flags=frappe._dict())
		live = self._agg(build(), live_doc)
		riv = self._agg(build(), riv_doc)
		live_sa = next(e for e in live if e.account.startswith("621301"))
		riv_sa = next(e for e in riv if e.account.startswith("621301"))
		self.assertEqual(flt(live_sa.credit), flt(riv_sa.credit))
		self.assertEqual(flt(live_sa.debit), flt(riv_sa.debit))
		self.assertEqual(
			[(c["source"], c["signed_credit_minus_debit"]) for c in live_sa._irr_sa_source_components],
			[(c["source"], c["signed_credit_minus_debit"]) for c in riv_sa._irr_sa_source_components],
		)

	def test_13_non_sa_counter_entries_not_aggregated_away(self):
		# A2 +520 / −58 style: only SA nets; WIP partner stays discrete.
		gl_map = [
			_sa_leg(credit=520.0),
			_sa_leg(debit=58.0, a2=True),
			_wip(debit=520.0),
			_wip(credit=58.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].credit), 462.0)
		wip = [e for e in out if e.account == "WIP"]
		self.assertEqual(len(wip), 2)
		self.assertEqual(sum(flt(e.debit) for e in wip), 520.0)
		self.assertEqual(sum(flt(e.credit) for e in wip), 58.0)

	def test_14_legitimate_pm1_sa_behavior_intact(self):
		# Single ±1 SA row must pass through unchanged (no spurious drop/merge).
		gl_map = [
			_sa_leg(credit=1.0, remarks="IRR stock-valuation currency-precision residual"),
			_wip(debit=1.0),
		]
		out = self._agg(gl_map)
		sa = [e for e in out if e.account.startswith("621301")]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].credit), 1.0)
		self.assertFalse(sa[0].get("_irr_sa_aggregated"))

	def test_align_a2_sa_dimensions_prefers_in_map_core_sa(self):
		gl_map = [
			_sa_leg(credit=461.0, cost_center="1130", department="PACK"),
			_wip(credit=100.0, cost_center="3000"),
		]
		with _agg_patches():
			dims = align_sabe_a2_sa_dimensions_with_in_map(
				self.doc, gl_map, "621301 - تعدیلات موجودی کالا"
			)
		self.assertEqual(dims.get("cost_center"), "1130")
		self.assertEqual(dims.get("department"), "PACK")

	def test_aggregation_key_excludes_remarks_and_against(self):
		a = _sa_leg(credit=1.0, remarks="A")
		b = _sa_leg(credit=2.0, remarks="B")
		a["against"] = "X"
		b["against"] = "Y"
		self.assertEqual(
			sa_aggregation_key(a, ["department"]),
			sa_aggregation_key(b, ["department"]),
		)


if __name__ == "__main__":
	unittest.main()
