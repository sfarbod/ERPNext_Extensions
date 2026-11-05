# Copyright (c) 2026, ERPNext Extensions contributors
"""Integer SABE valuation policy + Manufacture SLE→SED mirror (v5.7.0)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	PENDING_SABE_LOCALIZATION_ATTR,
	apply_sabe_localization_sa_gl,
	localize_sabe_row_values,
)


class TestLocalizeSabeRowValues(unittest.TestCase):
	def test_integer_qty_incoming_uses_qty_times_rhu_rate(self):
		# Core raw: 126 × 28297.142… → amount 3565452-ish; integerize to 126×28297.
		out = localize_sabe_row_values(
			qty=126,
			raw_rate=28297.142857,
			raw_svd=3565452.0,
		)
		self.assertEqual(out["rate"], 28297)
		self.assertEqual(out["svd"], 126 * 28297)
		self.assertEqual(out["localization_difference"], (126 * 28297) - 3565452)

	def test_integer_qty_outgoing_signed(self):
		out = localize_sabe_row_values(
			qty=-126,
			raw_rate=28297.142857,
			raw_svd=-3565452.0,
		)
		self.assertEqual(out["rate"], 28297)
		self.assertEqual(out["svd"], -(126 * 28297))
		self.assertEqual(out["localization_difference"], -(126 * 28297) - (-3565452))

	def test_fractional_qty_keeps_rhu_amount(self):
		out = localize_sabe_row_values(
			qty=0.19,
			raw_rate=121055292.1,
			raw_svd=23000505.4,
		)
		self.assertEqual(out["svd"], 23000505.0)
		self.assertTrue(out["rate"] == int(out["rate"]))

	def test_already_integer_noop_diff_zero(self):
		out = localize_sabe_row_values(qty=10, raw_rate=100, raw_svd=1000)
		self.assertEqual(out["rate"], 100)
		self.assertEqual(out["svd"], 1000.0)
		self.assertEqual(out["localization_difference"], 0.0)


class TestSabeLocalizationSaGl(unittest.TestCase):
	def tearDown(self):
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])

	@patch("erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company", return_value=True)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
		return_value="IRR",
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
		side_effect=lambda v, c: round(float(v)),
	)
	def test_posts_sa_balanced_against_stock_partner(self, *_mocks):
		def _cached(doctype, name, field=None, **kwargs):
			if doctype == "Company" and field == "stock_adjustment_account":
				return "621301 - SA - E"
			if doctype == "Company" and field == "round_off_cost_center":
				return "Main - E"
			if doctype == "Account" and field == "account_type":
				return "Stock" if "Stock" in str(name) else "Expense Account"
			return None

		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
			return_value={"account": "Round Off - E", "cost_center": "Main - E"},
		), patch.object(frappe, "get_cached_value", side_effect=_cached):
			doc = frappe._dict(
				name="MAT-STE-TEST",
				doctype="Stock Entry",
				company="E",
				purpose="Material Transfer",
			)
			setattr(
				doc,
				PENDING_SABE_LOCALIZATION_ATTR,
				[
					{
						"localization_difference": -30.0,
						"voucher_no": "MAT-STE-TEST",
						"voucher_detail_no": "d1",
						"item_code": "X",
						"warehouse": "W",
					}
				],
			)
			setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])
			gl_map = [
				frappe._dict(
					account="Stock - E",
					debit=0,
					credit=100,
					cost_center="Main - E",
					project=None,
				)
			]
			out = apply_sabe_localization_sa_gl(doc, gl_map)
			self.assertEqual(len(out), 3)
			sa = [e for e in out if e.get("_irr_sabe_localization")][0]
			partner = [e for e in out if e.get("_irr_sabe_localization_partner")][0]
			# net=-30 → sa_debit = -(-30) = +30 → Debit SA / Credit stock partner
			self.assertEqual(flt(sa.debit), 30)
			self.assertEqual(partner.account, "Stock - E")
			self.assertEqual(flt(partner.credit), 30)
			# Original stock leg untouched (inventory already integerized).
			self.assertEqual(flt(out[0].credit), 100)

	@patch("erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company", return_value=True)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
		return_value="IRR",
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
		side_effect=lambda v, c: round(float(v)),
	)
	def test_manufacture_a2_partner_is_wip_not_round_off(self, *_mocks):
		def _cached(doctype, name, field=None, **kwargs):
			if doctype == "Company" and field == "stock_adjustment_account":
				return "621301 - SA - E"
			if doctype == "Company" and field == "round_off_cost_center":
				return "Main - E"
			if doctype == "Account" and field == "account_type":
				if "WIP" in str(name) or "Stock" in str(name):
					return "Stock"
				return "Expense Account"
			return None

		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
			return_value={"account": "Round Off - E", "cost_center": "Main - E"},
		), patch.object(frappe, "get_cached_value", side_effect=_cached):
			doc = frappe._dict(
				name="MAT-STE-MFG",
				doctype="Stock Entry",
				company="E",
				purpose="Manufacture",
				value_difference=1000.0,
			)
			setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [
				{
					"localization_difference": 1.0,
					"voucher_no": "MAT-STE-MFG",
					"voucher_detail_no": "d1",
					"item_code": "X",
					"warehouse": "W",
					"detail": {"idempotency_key": "IRR_SABE_A2|E|Stock Entry|MAT-STE-MFG|d1|1"},
				}
			])
			gl_map = [
				frappe._dict(account="WIP - E", debit=0, credit=500, cost_center="Main - E", project=None),
				frappe._dict(account="FG - E", debit=500, credit=0, cost_center="Main - E", project=None),
			]
			# Force Account type lookup for WIP/FG
			out = apply_sabe_localization_sa_gl(doc, gl_map)
			partner = [e for e in out if e.get("_irr_sabe_localization_partner")][0]
			sa = [e for e in out if e.get("_irr_sabe_localization")][0]
			self.assertEqual(partner.account, "WIP - E")
			self.assertNotEqual(partner.account, "Round Off - E")
			self.assertEqual(flt(sa.credit), 1.0)
			self.assertEqual(flt(partner.debit), 1.0)
			self.assertEqual(
				sum(flt(e.get("debit")) - flt(e.get("credit")) for e in out),
				0.0,
			)


class TestRefreshSleAuthoritativeSed(unittest.TestCase):
	def _run_refresh(self, doc, sle):
		from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
			refresh_sle_authoritative_sed_from_ledger,
		)

		with patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
			side_effect=lambda v, c: round(float(v)),
		), patch.object(
			frappe.db,
			"get_value",
			side_effect=lambda *a, **k: sle if a[0] == "Stock Ledger Entry" else None,
		), patch.object(frappe.db, "set_value") as set_value:
			n = refresh_sle_authoritative_sed_from_ledger(doc)
			return n, set_value

	def test_manufacture_consume_mirrors_sle_amount(self):
		row = frappe._dict(
			name="sed-132",
			s_warehouse="WIP",
			t_warehouse=None,
			qty=1532,
			transfer_qty=1532,
			basic_rate=28297,
			valuation_rate=28297,
			basic_amount=43351004,  # poisoned rate-first
			amount=43351004,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			set_basic_rate_manually=0,
		)
		doc = frappe._dict(
			name="MAT-STE-2026-43536",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			items=[row],
		)
		sle = frappe._dict(
			name="sle1",
			actual_qty=-1532,
			stock_value_difference=-43350902,  # ABSORB / WH clear
			qty_after_transaction=0,
			stock_value=0,
			outgoing_rate=28297,
			valuation_rate=28297,
		)
		n, set_value = self._run_refresh(doc, sle)
		self.assertEqual(n, 1)
		self.assertEqual(row.basic_amount, 43350902)
		self.assertEqual(row.amount, 43350902)
		self.assertTrue(set_value.called)

	def test_manufacture_fg_mirrors_sabe_aligned_sle(self):
		row = frappe._dict(
			name="sed-fg",
			s_warehouse=None,
			t_warehouse="FG-WH",
			qty=1532,
			transfer_qty=1532,
			basic_rate=3527503,
			valuation_rate=3527503,
			basic_amount=5404134135,
			amount=5404134135,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			set_basic_rate_manually=0,
			is_finished_item=1,
		)
		doc = frappe._dict(
			name="MAT-STE-2026-43536",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			items=[row],
		)
		sle = frappe._dict(
			name="sle-fg",
			actual_qty=1532,
			stock_value_difference=5404134596,
			qty_after_transaction=1532,
			stock_value=5404134596,
			incoming_rate=3527503,
			valuation_rate=3527503,
		)
		n, set_value = self._run_refresh(doc, sle)
		self.assertEqual(n, 1)
		self.assertEqual(row.basic_amount, 5404134596)
		self.assertEqual(row.amount, 5404134596)
		self.assertTrue(set_value.called)

	def test_case_b_full_warehouse_clear_amount(self):
		row = frappe._dict(
			name="sed-303",
			s_warehouse="WIP",
			qty=1,
			transfer_qty=1,
			basic_rate=626355136,
			valuation_rate=626355136,
			basic_amount=626355136,  # batch amount — wrong for WH clear
			amount=626355136,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			set_basic_rate_manually=0,
		)
		doc = frappe._dict(
			name="MAT-STE-2026-43536",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			items=[row],
		)
		sle = frappe._dict(
			name="sle2",
			actual_qty=-1,
			stock_value_difference=-626355731,  # full previous WH
			qty_after_transaction=0,
			stock_value=0,
			outgoing_rate=626355731,
			valuation_rate=626355731,
		)
		self._run_refresh(doc, sle)
		self.assertEqual(row.basic_amount, 626355731)
		self.assertEqual(row.amount, 626355731)


class TestAlignSkipsSleAuthoritative(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company", return_value=True)
	@patch(
		"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
		return_value="IRR",
	)
	def test_flagged_row_not_rate_first_overwritten(self, *_mocks):
		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
			align_stock_entry_item_amounts,
		)

		row = frappe._dict(
			name="sed",
			qty=1532,
			transfer_qty=1532,
			basic_rate=28297,
			valuation_rate=28297,
			basic_amount=43350902,
			amount=43350902,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			flags=frappe._dict(iran_sle_authoritative_amount=43350902),
		)
		doc = frappe._dict(company="E", purpose="Manufacture", items=[row])
		with patch(
			"erpnext_extensions.iran_accounting.manufacture_output_contract.protect_closed_row_amounts",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.manufacture_output_contract.polish_closed_plan_after_align"
		), patch(
			"erpnext_extensions.iran_accounting.scrap_costing.row_is_component_scrap_fg_pool_residual",
			return_value=False,
		):
			align_stock_entry_item_amounts(doc)
		# Must remain SLE magnitude, not 1532×28297=43351004.
		self.assertEqual(row.basic_amount, 43350902)
		self.assertEqual(row.amount, 43350902)


class TestHistoricalDryRunApi(unittest.TestCase):
	def test_dry_run_integerize_row_no_write(self):
		from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
			integerize_sabe_row,
		)

		fake = frappe._dict(
			name="sabe1",
			qty=-10,
			incoming_rate=100.4,
			outgoing_rate=100.4,
			stock_value_difference=-1004.0,
			item_code="13200333",
			batch_no="B",
			warehouse="W",
			voucher_type="Stock Entry",
			voucher_no="STE-1",
		)
		with patch.object(frappe.db, "get_value", return_value=fake), patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.is_irr_company",
			return_value=True,
		), patch.object(frappe.db, "set_value") as set_value:
			diff = integerize_sabe_row("sabe1", company="E", dry_run=True)
			self.assertIsNotNone(diff)
			self.assertTrue(diff["dry_run"])
			self.assertEqual(diff["new_rate"], 100)
			self.assertEqual(float(diff["new_svd"]), -1000.0)
			set_value.assert_not_called()


if __name__ == "__main__":
	unittest.main()
