# Copyright (c) 2026, ERPNext Extensions contributors
"""MAT-PRE-2026-01642 shape: FX rate-first integer VR vs amount-preserving auth.

EUR 0.4 × FX 2_830_792 = 1_132_316.8 → historically integerized to 1_132_317.
qty 51_000 × integer VR overstates SLE by 10_200 vs base_net (EUR×FX).
"""

from __future__ import annotations

import unittest
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	STATUS_BYPASS,
	classify_amount_rate_residual,
	classify_document_residuals,
	evaluate_irr_rate_rounding_residual,
)
from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
	classify_pr_item_discrepancy,
	expected_acquisition_amount,
)


QTY = 51000.0
AUTH = 57748156800.0  # 20400 EUR × 2830792
INT_VR = 1132317.0
PRESERVING_VR = AUTH / QTY  # 1132316.8
GAP = 10200.0


class _Row(dict):
	def get(self, k, default=None):
		return super().get(k, default)


class _PR:
	doctype = "Purchase Receipt"
	company = "C"
	name = "MAT-PRE-2026-01642"
	currency = "EUR"
	conversion_rate = 2830792.0
	is_old_subcontracting_flow = 0

	def __init__(self, row):
		self.items = [row]

	def get(self, k, default=None):
		return getattr(self, k, default)


class TestFxRateFirstMath(unittest.TestCase):
	def test_gap_is_exactly_10200(self):
		self.assertEqual(QTY * INT_VR - AUTH, GAP)
		self.assertAlmostEqual(0.4 * 2830792.0, 1132316.8, places=1)
		self.assertEqual(round(1132316.8), 1132317)
		self.assertAlmostEqual(QTY * PRESERVING_VR, AUTH, places=6)


class TestFxPrResidualSkip(unittest.TestCase):
	def test_generic_classifier_still_marks_class_a(self):
		# Generic helper keeps Class A provenance; PR document path remaps to skip.
		out = classify_amount_rate_residual(
			qty=QTY,
			authoritative_amount=AUTH,
			valuation_rate=INT_VR,
			currency="IRR",
			item_code="13100023",
			idx=1,
		)
		self.assertEqual(out["class"], "A")
		self.assertEqual(flt(out["residual"]), -GAP)

	def test_pr_document_path_skips_amount_preserving(self):
		row = _Row(
			item_code="13100023",
			idx=1,
			name="row1",
			qty=QTY,
			stock_qty=QTY,
			conversion_factor=1,
			valuation_rate=INT_VR,
			base_rate=INT_VR,
			base_amount=QTY * INT_VR,
			base_net_amount=AUTH,
			amount=20400,
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			amount_difference_with_purchase_invoice=0,
			is_fixed_asset=0,
		)
		doc = _PR(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_purchase_receipt_stock_valuation_eligible_item_codes",
				return_value=frozenset({"13100023"}),
			),
		):
			a, b = classify_document_residuals(doc)
			decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)
		self.assertEqual(a, [])
		self.assertEqual(b, [])
		self.assertEqual(decision.status, STATUS_BYPASS)
		self.assertEqual(flt(decision.net_signed_debit), 0)


class TestFxPrDetection(unittest.TestCase):
	def test_unbilled_rate_first_flagged(self):
		row = {
			"qty": QTY,
			"stock_qty": QTY,
			"base_net_amount": AUTH,
			"base_amount": QTY * INT_VR,
			"valuation_rate": INT_VR,
			"billed_amt": 0,
			"per_billed": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
			"item_code": "13100023",
		}
		out = classify_pr_item_discrepancy(row, sle_svd=QTY * INT_VR)
		self.assertIsNotNone(out)
		self.assertEqual(out["difference"], GAP)
		self.assertEqual(out["expected"], AUTH)
		self.assertIn("unbilled_rate_first_integer_vr_sle_ne_auth", out["notes"])

	def test_unbilled_matching_sle_not_flagged(self):
		row = {
			"qty": QTY,
			"base_net_amount": AUTH,
			"valuation_rate": PRESERVING_VR,
			"billed_amt": 0,
			"per_billed": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
		}
		self.assertIsNone(classify_pr_item_discrepancy(row, sle_svd=AUTH))

	def test_expected_acquisition_uses_base_net(self):
		self.assertEqual(
			expected_acquisition_amount({"base_net_amount": AUTH, "base_amount": QTY * INT_VR}),
			AUTH,
		)


class TestAuthoritativeAcquisitionPrefersAmountPreserving(unittest.TestCase):
	def test_stale_integer_vr_yields_preserving_rate(self):
		from erpnext_extensions.iran_accounting.domain import stock_ledger_deterministic as sld

		sle = {
			"voucher_type": "Purchase Receipt",
			"voucher_no": "PR-X",
			"voucher_detail_no": "ROW-X",
			"item_code": "13100023",
			"actual_qty": QTY,
		}
		parent = mock.Mock()
		parent.is_return = 0
		parent.is_internal_supplier = 0
		parent.get = lambda k, d=None: getattr(parent, k, d)
		parent.is_old_subcontracting_flow = 0

		row = {
			"item_code": "13100023",
			"valuation_rate": INT_VR,
			"qty": QTY,
			"stock_qty": QTY,
			"conversion_factor": 1,
			"base_net_amount": AUTH,
			"base_amount": QTY * INT_VR,
			"item_tax_amount": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
			"sales_incoming_rate": 0,
			"rejected_qty": 0,
			"net_rate": 0,
			"rm_supp_cost": 0,
		}

		with (
			mock.patch("frappe.db.get_value") as gv,
			mock.patch("frappe.get_doc", return_value=parent),
		):

			def _gv(dt, name, fields=None, as_dict=False):
				if dt == "Purchase Receipt":
					return {"is_return": 0, "is_internal_supplier": 0} if as_dict else (0, 0)
				if dt == "Purchase Receipt Item":
					return row if as_dict else INT_VR
				return None

			gv.side_effect = _gv
			rate = sld.authoritative_acquisition_incoming_rate(sle, "IRR")
		self.assertAlmostEqual(flt(rate), PRESERVING_VR, places=6)
		self.assertAlmostEqual(flt(rate) * QTY, AUTH, places=4)


if __name__ == "__main__":
	unittest.main()
