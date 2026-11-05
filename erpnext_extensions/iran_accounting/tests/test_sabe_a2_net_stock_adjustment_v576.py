# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.43 — A2 is intermediate; post only final net Stock Adjustment (no partner)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	PENDING_SABE_LOCALIZATION_ATTR,
	apply_sabe_localization_sa_gl,
	reconcile_final_stock_adjustment_net,
)


SA = "621301 - SA"
RO = "622515 - Round Off"
WIP = "111701 - WIP"
FG = "111625 - FG"


def _patches():
	return (
		patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.existing_sabe_a2_recognition",
			return_value={
				"stock_adjustment_account": SA,
				"recognized_signed_credit_minus_debit": 0.0,
				"matches": [],
			},
		),
		patch(
			"frappe.get_cached_value",
			side_effect=lambda *a, **k: {
				("Company", "E", "stock_adjustment_account"): SA,
			}.get(a if len(a) == 3 else a[:3]),
		),
		patch(
			"erpnext.accounts.doctype.accounting_dimension.accounting_dimension.get_accounting_dimensions",
			return_value=["cost_center", "department"],
		),
	)


def _doc(**kwargs):
	base = dict(
		name="MAT-STE-NET",
		doctype="Stock Entry",
		company="E",
		purpose="Manufacture",
		value_difference=0.0,
		posting_date="2026-09-02",
		items=[frappe._dict(department="D", cost_center="CC")],
	)
	base.update(kwargs)
	return frappe._dict(base)


def _queue(doc, amount, voucher_no=None):
	vn = voucher_no or doc.name
	payload = [
		{
			"localization_difference": amount,
			"voucher_no": vn,
			"voucher_detail_no": "d1",
			"item_code": "X",
			"warehouse": "W",
			"detail": {"idempotency_key": f"IRR_SABE_A2|{amount}"},
		}
	]
	setattr(doc, PENDING_SABE_LOCALIZATION_ATTR, payload)
	setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])


class TestReconcileFinalStockAdjustmentNetV576(unittest.TestCase):
	"""Source-level Core↔A2 reconciliation formula (no hardcoded 461)."""

	def _recon(self, core_sa, proposed_a2, *, purpose="Manufacture", vd=0.0):
		doc = _doc(purpose=purpose, value_difference=vd)
		gl_map = []
		if abs(core_sa) >= 0.5:
			gl_map.append(
				frappe._dict(
					account=SA,
					debit=abs(core_sa) if core_sa < 0 else 0.0,
					credit=core_sa if core_sa > 0 else 0.0,
					remarks="Accounting Entry for Stock",
				)
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
				"stock_adjustment_account": SA,
				"recognized_signed_credit_minus_debit": 0.0,
				"matches": [],
			},
		), patch(
			"frappe.get_cached_value",
			side_effect=lambda *a, **k: SA
			if a and a[-1] == "stock_adjustment_account"
			else None,
		), patch("frappe.flags", {}):
			return reconcile_final_stock_adjustment_net(doc, gl_map, proposed_a2)

	def test_positive_core_sa_eliminates_positive_a2(self):
		# Core Cr 461, A2 +1970 → final = 461, outstanding = 0
		recon = self._recon(461.0, 1970.0)
		self.assertEqual(recon["core_sa_net"], 461.0)
		self.assertEqual(recon["final_sa_net"], 461.0)
		self.assertEqual(recon["a2_recon_effect"], -1970.0)
		self.assertEqual(recon["gross_sa_before_recon"], 2431.0)
		self.assertEqual(recon["outstanding_to_post"], 0.0)
		self.assertEqual(recon["coverage_source"], "core_stock_adjustment")

	def test_negative_core_sa_eliminates_negative_a2(self):
		recon = self._recon(-100.0, -40.0)
		self.assertEqual(recon["final_sa_net"], -100.0)
		self.assertEqual(recon["a2_recon_effect"], 40.0)
		self.assertEqual(recon["outstanding_to_post"], 0.0)

	def test_positive_core_with_negative_a2(self):
		recon = self._recon(50.0, -20.0)
		self.assertEqual(recon["final_sa_net"], 50.0)
		self.assertEqual(recon["a2_recon_effect"], 20.0)
		self.assertEqual(recon["outstanding_to_post"], 0.0)

	def test_negative_core_with_positive_a2(self):
		recon = self._recon(-80.0, 30.0)
		self.assertEqual(recon["final_sa_net"], -80.0)
		self.assertEqual(recon["outstanding_to_post"], 0.0)

	def test_core_covers_all_a2_via_value_difference(self):
		recon = self._recon(0.0, 520.0, vd=520.0)
		self.assertTrue(recon["value_difference_covers"])
		self.assertEqual(recon["outstanding_to_post"], 0.0)
		self.assertEqual(recon["coverage_source"], "manufacture_value_difference")

	def test_a2_only_partially_in_value_difference_no_false_cover(self):
		recon = self._recon(0.0, 100.0, vd=58.0)
		self.assertFalse(recon["value_difference_covers"])
		self.assertEqual(recon["final_sa_net"], 100.0)
		self.assertEqual(recon["outstanding_to_post"], 100.0)
		self.assertEqual(recon["coverage_source"], "a2_becomes_stock_adjustment")

	def test_no_core_sa_a2_becomes_final(self):
		recon = self._recon(0.0, 1970.0, purpose="Material Transfer")
		self.assertEqual(recon["final_sa_net"], 1970.0)
		self.assertEqual(recon["a2_recon_effect"], 0.0)
		self.assertEqual(recon["outstanding_to_post"], 1970.0)

	def test_no_core_negative_a2(self):
		recon = self._recon(0.0, -58.0, purpose="Material Transfer")
		self.assertEqual(recon["final_sa_net"], -58.0)
		self.assertEqual(recon["outstanding_to_post"], -58.0)

	def test_formula_identity_gross_plus_recon_effect(self):
		for core, a2 in ((461, 1970), (100, -40), (-50, 10), (0, 7)):
			with self.subTest(core=core, a2=a2):
				recon = self._recon(float(core), float(a2), purpose="Material Transfer" if not core else "Manufacture")
				self.assertEqual(
					recon["final_sa_net"],
					round(recon["gross_sa_before_recon"] + recon["a2_recon_effect"]),
				)


