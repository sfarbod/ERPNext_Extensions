# Copyright (c) 2026, ERPNext Extensions contributors
"""5.5.28: historical Purchase Receipt UVR float valuation_rate → Class A.

Reproduces MAT-PRE-2026-00780 (IRR multi-row IEEE float) and
MAT-PRE-2026-00707 (EUR→IRR + LCV, DECIMAL(30,9) float) shapes without
mutating documents. True non-integer mismatches stay Class B.
"""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	STATUS_BYPASS,
	STATUS_CLASS_B,
	STATUS_READY,
	classify_amount_rate_residual,
	classify_document_residuals,
	evaluate_irr_rate_rounding_residual,
	is_legacy_uvr_float_valuation_rate,
)


class TestLegacyUvrFloatDetection(unittest.TestCase):
	def test_ieee_float_auth_div_qty(self):
		# MAT-PRE-2026-00780 row 1
		self.assertTrue(
			is_legacy_uvr_float_valuation_rate(
				488767096.0, 15.0, 32584473.066666666, "IRR"
			)
		)

	def test_decimal9_half_up_auth_div_qty(self):
		# MAT-PRE-2026-00707 — MariaDB DECIMAL(30,9) of 12104137419/135000
		self.assertTrue(
			is_legacy_uvr_float_valuation_rate(
				12104137419.0, 135000.0, 89660.277177778, "IRR"
			)
		)

	def test_mismatch_amount_not_legacy(self):
		# Existing Class B unit-test shape: float rate of a different amount
		self.assertFalse(
			is_legacy_uvr_float_valuation_rate(
				510000000.0, 15.0, 32584473.066666666, "IRR"
			)
		)

	def test_arbitrary_float_rounding_to_int_from_amount_not_legacy(self):
		# ROUND(33.1)=33=ROUND(100/3) but 33.1 is not auth/qty reconstruction
		self.assertFalse(is_legacy_uvr_float_valuation_rate(100, 3, 33.1, "IRR"))


