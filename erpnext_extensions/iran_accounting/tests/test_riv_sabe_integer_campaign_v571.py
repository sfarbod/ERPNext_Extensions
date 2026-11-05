# Copyright (c) 2026, ERPNext Extensions contributors
"""RIV SABE integerization: A2 dedupe, mid-process align_sle=False, dry-run preview."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	PENDING_SABE_LOCALIZATION_ATTR,
	apply_sabe_localization_sa_gl,
	localize_sabe_row_values,
	make_sabe_a2_idempotency_key,
	outstanding_sabe_a2_net,
)


class TestOutstandingA2Dedupe(unittest.TestCase):
	def tearDown(self):
		frappe.flags.through_repost_item_valuation = False
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])

	def test_manufacture_value_difference_covers_exact_a2(self):
		doc = frappe._dict(
			name="MAT-STE-2026-30392",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			value_difference=520,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		):
			frappe.flags.through_repost_item_valuation = True
			recon = outstanding_sabe_a2_net(doc, 520.0)
		self.assertTrue(recon["value_difference_covers"])
		self.assertEqual(recon["outstanding_net"], 0.0)

	def test_unrelated_large_value_difference_does_not_cover(self):
		doc = frappe._dict(
			name="MAT-STE-2026-25331",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			value_difference=1209990,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		):
			frappe.flags.through_repost_item_valuation = True
			recon = outstanding_sabe_a2_net(doc, 948.0)
		self.assertFalse(recon["value_difference_covers"])
		self.assertEqual(recon["outstanding_net"], 948.0)

	def test_marked_gl_dedupe_outside_riv(self):
		doc = frappe._dict(
			name="STE-X",
			doctype="Stock Entry",
			company="E",
			purpose="Material Transfer",
			value_difference=0,
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
				"recognized_signed_credit_minus_debit": 30.0,
				"matches": [{"source": "gl_sabe_a2_marker", "signed": 30.0}],
			},
		):
			frappe.flags.through_repost_item_valuation = False
			recon = outstanding_sabe_a2_net(doc, 30.0)
		self.assertEqual(recon["outstanding_net"], 0.0)

	def test_gl_apply_skips_when_outstanding_zero(self):
		doc = frappe._dict(
			name="MAT-STE-2026-30392",
			doctype="Stock Entry",
			company="E",
			purpose="Manufacture",
			value_difference=520,
		)
		setattr(
			doc,
			PENDING_SABE_LOCALIZATION_ATTR,
			[
				{
					"localization_difference": 520.0,
					"voucher_no": "MAT-STE-2026-30392",
					"voucher_detail_no": "d1",
					"item_code": "X",
					"warehouse": "W",
					"detail": {"idempotency_key": "IRR_SABE_A2|E|Stock Entry|MAT-STE-2026-30392|d1|520"},
				}
			],
		)
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])
		gl_map = [frappe._dict(account="Stock", debit=0, credit=100, cost_center="Main", project=None)]
		with patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: round(float(v)),
		):
			frappe.flags.through_repost_item_valuation = True
			out = apply_sabe_localization_sa_gl(doc, gl_map)
		self.assertEqual(len(out), 1)  # no SA legs added
		self.assertTrue(getattr(doc.flags, "iran_sabe_a2_recon", {}).get("value_difference_covers"))


class TestAlignSleFlag(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company", return_value=True)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
		return_value="IRR",
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
		side_effect=lambda v, c: round(float(v)),
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_monetary_rate",
		side_effect=lambda v, c: round(float(v)),
	)
	def test_align_sle_false_does_not_mutate_sle_svd(self, *_mocks):
		from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
			apply_integer_sabe_policy_to_sle,
		)

		sle = frappe._dict(
			company="E",
			serial_and_batch_bundle="B1",
			item_code="13200333",
			warehouse="W",
			voucher_type="Stock Entry",
			voucher_no="STE-1",
			voucher_detail_no="d1",
			actual_qty=-10,
			qty_after_transaction=5,
			stock_value_difference=-1004.0,
			stock_value=5000.0,
			valuation_rate=100,
			outgoing_rate=100.4,
		)
		rows = [
			frappe._dict(
				name="s1",
				qty=-10,
				incoming_rate=0,
				outgoing_rate=100.4,
				stock_value_difference=-1004.0,
				batch_no="B",
			)
		]
		with patch.object(frappe, "get_all", return_value=rows), patch.object(
			frappe.db, "set_value"
		) as set_value:
			setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])
			out = apply_integer_sabe_policy_to_sle(sle, align_sle=False)
			self.assertFalse(out["sle_adjusted"])
			self.assertEqual(sle.stock_value_difference, -1004.0)  # unchanged
			self.assertTrue(set_value.called)
			pending = getattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR) or []
			self.assertEqual(len(pending), 1)
			# integerized=-1000, ROUND(raw)=-1004 → A2=+4
			self.assertEqual(pending[0]["localization_difference"], 4.0)


class TestIdempotencyKey(unittest.TestCase):
	def test_key_stable(self):
		a = make_sabe_a2_idempotency_key(
			voucher_type="Stock Entry",
			voucher_no="STE-1",
			voucher_detail_no="d1",
			company="E",
			amount=520.4,
		)
		b = make_sabe_a2_idempotency_key(
			voucher_type="Stock Entry",
			voucher_no="STE-1",
			voucher_detail_no="d1",
			company="E",
			amount=520.4,
		)
		self.assertEqual(a, b)
		self.assertIn("IRR_SABE_A2|", a)


class TestCaseABClassification(unittest.TestCase):
	def test_102_not_in_paired_mt_net(self):
		# Paired MT A2 cancel — net 0; 102 is not a dry-run line sum.
		a = localize_sabe_row_values(qty=-126, raw_rate=28297.142857, raw_svd=-3565452)
		b = localize_sabe_row_values(qty=126, raw_rate=28297.142857, raw_svd=3565452)
		self.assertEqual(a["localization_difference"] + b["localization_difference"], 0.0)

	def test_type_b_gap_is_not_a2_formula(self):
		# Batch integer amount vs WH clear — A2 on integer batch row is 0.
		loc = localize_sabe_row_values(qty=-1532, raw_rate=408848, raw_svd=-626355136)
		self.assertEqual(loc["localization_difference"], 0.0)
		type_b = 626355731 - 626355136
		self.assertEqual(type_b, 595)


class TestRivDryRunPreviewUnit(unittest.TestCase):
	@patch(
		"erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign.find_fractional_sabe_rows",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign._case_b_notes",
		return_value={"type_b_gap": 595, "classification": "Type B"},
	)
	def test_empty_scope_preview(self, *_mocks):
		from unittest.mock import mock_open

		from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign import (
			build_riv_scope_for_items,
		)

		with patch(
			"erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign._audit_dir",
			return_value="/tmp",
		), patch("builtins.open", mock_open()):
			out = build_riv_scope_for_items(item_codes=["13200333"])
		self.assertEqual(out["mode"], "riv_dry_run_preview")
		self.assertEqual(out["execution_status"], "NOT_EXECUTED")
		self.assertEqual(out["fractional_sabe_count"], 0)


if __name__ == "__main__":
	unittest.main()