class TestApplySabeNetStockAdjustmentOnlyV576(unittest.TestCase):
	def test_43536_shape_posts_no_a2_and_no_round_off(self):
		"""Core SA already Cr 461; A2 1970 eliminated — no partner, no extra SA."""
		doc = _doc(name="MAT-STE-2026-43536", value_difference=2431.0)
		_queue(doc, 1970.0)
		# Balanced Core map: WIP Cr + SA Cr = FG/scrap/pack Dr (simplified)
		gl_map = [
			frappe._dict(account=WIP, debit=0, credit=1000, cost_center="CC", posting_date="2026-09-02"),
			frappe._dict(
				account=SA,
				debit=0,
				credit=461,
				cost_center="CC",
				remarks="Accounting Entry for Stock",
				posting_date="2026-09-02",
			),
			frappe._dict(account=FG, debit=1461, credit=0, cost_center="CC", posting_date="2026-09-02"),
		]
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		self.assertFalse(any(e.get("_irr_sabe_localization") for e in out))
		self.assertFalse(any(e.get("_irr_sabe_localization_partner") for e in out))
		self.assertFalse(any(e.get("account") == RO for e in out))
		sa = [e for e in out if e.get("account") == SA]
		self.assertEqual(len(sa), 1)
		self.assertEqual(flt(sa[0].credit), 461.0)
		self.assertEqual(flt(sa[0].debit), 0.0)
		self.assertEqual(sum(flt(e.get("debit")) - flt(e.get("credit")) for e in out), 0.0)
		recon = doc.flags.iran_sabe_a2_recon
		self.assertEqual(recon["final_sa_net"], 461.0)
		self.assertEqual(recon["outstanding_to_post"], 0.0)

	def test_balanced_map_without_core_sa_does_not_invent_partner(self):
		"""A2 intermediate with already-balanced inventory map → no artificial counter."""
		doc = _doc(purpose="Material Transfer")
		_queue(doc, 30.0)
		gl_map = [
			frappe._dict(account="Stock-A", debit=0, credit=100, cost_center="CC", posting_date="2026-09-02"),
			frappe._dict(account="Stock-B", debit=100, credit=0, cost_center="CC", posting_date="2026-09-02"),
		]
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		self.assertEqual(len(out), 2)
		self.assertTrue(doc.flags.iran_sabe_a2_recon.get("skipped_unbalanced_without_partner"))
		self.assertFalse(any(e.get("account") == RO for e in out))

	def test_sa_only_append_when_map_gap_equals_outstanding(self):
		doc = _doc(purpose="Material Transfer")
		_queue(doc, 7.0)
		# Excess debit 7 → needs SA credit 7; no partner.
		gl_map = [
			frappe._dict(
				account="Stock-A",
				debit=107,
				credit=0,
				cost_center="CC",
				department="D",
				posting_date="2026-09-02",
			),
			frappe._dict(
				account="Stock-B",
				debit=0,
				credit=100,
				cost_center="CC",
				department="D",
				posting_date="2026-09-02",
			),
		]
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		sa = next(e for e in out if e.get("_irr_sabe_localization"))
		self.assertEqual(sa.account, SA)
		self.assertEqual(flt(sa.credit), 7.0)
		self.assertEqual(flt(sa.credit_in_account_currency), 7.0)
		self.assertFalse(any(e.get("_irr_sabe_localization_partner") for e in out))
		self.assertEqual(sum(flt(e.get("debit")) - flt(e.get("credit")) for e in out), 0.0)
		self.assertEqual(sa.get("department"), "D")
		self.assertEqual(sa.get("posting_date"), "2026-09-02")

	def test_negative_outstanding_posts_debit_sa_only(self):
		doc = _doc(purpose="Material Transfer")
		_queue(doc, -13.0)
		# Excess credit 13 → needs SA debit 13
		gl_map = [
			frappe._dict(account="Stock-A", debit=0, credit=113, cost_center="CC", posting_date="2026-09-02"),
			frappe._dict(account="Stock-B", debit=100, credit=0, cost_center="CC", posting_date="2026-09-02"),
		]
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		sa = next(e for e in out if e.get("_irr_sabe_localization"))
		self.assertEqual(flt(sa.debit), 13.0)
		self.assertEqual(flt(sa.credit), 0.0)
		self.assertFalse(any(e.get("_irr_sabe_localization_partner") for e in out))
		self.assertEqual(sum(flt(e.get("debit")) - flt(e.get("credit")) for e in out), 0.0)

	def test_no_duplicate_when_already_marked_a2_in_map(self):
		doc = _doc()
		_queue(doc, 50.0)
		gl_map = [
			frappe._dict(
				account=SA,
				debit=0,
				credit=50,
				_irr_sabe_localization=1,
				remarks="IRR_SABE_A2|prior",
				cost_center="CC",
			)
		]
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5]:
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		self.assertEqual(len([e for e in out if e.get("_irr_sabe_localization")]), 1)


if __name__ == "__main__":
	unittest.main()
