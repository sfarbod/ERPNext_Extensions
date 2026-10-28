# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests: Class A/B classification, net gate, dimension resolution (3.8.7)."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	STATUS_BYPASS,
	STATUS_CLASS_B,
	STATUS_PARTNER,
	STATUS_READY,
	classify_amount_rate_residual,
	classify_document_residuals,
	evaluate_irr_rate_rounding_residual,
	purchase_receipt_stock_valuation_amount,
	purchase_receipt_valuation_stock_qty,
)


class TestClassifyAmountRateResidual(unittest.TestCase):
	def test_class_a_amount_authoritative_integer_rate(self):
		# amount=1371, qty=7, rate=ROUND(1371/7)=196 → residual -1
		out = classify_amount_rate_residual(
			qty=7, authoritative_amount=1371, valuation_rate=196, currency="IRR", item_code="X"
		)
		self.assertEqual(out["class"], "A")
		self.assertEqual(out["residual"], -1)

	def test_zero_residual_skip(self):
		out = classify_amount_rate_residual(
			qty=7, authoritative_amount=1372, valuation_rate=196, currency="IRR"
		)
		self.assertEqual(out["class"], "skip")

	def test_class_b_rate_zero_with_amount(self):
		out = classify_amount_rate_residual(
			qty=1, authoritative_amount=4050000000, valuation_rate=0, currency="IRR", item_code="230049"
		)
		self.assertEqual(out["class"], "B")
		self.assertEqual(out["reason"], "valuation_rate_le_zero_with_nonzero_amount")

	def test_class_b_non_integer_rate(self):
		out = classify_amount_rate_residual(
			qty=15,
			authoritative_amount=510000000,
			valuation_rate=32584473.066666666,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")
		self.assertEqual(out["reason"], "non_integer_rate_under_irr_contract")

	def test_class_b_auth_rate_mismatch(self):
		# integer rate but not ROUND(amount/qty)
		out = classify_amount_rate_residual(
			qty=3,
			authoritative_amount=2244000000,
			valuation_rate=733040000,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")
		self.assertEqual(out["reason"], "amount_rate_mismatch_not_reproducible_by_approved_pipeline")


class TestEvaluateNetGate(unittest.TestCase):
	def test_non_irr_bypass(self):
		doc = mock.Mock(company="X", doctype="Purchase Receipt", name="PR-1")
		doc.get = lambda k, d=None: getattr(doc, k, d)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
			return_value=False,
		):
			d = evaluate_irr_rate_rounding_residual(doc)
		self.assertEqual(d.status, STATUS_BYPASS)

	def test_manufacture_excluded_bypass(self):
		doc = mock.Mock(company="C", doctype="Stock Entry", purpose="Manufacture", name="SE-1")
		doc.get = lambda k, d=None: getattr(doc, k, d)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
			return_value=True,
		):
			d = evaluate_irr_rate_rounding_residual(doc)
		self.assertEqual(d.status, STATUS_BYPASS)
		self.assertIn("manufacture_repack", d.messages[0])

	def test_class_b_pr_shape_fails_before_round_off_lookup(self):
		row = mock.Mock(
			qty=1,
			base_amount=4050000000,
			base_net_amount=4050000000,
			amount=4050000000,
			valuation_rate=0,
			base_rate=4050000000,
			item_code="230049",
			idx=2,
			name="r1",
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			amount_difference_with_purchase_invoice=0,
			conversion_factor=1,
			department="Dept-A",
		)
		row.get = lambda k, d=None: getattr(row, k, d)
		doc = mock.Mock(
			company="C",
			doctype="Purchase Receipt",
			name="MAT-PRE-TEST",
			items=[row],
			department=None,
			is_old_subcontracting_flow=0,
		)
		doc.get = lambda k, d=None: getattr(doc, k, d)
		doc.get_stock_items = lambda: [row.item_code]
		doc.get_asset_items = lambda: []

		resolve_calls = []

		def resolve(company, require=True):
			resolve_calls.append(company)
			return {"account": "RO", "cost_center": "CC"}

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
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
				side_effect=resolve,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification._dimension_fieldnames",
				return_value=["department"],
			),
		):
			d = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)

		self.assertEqual(d.status, STATUS_CLASS_B)
		self.assertEqual(resolve_calls, [], "Round Off Account must not be resolved on Class B")

	def test_net_zero_bypass_no_account_lookup(self):
		row = mock.Mock(
			qty=7,
			base_amount=1372,
			base_net_amount=1372,
			amount=1372,
			valuation_rate=196,
			base_rate=196,
			item_code="X",
			idx=1,
			name="r1",
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			amount_difference_with_purchase_invoice=0,
			conversion_factor=1,
			department="D",
		)
		row.get = lambda k, d=None: getattr(row, k, d)
		doc = mock.Mock(
			company="C",
			doctype="Purchase Receipt",
			name="PR-OK",
			items=[row],
			department="D",
			is_old_subcontracting_flow=0,
		)
		doc.get = lambda k, d=None: getattr(doc, k, d)
		doc.get_stock_items = lambda: [row.item_code]
		doc.get_asset_items = lambda: []
		resolve_calls = []

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
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
				side_effect=lambda *a, **k: resolve_calls.append(1) or {"account": "RO", "cost_center": "CC"},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification._dimension_fieldnames",
				return_value=["department"],
			),
		):
			d = evaluate_irr_rate_rounding_residual(doc)
		self.assertEqual(d.status, STATUS_BYPASS)
		self.assertEqual(resolve_calls, [])

	def test_no_partner_fails_closed(self):
		class _R:
			def __init__(self, **kw):
				self.__dict__.update(kw)

			def get(self, k, default=None):
				return self.__dict__.get(k, default)

		class _D:
			def __init__(self, **kw):
				self.__dict__.update(kw)

			def get(self, k, default=None):
				return self.__dict__.get(k, default)

			def get_debit_field_precision(self):
				return 0

		row = _R(
			qty=7,
			transfer_qty=7,
			amount=1371,
			valuation_rate=196,
			basic_rate=196,
			basic_amount=1372,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			item_code="X",
			idx=1,
			name="r1",
			t_warehouse="WH-T",
			s_warehouse=None,
			department="D",
		)
		doc = _D(
			company="C",
			doctype="Stock Entry",
			purpose="Material Receipt",
			name="SE-1",
			items=[row],
			department="D",
			additional_costs=[],
		)
		gl = [
			{"account": "Stock - T", "debit": 1371, "credit": 0, "company": "C"},
		]

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
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
				return_value={"account": "RO - T", "cost_center": "CC-RO"},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.validate_round_off_configuration",
				return_value=None,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_stock_adjustment_residual",
				return_value={"account": "SA - T", "cost_center": "CC-RO"},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.validate_stock_adjustment_residual_configuration",
				return_value=None,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.resolve_round_off_dimensions",
				return_value={"department": "D"},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification._dimension_fieldnames",
				return_value=["department"],
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.zero_value_transfer._should_force_balanced_transfer_gl",
				return_value=False,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual._is_stock_account",
				side_effect=lambda a: a == "Stock - T",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual._is_stock_adjustment_account",
				return_value=False,
			),
			mock.patch(
				"frappe.get_cached_value",
				side_effect=lambda *a, **k: (
					"RO - T" if a[-1] == "round_off_account" else None
				),
			),
		):
			d = evaluate_irr_rate_rounding_residual(doc, gl_entries=gl)

		self.assertEqual(d.status, STATUS_PARTNER)
		self.assertTrue(d.class_a_rows)
		self.assertEqual(d.net_signed_debit, 1.0)