class TestClassifyLegacyUvrFloat(unittest.TestCase):
	def test_00780_row1_becomes_class_a(self):
		out = classify_amount_rate_residual(
			qty=15.0,
			authoritative_amount=488767096.0,
			valuation_rate=32584473.066666666,
			currency="IRR",
			item_code="16800200",
			idx=1,
		)
		self.assertEqual(out["class"], "A")
		self.assertEqual(out["reason"], "amount_authoritative_legacy_uvr_float_rate")
		self.assertEqual(out["expected_valuation_rate"], 32584473.0)
		self.assertEqual(out["residual"], 1.0)
		self.assertEqual(out["legacy_uvr_float_valuation_rate"], 32584473.066666666)

	def test_00780_row2_negative_residual_class_a(self):
		out = classify_amount_rate_residual(
			qty=6.0,
			authoritative_amount=557769509.0,
			valuation_rate=92961584.83333333,
			currency="IRR",
			item_code="16800203",
			idx=2,
		)
		self.assertEqual(out["class"], "A")
		self.assertEqual(out["residual"], -1.0)

	def test_00707_lcv_large_qty_class_a(self):
		# residual 37419 < path_derived_bound(135000)
		out = classify_amount_rate_residual(
			qty=135000.0,
			authoritative_amount=12104137419.0,
			valuation_rate=89660.277177778,
			currency="IRR",
			item_code="13100018",
			idx=1,
		)
		self.assertEqual(out["class"], "A")
		self.assertEqual(out["reason"], "amount_authoritative_legacy_uvr_float_rate")
		self.assertEqual(out["expected_valuation_rate"], 89660.0)
		self.assertEqual(out["residual"], 37419.0)
		self.assertLess(abs(out["residual"]), out["path_derived_bound"])

	def test_true_mismatch_stays_class_b(self):
		out = classify_amount_rate_residual(
			qty=15.0,
			authoritative_amount=510000000.0,
			valuation_rate=32584473.066666666,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")
		self.assertEqual(out["reason"], "non_integer_rate_under_irr_contract")

	def test_idempotent_second_classify(self):
		kwargs = dict(
			qty=15.0,
			authoritative_amount=488767096.0,
			valuation_rate=32584473.066666666,
			currency="IRR",
		)
		a = classify_amount_rate_residual(**kwargs)
		b = classify_amount_rate_residual(**kwargs)
		self.assertEqual(a["class"], b["class"])
		self.assertEqual(a["residual"], b["residual"])
		self.assertEqual(a["expected_valuation_rate"], b["expected_valuation_rate"])


class TestDocumentResidualsBothShapes(unittest.TestCase):
	def _row(self, **kw):
		defaults = dict(
			item_code="ITEM",
			idx=1,
			name="r1",
			qty=1,
			conversion_factor=1,
			stock_qty=None,
			base_net_amount=0,
			base_amount=0,
			amount=0,
			valuation_rate=0,
			base_rate=0,
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			amount_difference_with_purchase_invoice=0,
			rejected_qty=0,
			sales_incoming_rate=None,
			is_fixed_asset=0,
			department=None,
		)
		defaults.update(kw)
		row = mock.Mock(**defaults)
		row.get = lambda k, d=None: getattr(row, k, d)
		return row

	def _doc(self, items, stock_codes=None):
		doc = mock.Mock(
			company="C",
			doctype="Purchase Receipt",
			name="MAT-PRE-TEST",
			items=items,
			is_old_subcontracting_flow=0,
			department=None,
		)
		codes = stock_codes or [r.item_code for r in items]
		doc.get = lambda k, d=None: getattr(doc, k, d)
		doc.get_stock_items = lambda: list(codes)
		doc.get_asset_items = lambda: []
		return doc

	def test_pr_00780_shape_all_float_rows_class_a(self):
		"""IRR multi-row: float VR = base_net/qty; integer rows skip."""
		rows = [
			self._row(
				item_code="16800200",
				idx=1,
				name="r1",
				qty=15,
				base_net_amount=488767096,
				amount=488767096,
				valuation_rate=32584473.066666666,
			),
			self._row(
				item_code="16800203",
				idx=2,
				name="r2",
				qty=6,
				base_net_amount=557769509,
				amount=557769509,
				valuation_rate=92961584.83333333,
			),
			self._row(
				item_code="16702908",
				idx=5,
				name="r5",
				qty=3,
				base_net_amount=28074741,
				amount=28074741,
				valuation_rate=9358247.0,  # already integer — zero residual skip
			),
		]
		doc = self._doc(rows)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification._dimension_fieldnames",
				return_value=[],
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [])
		self.assertEqual(len(a), 2)
		self.assertTrue(all(r["reason"] == "amount_authoritative_legacy_uvr_float_rate" for r in a))

	def test_pr_00707_shape_lcv_class_a_ready(self):
		"""EUR receipt + LCV: auth = base_net + LCV; DECIMAL(30,9) float VR."""
		row = self._row(
			item_code="13100018",
			idx=1,
			name="r1",
			qty=135,
			conversion_factor=1000,
			stock_qty=135000,
			base_net_amount=4424625000,
			amount=2949.75,
			base_amount=4424625000,
			landed_cost_voucher_amount=7679512419,
			valuation_rate=89660.277177778,
			base_rate=32775000,
		)
		doc = self._doc([row])
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification._dimension_fieldnames",
				return_value=[],
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
				return_value={"account": "Round Off - E", "cost_center": "Main - E"},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.validate_round_off_configuration",
				return_value=None,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.resolve_round_off_dimensions",
				return_value={},
			),
		):
			a, b = classify_document_residuals(doc)
			decision = evaluate_irr_rate_rounding_residual(doc)
		self.assertEqual(b, [])
		self.assertEqual(len(a), 1)
		self.assertEqual(a[0]["residual"], 37419.0)
		self.assertNotEqual(decision.status, STATUS_CLASS_B)
		self.assertEqual(decision.status, STATUS_READY)


if __name__ == "__main__":
	unittest.main()
