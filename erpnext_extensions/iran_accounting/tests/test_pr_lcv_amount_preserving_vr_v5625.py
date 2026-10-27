# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.25 — PR/LCV valuation_rate must preserve acquisition total (no premature integer VR)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	_align_purchase_valuation_rate_amount_preserving,
	align_purchase_receipt_item_amounts,
)
from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
	_protected_reclass_accounts,
)


def _pr_row(**kwargs):
	base = dict(
		name="row1",
		idx=1,
		item_code="ITEM-1",
		qty=800,
		rate=1_000_000,
		amount=800_000_000,
		base_rate=1_000_000,
		base_amount=800_000_000,
		base_net_amount=800_000_000,
		item_tax_amount=0,
		landed_cost_voucher_amount=1500,
		amount_difference_with_purchase_invoice=0,
		valuation_rate=1_000_001.875,
		conversion_factor=1,
		stock_qty=800,
		rejected_qty=0,
		sales_incoming_rate=None,
		is_fixed_asset=0,
		cost_center=None,
	)
	base.update(kwargs)
	row = SimpleNamespace(**base)
	row.get = lambda k, d=None: getattr(row, k, d)
	row.precision = lambda field: 6 if field == "stock_qty" else 2
	return row


class TestAmountPreservingValuationRate(unittest.TestCase):
	def test_lcv_1500_on_800_preserves_fractional_vr(self):
		row = _pr_row()
		doc = SimpleNamespace(doctype="Purchase Receipt", company="Co", is_old_subcontracting_flow=0)
		doc.get = lambda k, d=None: getattr(doc, k, d)
		_align_purchase_valuation_rate_amount_preserving(row, doc)
		self.assertAlmostEqual(flt(row.valuation_rate), 1_000_001.875, places=6)
		self.assertAlmostEqual(flt(row.valuation_rate) * 800, 800_001_500, places=2)

	def test_cases_table(self):
		cases = [
			(800, 1500),
			(3, 1),
			(7, 10),
			(100, 1),
			(1000, 999),
			(1000, 1001),
			(800, 1501),
			(800, 1499),
		]
		for qty, lcv in cases:
			with self.subTest(qty=qty, lcv=lcv):
				base = 1_000_000 * qty
				row = _pr_row(
					qty=qty,
					amount=base,
					base_amount=base,
					base_net_amount=base,
					landed_cost_voucher_amount=lcv,
					stock_qty=qty,
					valuation_rate=0,
				)
				doc = SimpleNamespace(doctype="Purchase Receipt", company="Co", is_old_subcontracting_flow=0)
				doc.get = lambda k, d=None: getattr(doc, k, d)
				_align_purchase_valuation_rate_amount_preserving(row, doc)
				expected_total = base + lcv
				self.assertAlmostEqual(
					flt(row.valuation_rate) * qty,
					expected_total,
					places=2,
					msg=f"qty={qty} lcv={lcv} vr={row.valuation_rate}",
				)

	def test_align_pr_does_not_integerize_vr(self):
		row = _pr_row()
		doc = SimpleNamespace(
			doctype="Purchase Receipt",
			company="IRR Co",
			currency="IRR",
			is_old_subcontracting_flow=0,
			items=[row],
		)
		doc.get = lambda k, d=None: getattr(doc, k, d) if k != "items" else doc.items
		with patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			# qty_rate_amount.rounding is the currency module
			align_purchase_receipt_item_amounts(doc)
		self.assertNotEqual(int(row.valuation_rate), row.valuation_rate)  # still fractional
		self.assertAlmostEqual(flt(row.valuation_rate), 1_000_001.875, places=6)

	def test_align_dn_still_integerizes_vr(self):
		row = _pr_row(landed_cost_voucher_amount=0, valuation_rate=1_000_001.875)
		doc = SimpleNamespace(
			doctype="Delivery Note",
			company="IRR Co",
			currency="IRR",
			items=[row],
		)
		doc.get = lambda k, d=None: getattr(doc, k, d) if k != "items" else doc.items
		with patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			align_purchase_receipt_item_amounts(doc)
		self.assertEqual(flt(row.valuation_rate), 1_000_002.0)

	def test_pi_amt_diff_included_in_vr(self):
		row = _pr_row(
			landed_cost_voucher_amount=1500,
			amount_difference_with_purchase_invoice=25_900_332_000,
			base_net_amount=1_000_000_000,
			qty=1000,
			stock_qty=1000,
			amount=1_000_000_000,
			base_amount=1_000_000_000,
		)
		doc = SimpleNamespace(doctype="Purchase Receipt", company="Co", is_old_subcontracting_flow=0)
		doc.get = lambda k, d=None: getattr(doc, k, d)
		_align_purchase_valuation_rate_amount_preserving(row, doc)
		expected = 1_000_000_000 + 1500 + 25_900_332_000
		self.assertAlmostEqual(flt(row.valuation_rate) * 1000, expected, places=2)


class TestAuthoritativeAcquisitionKeepsFractionalRate(unittest.TestCase):
	def test_lcv_fractional_document_rate_not_integerized(self):
		from erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic import (
			authoritative_acquisition_incoming_rate,
			align_authoritative_acquisition_movement,
		)

		sle = {
			"voucher_type": "Purchase Receipt",
			"voucher_no": "PR-TEST",
			"voucher_detail_no": "row1",
			"item_code": "ITEM",
			"actual_qty": 800,
			"qty_after_transaction": 800,
			"incoming_rate": 0,
			"stock_value": 0,
			"stock_value_difference": 0,
			"company": "Co",
		}
		rows = {
			("Purchase Receipt", "PR-TEST"): {"is_return": 0, "is_internal_supplier": 0},
			("Purchase Receipt Item", "row1"): {
				"item_code": "ITEM",
				"valuation_rate": 1_000_001.875,
			},
		}

		def _get_value(doctype, name, fieldname=None, **kwargs):
			row = rows.get((doctype, name))
			if not row:
				return None
			if isinstance(fieldname, (list, tuple)):
				return {field: row.get(field) for field in fieldname}
			return row.get(fieldname)

		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic.is_irr_company",
				return_value=True,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic.get_company_currency",
				return_value="IRR",
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic.frappe.db.get_value",
				side_effect=_get_value,
			),
		):
			rate = authoritative_acquisition_incoming_rate(sle, "IRR")
			self.assertEqual(rate, 1_000_001.875)
			self.assertTrue(align_authoritative_acquisition_movement(sle, "Co"))
		self.assertEqual(sle["incoming_rate"], 1_000_001.875)
		self.assertEqual(sle["stock_value_difference"], 800_001_500)


class TestGrniProtectedFromResidualPartner(unittest.TestCase):
	def test_pr_protects_grni_and_lcv_expense(self):
		doc = SimpleNamespace(doctype="Purchase Receipt", company="Co")

		class _Entry:
			expense_account = "LCV-CLEAR"

		def fake_lcv():
			return {("ITEM", "row"): [_Entry()]}

		doc.get_item_account_wise_lcv_entries = fake_lcv
		with patch("frappe.get_cached_value") as gcv:
			def _gcv(dt, name, field):
				if field == "stock_received_but_not_billed":
					return "GRNI-ACC"
				if field == "asset_received_but_not_billed":
					return "ARBNB"
				return None

			gcv.side_effect = _gcv
			protected = _protected_reclass_accounts(doc)
		self.assertIn("GRNI-ACC", protected)
		self.assertIn("ARBNB", protected)
		self.assertIn("LCV-CLEAR", protected)


if __name__ == "__main__":
	unittest.main()