class TestPurchaseReceiptStockValuationAuth(unittest.TestCase):
	"""3.8.7: PR classifier follows ERPNext update_valuation_rate numerator + stock qty."""

	@staticmethod
	def _row(**kw):
		defaults = {
			"qty": 10,
			"conversion_factor": 1,
			"base_amount": 117000000,
			"amount": 117000000,
			"base_net_amount": 117000000,
			"item_tax_amount": 0,
			"landed_cost_voucher_amount": 0,
			"amount_difference_with_purchase_invoice": 0,
			"sales_incoming_rate": None,
			"rejected_qty": 0,
			"rm_supp_cost": 0,
			"valuation_rate": 11700000,
			"base_rate": 11700000,
			"item_code": "ITEM",
			"idx": 1,
			"name": "r1",
			"department": None,
		}
		defaults.update(kw)

		class _R:
			def __init__(self, data):
				self.__dict__.update(data)

			def get(self, k, default=None):
				return self.__dict__.get(k, default)

		return _R(defaults)

	@staticmethod
	def _doc(row, **kw):
		items = kw.pop("items", None)
		stock_item_codes = kw.pop("stock_item_codes", None)
		asset_item_codes = kw.pop("asset_item_codes", None)
		if items is None:
			items = [row] if row is not None else []
		data = {
			"company": "C",
			"doctype": "Purchase Receipt",
			"name": "PR-1",
			"items": items,
			"is_old_subcontracting_flow": 0,
		}
		data.update(kw)

		class _D:
			def __init__(self, d):
				self.__dict__.update(d)
				# Default: all item codes are stock-eligible (mirrors prior test contract).
				# Override via stock_item_codes / asset_item_codes for eligibility tests.
				if stock_item_codes is not None:
					self._stock_codes = list(stock_item_codes)
				else:
					self._stock_codes = [
						r.get("item_code") for r in self.items if r.get("item_code")
					]
				self._asset_codes = list(asset_item_codes or [])

			def get(self, k, default=None):
				return self.__dict__.get(k, default)

			def get_stock_items(self):
				return list(self._stock_codes)

			def get_asset_items(self):
				return list(self._asset_codes)

		return _D(data)

	def test_helpers_mirror_erpnext_numerator_and_stock_qty(self):
		row = self._row(
			qty=2,
			conversion_factor=10,
			base_net_amount=2000000,
			item_tax_amount=100000,
			landed_cost_voucher_amount=50000,
			amount_difference_with_purchase_invoice=25,
		)
		self.assertEqual(purchase_receipt_valuation_stock_qty(row), 20.0)
		self.assertEqual(purchase_receipt_stock_valuation_amount(row), 2150025.0)

	def test_excluded_vat_total_only_not_class_b(self):
		# Gross amount may equal net; VR = net/qty. Category Total tax not in item_tax_amount.
		row = self._row(
			base_amount=117000000,
			amount=117000000,
			base_net_amount=117000000,
			item_tax_amount=0,
			valuation_rate=11700000,
		)
		doc = self._doc(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(a, [])
		self.assertEqual(b, [])

	def test_included_vat_uses_base_net_not_gross_amount(self):
		# Production-shaped: amount 117M inclusive, base_net 106470000, VR 10647000.
		row = self._row(
			base_amount=117000000,
			amount=117000000,
			base_net_amount=106470000,
			item_tax_amount=0,
			valuation_rate=10647000,
		)
		doc = self._doc(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [], "valid inclusive VAT must not be Class B")
		self.assertEqual(a, [])

	def test_valuation_tax_included_in_auth(self):
		row = self._row(
			base_amount=117000000,
			base_net_amount=117000000,
			item_tax_amount=11700000,
			valuation_rate=12870000,
		)
		doc = self._doc(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [])
		self.assertEqual(a, [])

	def test_landed_cost_included_in_auth(self):
		row = self._row(
			base_amount=10000000,
			base_net_amount=10000000,
			landed_cost_voucher_amount=500000,
			valuation_rate=1050000,
		)
		doc = self._doc(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [])
		self.assertEqual(a, [])

	def test_conversion_factor_uses_stock_qty(self):
		# qty=2, CF=10 → stock qty 20; base_net 2000000 → VR 100000
		row = self._row(
			qty=2,
			conversion_factor=10,
			base_amount=2000000,
			base_net_amount=2000000,
			amount=2000000,
			valuation_rate=100000,
			base_rate=1000000,
		)
		doc = self._doc(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [])
		self.assertEqual(a, [])

	def test_true_inconsistency_still_class_b(self):
		# Stock numerator 10M but VR implies 12M — still Class B.
		row = self._row(
			base_net_amount=10000000,
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			valuation_rate=1200000,
		)
		doc = self._doc(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(a, [])
		self.assertEqual(len(b), 1)
		self.assertEqual(b[0]["reason"], "amount_rate_mismatch_not_reproducible_by_approved_pipeline")

	def test_non_stock_service_row_skipped(self):
		row = self._row(
			item_code="SVC-ETO",
			qty=1922,
			base_net_amount=235996614,
			amount=235996614,
			base_amount=235996614,
			valuation_rate=0,
			base_rate=122787,
			rate=122787,
		)
		doc = self._doc(row, stock_item_codes=[], asset_item_codes=[])
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(a, [])
		self.assertEqual(b, [])

	def test_subcontracting_auto_pr_service_shape_not_class_b(self):
		"""Production MAT-PRE-2026-01123 shape must not raise valuation_rate_le_zero."""
		from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
			evaluate_irr_rate_rounding_residual,
		)

		row = self._row(
			item_code="23000001",
			qty=1922,
			base_net_amount=235996614,
			amount=235996614,
			base_amount=235996614,
			valuation_rate=0,
			base_rate=122787,
			rate=122787,
		)
		doc = self._doc(
			row,
			name="MAT-PRE-2026-01123",
			is_subcontracted=1,
			is_old_subcontracting_flow=0,
			stock_item_codes=[],
			asset_item_codes=[],
		)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
			decision = evaluate_irr_rate_rounding_residual(doc)
		self.assertEqual(b, [])
		self.assertEqual(a, [])
		self.assertFalse(decision.class_b_rows)
		self.assertNotEqual(decision.status, "class_b_error")

	def test_mixed_pr_service_skipped_stock_classified(self):
		svc = self._row(
			item_code="SVC",
			idx=1,
			name="r-svc",
			qty=1922,
			base_net_amount=235996614,
			amount=235996614,
			valuation_rate=0,
		)
		# Stock Class A: auth 10_000_001, qty 10 → integer VR 1_000_000 → residual 1
		stock = self._row(
			item_code="STOCK",
			idx=2,
			name="r-stock",
			qty=10,
			base_net_amount=10000001,
			amount=10000001,
			base_amount=10000001,
			valuation_rate=1000000,
			base_rate=1000000,
		)
		doc = self._doc(
			None,
			items=[svc, stock],
			stock_item_codes=["STOCK"],
			asset_item_codes=[],
		)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		# Domestic IRR PR: integer Class A uses standalone SA (inverted debit).
		self.assertEqual(b, [])
		self.assertEqual(len(a), 1)
		self.assertEqual(a[0]["class"], "A")
		self.assertTrue(a[0].get("pr_integer_rate_standalone_sa"))
		self.assertEqual(a[0]["round_off_debit"], 1)  # residual +1 → preset SA debit sign invert

	def test_stock_pr_class_a_unchanged(self):
		row = self._row(
			qty=10,
			base_net_amount=10000001,
			amount=10000001,
			base_amount=10000001,
			valuation_rate=1000000,
		)
		doc = self._doc(row)
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
				"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [])
		self.assertEqual(len(a), 1)
		self.assertEqual(a[0]["class"], "A")
		self.assertEqual(a[0]["residual"], 1)
		self.assertTrue(a[0].get("pr_integer_rate_standalone_sa"))
		# residual +1 → inverted preset_debit = +1 (SA debit when inventory short)
		self.assertEqual(a[0]["round_off_debit"], 1)

	def test_stock_row_vr_zero_still_class_b(self):
		"""Genuinely stock-valued row with VR=0 must remain fail-closed Class B."""
		row = self._row(
			item_code="STOCK",
			qty=10,
			base_net_amount=10000000,
			amount=10000000,
			valuation_rate=0,
		)
		doc = self._doc(row, stock_item_codes=["STOCK"])
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(a, [])
		self.assertEqual(len(b), 1)
		self.assertEqual(b[0]["reason"], "valuation_rate_le_zero_with_nonzero_amount")

	def test_asset_row_remains_eligible(self):
		# Class A shape: auth 5_000_001 / qty 1 → integer VR 5_000_001 exact → skip;
		# use residual Class A: auth 10_000_001, VR 10_000_000.
		row = self._row(
			item_code="ASSET-1",
			is_fixed_asset=1,
			qty=10,
			base_net_amount=10000001,
			amount=10000001,
			base_amount=10000001,
			valuation_rate=1000000,
			base_rate=1000000,
		)
		doc = self._doc(row, stock_item_codes=[], asset_item_codes=["ASSET-1"])
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		# Asset rows remain eligible; domestic integer Class A is not remapped away.
		self.assertEqual(b, [])
		self.assertEqual(len(a), 1)
		self.assertEqual(a[0]["class"], "A")

	def test_old_subcontracting_stock_fg_still_classified(self):
		# Stock FG on old subcontract PR must not be blanket-skipped.
		row = self._row(
			item_code="FG-OLD",
			qty=10,
			base_net_amount=10000000,
			rm_supp_cost=500000,
			valuation_rate=1050000,
		)
		doc = self._doc(row, is_old_subcontracting_flow=1, is_subcontracted=1, stock_item_codes=["FG-OLD"])
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
				return_value="IRR",
			),
		):
			a, b = classify_document_residuals(doc)
		self.assertEqual(b, [])
		self.assertEqual(a, [])  # exact match auth/qty → skip residual


class TestRegionalValuationRateHook(unittest.TestCase):
	"""UVR regional extension must reuse align_purchase_receipt_item_amounts."""

	def test_regional_hook_integerizes_fractional_valuation_rate(self):
		from erpnext_extensions.iran_accounting.buying_selling import (
			update_regional_item_valuation_rate,
		)

		class _R:
			def __init__(self):
				self.qty = 10
				self.rate = 1000000
				self.base_rate = 1000000
				self.amount = 10000000
				self.base_amount = 10000000
				self.valuation_rate = 1000000.1

			def get(self, k, default=None):
				return getattr(self, k, default)

		class _D:
			doctype = "Purchase Receipt"
			company = "C"
			currency = "IRR"
			items = None

			def __init__(self, row):
				self.items = [row]

			def get(self, k, default=None):
				return getattr(self, k, default)

		row = _R()
		doc = _D(row)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				return_value="IRR",
			),
		):
			# domain.qty_rate_amount imports currency as rounding
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
				return_value="IRR",
			):
				update_regional_item_valuation_rate(doc)
		self.assertEqual(row.valuation_rate, 1000000.0)


if __name__ == "__main__":
	unittest.main()
