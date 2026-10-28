# Copyright (c) 2026, ERPNext Extensions contributors
"""MAT-PRE-2026-01642 shape: FX integer IRR VR + Stock Adjustment (v5.6.26).

EUR 0.4 × FX 2_830_792 = 1_132_316.8 → ROUND_HALF_UP → 1_132_317.
qty 51_000 × integer VR overstates inventory by 10_200 vs commercial base_net.
GRNI stays at commercial auth; Stock Adjustment credits the 10_200 gap.
"""

from __future__ import annotations

import unittest
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	STATUS_READY,
	classify_amount_rate_residual,
	classify_document_residuals,
	evaluate_irr_rate_rounding_residual,
)
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	_align_purchase_valuation_rate_amount_preserving,
	_is_fx_purchase_doc,
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


class TestFxPrIntegerVrAlignment(unittest.TestCase):
	def test_fx_doc_detected(self):
		doc = _PR(_Row())
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		):
			self.assertTrue(_is_fx_purchase_doc(doc))

	def test_fx_align_forces_integer_vr(self):
		row = _Row(
			qty=QTY,
			stock_qty=QTY,
			conversion_factor=1,
			valuation_rate=PRESERVING_VR,
			base_net_amount=AUTH,
			base_amount=QTY * INT_VR,
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			amount_difference_with_purchase_invoice=0,
		)
		doc = _PR(row)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		):
			_align_purchase_valuation_rate_amount_preserving(row, doc)
		self.assertEqual(flt(row.valuation_rate), INT_VR)


class TestFxPrStandaloneStockAdjustment(unittest.TestCase):
	def test_generic_classifier_marks_class_a(self):
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

	def test_pr_document_path_standalone_sa_inverts_sign(self):
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
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_stock_adjustment_residual",
				return_value={"account": "SA", "cost_center": "CC"},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.validate_stock_adjustment_residual_configuration",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.resolve_round_off_dimensions",
				return_value={},
			),
		):
			a, b = classify_document_residuals(doc)
			decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)
		self.assertEqual(b, [])
		self.assertEqual(len(a), 1)
		self.assertTrue(a[0].get("pr_integer_rate_standalone_sa"))
		self.assertTrue(a[0].get("fx_integer_rate_standalone_sa"))  # FX alias
		# Inverted vs default incoming (−residual): residual −10200 → SA credit.
		self.assertEqual(flt(a[0]["round_off_debit"]), -GAP)
		self.assertEqual(decision.status, STATUS_READY)
		self.assertTrue(decision.standalone_sa)
		self.assertEqual(flt(decision.net_signed_debit), -GAP)
		self.assertIn("standalone_sa", decision.messages[0])


class TestFxPrDetection(unittest.TestCase):
	def test_fractional_vr_flagged_for_integer_sa(self):
		row = {
			"qty": QTY,
			"stock_qty": QTY,
			"base_net_amount": AUTH,
			"base_amount": AUTH,
			"valuation_rate": PRESERVING_VR,
			"billed_amt": 0,
			"per_billed": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
			"item_code": "13100023",
		}
		out = classify_pr_item_discrepancy(row, sle_svd=AUTH)
		self.assertIsNotNone(out)
		self.assertIn("unbilled_fx_fractional_vr_needs_integer_sa", out["notes"])
		self.assertEqual(out["repair_eligibility"], "FX_INTEGER_VR_STOCK_ADJUSTMENT")
		self.assertEqual(out["fx_rounding_gap"], GAP)

	def test_correct_integer_inventory_not_flagged(self):
		row = {
			"qty": QTY,
			"base_net_amount": AUTH,
			"valuation_rate": INT_VR,
			"billed_amt": 0,
			"per_billed": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
		}
		self.assertIsNone(classify_pr_item_discrepancy(row, sle_svd=QTY * INT_VR))

	def test_integer_vr_sle_lag_flagged(self):
		row = {
			"qty": QTY,
			"base_net_amount": AUTH,
			"valuation_rate": INT_VR,
			"billed_amt": 0,
			"per_billed": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
		}
		out = classify_pr_item_discrepancy(row, sle_svd=AUTH)
		self.assertIsNotNone(out)
		self.assertIn("unbilled_fx_integer_vr_sle_lags_inventory", out["notes"])

	def test_expected_acquisition_uses_base_net(self):
		self.assertEqual(
			expected_acquisition_amount({"base_net_amount": AUTH, "base_amount": QTY * INT_VR}),
			AUTH,
		)


class TestAuthoritativeAcquisitionKeepsDocumentIntegerVr(unittest.TestCase):
	def test_document_integer_vr_is_authoritative(self):
		from erpnext_extensions.iran_accounting.domain import stock_ledger_deterministic as sld

		sle = {
			"voucher_type": "Purchase Receipt",
			"voucher_no": "PR-X",
			"voucher_detail_no": "ROW-X",
			"item_code": "13100023",
			"actual_qty": QTY,
		}
		row = {"item_code": "13100023", "valuation_rate": INT_VR}

		with mock.patch("frappe.db.get_value") as gv:

			def _gv(dt, name, fields=None, as_dict=False):
				if dt == "Purchase Receipt":
					return {"is_return": 0, "is_internal_supplier": 0} if as_dict else (0, 0)
				if dt == "Purchase Receipt Item":
					return row if as_dict else INT_VR
				return None

			gv.side_effect = _gv
			rate = sld.authoritative_acquisition_incoming_rate(sle, "IRR")
		self.assertEqual(flt(rate), INT_VR)


if __name__ == "__main__":
	unittest.main()
