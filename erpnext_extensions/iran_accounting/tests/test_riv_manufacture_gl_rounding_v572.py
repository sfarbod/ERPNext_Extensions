# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.2 — Manufacture RIV ±1 IRR residual + live/RIV GL handler parity.

Root cause (MAT-STE-2026-42067):
  ``riv_sabe_integer_phase3`` imported Core ``execute_reposting_entry`` at module
  load time, freezing the pre-bootstrap callable. RIV GL remake then used vanilla
  ``StockController.make_gl_entries`` + ``process_debit_credit_difference``
  (Stock Entry allowance 0.5), which throws on Difference is -1.0 instead of
  absorbing into Company.stock_adjustment_account (621301).
"""

from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.irr_gl_precision_align import (
	IRR_STOCK_VALUATION_RESIDUAL_REMARK,
	absorb_stock_valuation_precision_residual,
	align_irr_gl_map_to_currency_precision,
)
from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	PENDING_SABE_LOCALIZATION_ATTR,
	apply_sabe_localization_sa_gl,
	outstanding_sabe_a2_net,
)


def _entry(account, debit=0.0, credit=0.0, **extra):
	row = {
		"account": account,
		"debit": debit,
		"credit": credit,
		"debit_in_account_currency": debit,
		"credit_in_account_currency": credit,
		"debit_in_transaction_currency": debit,
		"credit_in_transaction_currency": credit,
		"cost_center": "CC",
		"company": "اسپاد فارمد دارو",
		"posting_date": "2026-09-07",
		"voucher_type": "Stock Entry",
		"voucher_no": "MAT-STE-2026-42067",
		"remarks": "test",
		"is_opening": "No",
	}
	row.update(extra)
	return row


def _flt_precision_safe(value, precision=None):
	num = float(value or 0)
	if precision is None:
		return num
	return float(round(num, int(precision)))


class TestManufactureGlRoundingResidualV572(unittest.TestCase):
	def test_plus_one_quantum_absorbed_to_stock_adjustment(self):
		gl_map = [
			_entry("111701 - WIP", credit=100.0),
			_entry("111625 - FG", debit=101.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(float(v or 0))),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.flt",
			side_effect=_flt_precision_safe,
		):
			ok = absorb_stock_valuation_precision_residual(
				gl_map, 1.0, 1.0, 0, company="اسپاد فارمد دارو"
			)
		self.assertTrue(ok)
		self.assertEqual(sum(float(e["debit"]) - float(e["credit"]) for e in gl_map), 0.0)
		sa = next(e for e in gl_map if e["account"] == "621301 - SA")
		self.assertEqual(float(sa["credit"]), 1.0)
		self.assertIn(IRR_STOCK_VALUATION_RESIDUAL_REMARK, sa.get("remarks") or "")

	def test_minus_one_quantum_absorbed_to_stock_adjustment(self):
		gl_map = [
			_entry("111701 - WIP", credit=101.0),
			_entry("111625 - FG", debit=100.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(float(v or 0))),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.flt",
			side_effect=_flt_precision_safe,
		):
			ok = absorb_stock_valuation_precision_residual(
				gl_map, -1.0, -1.0, 0, company="اسپاد فارمد دارو"
			)
		self.assertTrue(ok)
		self.assertEqual(sum(float(e["debit"]) - float(e["credit"]) for e in gl_map), 0.0)
		sa = next(e for e in gl_map if e["account"] == "621301 - SA")
		self.assertEqual(float(sa["debit"]), 1.0)

	def test_larger_a2_positive_and_negative_balance(self):
		import frappe

		cases = (520.0, -58.0, 776.0, -1.0, 1.0)
		for amount in cases:
			with self.subTest(amount=amount):
				def _cached(doctype, name, field=None, **kwargs):
					if doctype == "Company" and field == "stock_adjustment_account":
						return "621301 - SA"
					if doctype == "Company" and field == "round_off_cost_center":
						return "CC"
					if doctype == "Account" and field == "account_type":
						return "Stock"
					return None

				with patch(
					"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
					return_value=True,
				), patch(
					"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
					return_value="IRR",
				), patch(
					"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
					side_effect=lambda v, c: round(float(v)),
				), patch(
					"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
					return_value={"account": "Round Off", "cost_center": "CC"},
				), patch.object(frappe, "get_cached_value", side_effect=_cached):
					doc = frappe._dict(
						name="MAT-STE-A2",
						doctype="Stock Entry",
						company="E",
						purpose="Manufacture",
						value_difference=999999.0,
					)
					setattr(
						frappe.local,
						PENDING_SABE_LOCALIZATION_ATTR,
						[
							{
								"localization_difference": amount,
								"voucher_no": "MAT-STE-A2",
								"voucher_detail_no": "d1",
								"item_code": "X",
								"warehouse": "W",
								"detail": {"idempotency_key": f"IRR_SABE_A2|{amount}"},
							}
						],
					)
					gl_map = [
						frappe._dict(
							account="111701 - WIP",
							debit=0,
							credit=1000,
							cost_center="CC",
							project=None,
						),
						frappe._dict(
							account="111625 - FG",
							debit=1000,
							credit=0,
							cost_center="CC",
							project=None,
						),
					]
					out = apply_sabe_localization_sa_gl(doc, gl_map)
					net = sum(flt(e.get("debit")) - flt(e.get("credit")) for e in out)
					self.assertEqual(net, 0.0)
					partner = next(e for e in out if e.get("_irr_sabe_localization_partner"))
					self.assertEqual(partner.account, "Round Off")
					self.assertNotEqual(partner.account, "111701 - WIP")
					# Core WIP credit unchanged
					wip = next(e for e in out if e.get("account") == "111701 - WIP")
					self.assertEqual(flt(wip.credit), 1000)
					self.assertEqual(flt(wip.debit), 0)

	def test_a2_fully_covered_by_manufacture_value_difference(self):
		doc = frappe._dict(
			doctype="Stock Entry",
			name="MAT-STE-2026-30392",
			company="E",
			purpose="Manufacture",
			value_difference=520.0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.existing_sabe_a2_recognition",
			return_value={
				"stock_adjustment_account": "621301",
				"recognized_signed_credit_minus_debit": 0.0,
				"matches": [],
			},
		):
			recon = outstanding_sabe_a2_net(doc, 520.0)
		self.assertTrue(recon["value_difference_covers"])
		self.assertEqual(recon["outstanding_net"], 0.0)

	def test_a2_partially_covered_by_value_difference_no_false_match(self):
		doc = frappe._dict(
			doctype="Stock Entry",
			name="MAT-STE-PARTIAL",
			company="E",
			purpose="Manufacture",
			value_difference=58.0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.existing_sabe_a2_recognition",
			return_value={
				"stock_adjustment_account": "621301",
				"recognized_signed_credit_minus_debit": 0.0,
				"matches": [],
			},
		):
			# proposed 100, vd 58 → not exact → no coverage
			recon = outstanding_sabe_a2_net(doc, 100.0)
		self.assertFalse(recon["value_difference_covers"])
		self.assertEqual(recon["outstanding_net"], 100.0)

	def test_unrelated_gl_same_amount_not_coverage(self):
		"""Existing unmarked 621301 with same amount must not count outside RIV."""
		doc = frappe._dict(
			doctype="Stock Entry",
			name="MAT-STE-UNREL",
			company="E",
			purpose="Material Transfer",
			value_difference=0.0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.existing_sabe_a2_recognition",
			return_value={
				"stock_adjustment_account": "621301",
				"recognized_signed_credit_minus_debit": 0.0,  # unmarked ignored
				"matches": [],
			},
		), patch(
			"frappe.flags",
			{},
		):
			recon = outstanding_sabe_a2_net(doc, 520.0)
		self.assertEqual(recon["outstanding_net"], 520.0)

	def test_existing_520_and_58_vd_coverage(self):
		for voucher, amount in (("MAT-STE-2026-30392", 520.0), ("MAT-STE-2026-30451", -58.0)):
			with self.subTest(voucher=voucher):
				doc = frappe._dict(
					doctype="Stock Entry",
					name=voucher,
					company="E",
					purpose="Manufacture",
					value_difference=amount,
				)
				with patch(
					"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
					return_value="IRR",
				), patch(
					"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
					side_effect=lambda v, c: round(float(v)),
				), patch(
					"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.existing_sabe_a2_recognition",
					return_value={
						"stock_adjustment_account": "621301",
						"recognized_signed_credit_minus_debit": 0.0,
						"matches": [],
					},
				):
					recon = outstanding_sabe_a2_net(doc, amount)
				self.assertTrue(recon["value_difference_covers"])
				self.assertEqual(recon["outstanding_net"], 0.0)

	def test_align_absorbs_minus_one_before_validation(self):
		doc = frappe._dict(
			doctype="Stock Entry",
			name="MAT-STE-2026-42067",
			company="اسپاد فارمد دارو",
			purpose="Manufacture",
			posting_date="2026-09-07",
		)
		gl_map = [
			_entry("111701 - WIP", credit=101.0),
			_entry("111625 - FG", debit=100.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_currency_precision",
			return_value=0,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(float(v or 0))),
		):
			align_irr_gl_map_to_currency_precision(doc, gl_map)
		self.assertEqual(sum(float(e["debit"]) - float(e["credit"]) for e in gl_map), 0.0)

	def test_a2_skipped_when_in_map_sa_already_covers(self):
		"""Core Manufacture SA already in gl_map ⇒ do not double-post IRR_SABE_A2."""
		def _cached(doctype, name, field=None, **kwargs):
			if doctype == "Company" and field == "stock_adjustment_account":
				return "621301 - SA"
			if doctype == "Company" and field == "round_off_cost_center":
				return "CC"
			if doctype == "Account" and field == "account_type":
				return "Stock"
			return None

		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
			return_value={"account": "Round Off", "cost_center": "CC"},
		), patch.object(frappe, "get_cached_value", side_effect=_cached):
			doc = frappe._dict(
				name="MAT-STE-2026-30330-1",
				doctype="Stock Entry",
				company="E",
				purpose="Manufacture",
				posting_date="2026-06-16",
				value_difference=0.0,
			)
			setattr(
				frappe.local,
				PENDING_SABE_LOCALIZATION_ATTR,
				[
					{
						"localization_difference": 159.0,
						"voucher_no": "MAT-STE-2026-30330-1",
						"voucher_detail_no": "d1",
						"item_code": "X",
						"warehouse": "W",
						"detail": {"idempotency_key": "IRR_SABE_A2|159"},
					}
				],
			)
			gl_map = [
				frappe._dict(
					account="111701 - WIP",
					debit=0,
					credit=1000,
					cost_center="CC",
					posting_date="2026-06-16",
				),
				frappe._dict(
					account="621301 - SA",
					debit=0,
					credit=159,
					cost_center="CC",
					remarks="Rahkaran MANUFACTURE 135331",
					posting_date="2026-06-16",
				),
			]
			out = apply_sabe_localization_sa_gl(doc, gl_map)
			a2_legs = [e for e in out if e.get("_irr_sabe_localization")]
			self.assertEqual(a2_legs, [])
			self.assertEqual(len([e for e in out if e.get("account") == "621301 - SA"]), 1)
			self.assertTrue(doc.flags.iran_sabe_a2_recon.get("in_map_sa_covers"))

	def test_a2_gl_legs_include_department_dimension(self):
		from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
			PENDING_SABE_LOCALIZATION_ATTR,
			apply_sabe_localization_sa_gl,
		)

		doc = frappe._dict(
			name="MAT-STE-DIM-1",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			value_difference=0,
			items=[frappe._dict(department="Dept-Pack", cost_center="CC-Pack")],
		)
		setattr(
			doc,
			PENDING_SABE_LOCALIZATION_ATTR,
			[
				{
					"localization_difference": 7.0,
					"voucher_no": "MAT-STE-DIM-1",
					"voucher_detail_no": "d1",
					"item_code": "X",
					"warehouse": "W",
					"detail": {"idempotency_key": "IRR_SABE_A2|E|Stock Entry|MAT-STE-DIM-1|d1|7"},
				}
			],
		)
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])
		gl_map = [
			frappe._dict(
				account="Stock-WIP",
				debit=0,
				credit=100,
				cost_center="CC-Pack",
				project=None,
				posting_date="2026-09-02",
			)
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
			return_value={"account": "Round Off", "cost_center": "CC-RO"},
		), patch(
			"frappe.get_cached_value",
			side_effect=lambda *a, **k: {
				("Company", "E", "stock_adjustment_account"): "621301",
				("Account", "Stock-WIP", "account_type"): "Stock",
			}.get(a if len(a) == 3 else a[:3]),
		), patch(
			"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions",
			return_value=["cost_center", "department"],
		):
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		sa = [e for e in out if e.get("account") == "621301"][0]
		partner = next(e for e in out if e.get("_irr_sabe_localization_partner"))
		self.assertEqual(sa.get("department"), "Dept-Pack")
		self.assertEqual(sa.get("cost_center"), "CC-Pack")
		self.assertEqual(sa.get("posting_date"), "2026-09-02")
		self.assertEqual(partner.account, "Round Off")
		self.assertEqual(partner.get("cost_center"), "CC-RO")
		# Core stock leg untouched
		self.assertEqual(flt(out[0].credit), 100)
		self.assertEqual(flt(out[0].debit), 0)

	def test_a2_gl_legs_include_posting_date(self):
		"""RIV remake submits GLE; missing posting_date → KeyError fiscal_year[0]."""
		def _cached(doctype, name, field=None, **kwargs):
			if doctype == "Company" and field == "stock_adjustment_account":
				return "621301 - SA"
			if doctype == "Company" and field == "round_off_cost_center":
				return "CC"
			if doctype == "Account" and field == "account_type":
				return "Stock"
			return None

		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
			return_value={"account": "Round Off", "cost_center": "CC"},
		), patch.object(frappe, "get_cached_value", side_effect=_cached):
			doc = frappe._dict(
				name="MAT-STE-A2-DATE",
				doctype="Stock Entry",
				company="E",
				purpose="Manufacture",
				posting_date="2026-06-17",
				value_difference=999999.0,
			)
			setattr(
				frappe.local,
				PENDING_SABE_LOCALIZATION_ATTR,
				[
					{
						"localization_difference": 13.0,
						"voucher_no": "MAT-STE-A2-DATE",
						"voucher_detail_no": "d1",
						"item_code": "X",
						"warehouse": "W",
						"detail": {"idempotency_key": "IRR_SABE_A2|13"},
					}
				],
			)
			gl_map = [
				frappe._dict(
					account="111701 - WIP",
					debit=0,
					credit=100,
					cost_center="CC",
					project=None,
					posting_date="2026-06-17",
					is_opening="No",
				)
			]
			out = apply_sabe_localization_sa_gl(doc, gl_map)
			sa = next(e for e in out if e.get("_irr_sabe_localization"))
			partner = next(e for e in out if e.get("_irr_sabe_localization_partner"))
			self.assertEqual(sa.get("posting_date"), "2026-06-17")
			self.assertEqual(partner.get("posting_date"), "2026-06-17")
			self.assertEqual(sa.get("is_opening"), "No")

	def test_phase3_does_not_bind_core_execute_at_import(self):
		import erpnext_extensions.iran_accounting.domain.riv_sabe_integer_phase3 as phase3

		src = inspect.getsource(phase3)
		tree = ast.parse(src)
		# No top-level ``from erpnext... import execute_reposting_entry``
		for node in tree.body:
			if isinstance(node, ast.ImportFrom):
				names = {a.name for a in node.names}
				self.assertNotIn(
					"execute_reposting_entry",
					names,
					"phase3 must not import-bind Core execute_reposting_entry",
				)
		create_src = inspect.getsource(phase3.create_and_execute_riv)
		self.assertIn("riv_execute_guard", create_src)
		self.assertIn("ensure_iran_riv_gl_handlers_active", create_src)
		self.assertIn("bootstrap_apply", create_src)

	def test_riv_guard_ensures_gl_handlers(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		src = inspect.getsource(guard._ensure_iran_runtime_for_riv)
		self.assertIn("ensure_iran_riv_gl_handlers_active", src)

	def test_exact_debit_credit_equality_contract(self):
		"""No tolerance: final map must satisfy sum(debit)-sum(credit)==0."""
		gl_map = [
			_entry("111701 - WIP", credit=50.0),
			_entry("111625 - FG", debit=49.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(float(v or 0))),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.flt",
			side_effect=_flt_precision_safe,
		):
			absorb_stock_valuation_precision_residual(
				gl_map, -1.0, -1.0, 0, company="اسپاد فارمد دارو"
			)
		diff = sum(float(e["debit"]) - float(e["credit"]) for e in gl_map)
		self.assertEqual(diff, 0.0)


if __name__ == "__main__":
	unittest.main()
