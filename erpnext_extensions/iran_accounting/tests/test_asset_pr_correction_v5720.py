# Copyright (c) 2026, ERPNext Extensions contributors
"""5.7.18 — Asset PR correction account resolution (no blanket Land→Computer)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.service import (
	resolve_target_account,
)


class TestAssetPrAccountResolutionV5720(unittest.TestCase):
	def test_uses_pr_item_category_when_not_land(self):
		with patch(
			"erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.service.fa_account_for_category",
			side_effect=lambda cat, company: {
				"تجهیزات کامپیوتری3 ساله مستقیم": "121013 - تجهیزات کامپیوتری - E",
			}.get(cat),
		):
			to_acct, source, blocked = resolve_target_account(
				company="Co",
				land_account="121000 - زمین - E",
				pr_item_asset_category="تجهیزات کامپیوتری3 ساله مستقیم",
				item_code="X",
				assets=[],
			)
		self.assertEqual(to_acct, "121013 - تجهیزات کامپیوتری - E")
		self.assertEqual(source, "pr_item_asset_category")
		self.assertIsNone(blocked)

	def test_falls_back_to_linked_asset_category_not_blanket_computer(self):
		def fa(cat, company):
			return {
				"NA": "121000 - زمین - E",
				"اثاثه  و منصوبات 5 ساله مستقیم": "121006 - اثاثیه ومنصوبات - E",
				"ماشین آلات و تجهیزات 10 ساله مستقیم": "121003 - ماشین آلات و تجهیزات تولید - E",
			}.get(cat)

		# Linked Asset categories resolve before Item fallback — no frappe.db needed.
		with patch(
			"erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.service.fa_account_for_category",
			side_effect=fa,
		):
			to_furniture, src_f, _ = resolve_target_account(
				company="Co",
				land_account="121000 - زمین - E",
				pr_item_asset_category="NA",
				item_code="A",
				assets=[
					{"name": "3736", "asset_category": "اثاثه  و منصوبات 5 ساله مستقیم"}
				],
			)
			to_machine, src_m, _ = resolve_target_account(
				company="Co",
				land_account="121000 - زمین - E",
				pr_item_asset_category="NA",
				item_code="B",
				assets=[
					{
						"name": "3750",
						"asset_category": "ماشین آلات و تجهیزات 10 ساله مستقیم",
					}
				],
			)
		self.assertEqual(to_furniture, "121006 - اثاثیه ومنصوبات - E")
		self.assertIn("3736", src_f)
		self.assertEqual(to_machine, "121003 - ماشین آلات و تجهیزات تولید - E")
		self.assertIn("3750", src_m)

	def test_genuine_land_blocked(self):
		with patch(
			"erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.service.fa_account_for_category",
			return_value="121000 - زمین - E",
		):
			to_acct, source, blocked = resolve_target_account(
				company="Co",
				land_account="121000 - زمین - E",
				pr_item_asset_category="زمین",
				item_code="LAND-1",
				assets=[],
			)
		self.assertIsNone(to_acct)
		self.assertIsNone(source)
		self.assertEqual(blocked, "genuine_land_category_no_change")

	def test_override_rejects_land_target(self):
		to_acct, source, blocked = resolve_target_account(
			company="Co",
			land_account="121000 - زمین - E",
			pr_item_asset_category="NA",
			item_code="X",
			assets=[],
			override="121000 - زمین - E",
		)
		self.assertIsNone(to_acct)
		self.assertEqual(blocked, "override_targets_land_forbidden")


class TestIntegerVrFxScopeV5720(unittest.TestCase):
	"""Commercial FX rates keep tx precision; only IRR company VR integerizes."""

	def test_align_skips_non_irr_company(self):
		from types import SimpleNamespace

		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
			align_purchase_receipt_item_amounts,
		)

		row = SimpleNamespace(
			qty=2,
			rate=12.3456,
			amount=24.6912,
			base_rate=12.3456,
			base_amount=24.6912,
			valuation_rate=12.3456,
			get=lambda k, d=None: getattr(row, k, d),
		)
		doc = SimpleNamespace(
			doctype="Purchase Receipt",
			company="USD Co",
			currency="USD",
			conversion_rate=1,
			items=[row],
			get=lambda k, d=None: getattr(doc, k, d),
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=False,
		):
			align_purchase_receipt_item_amounts(doc)
		self.assertEqual(row.valuation_rate, 12.3456)

	def test_fx_commercial_rate_uses_tx_currency_not_forced_integer(self):
		from types import SimpleNamespace
		from unittest.mock import MagicMock

		from erpnext_extensions.iran_accounting.domain import qty_rate_amount as qra

		row = SimpleNamespace(
			qty=1,
			rate=99.99,
			amount=99.99,
			base_rate=4_200_000,
			base_amount=4_200_000,
			valuation_rate=4_200_000.4,
			conversion_factor=1,
			stock_qty=1,
			rejected_qty=0,
			item_tax_amount=0,
			landed_cost_voucher_amount=0,
			amount_difference_with_purchase_invoice=0,
			sales_incoming_rate=None,
			is_fixed_asset=0,
			get=lambda k, d=None: getattr(row, k, d),
			precision=lambda field: 6 if field == "stock_qty" else 2,
		)
		doc = SimpleNamespace(
			doctype="Purchase Receipt",
			company="IRR Co",
			currency="USD",
			conversion_rate=42000,
			is_old_subcontracting_flow=0,
			items=[row],
			get=lambda k, d=None: getattr(doc, k, d),
		)

		with patch.object(qra.rounding, "is_irr_company", return_value=True), patch.object(
			qra.rounding, "get_company_currency", return_value="IRR"
		), patch.object(
			qra.rounding, "round_monetary_rate", side_effect=lambda v, c: round(float(v), 2) if c == "USD" else float(int(round(float(v))))
		), patch.object(
			qra.rounding,
			"round_row_amount",
			side_effect=lambda q, r, c: float(q) * float(r),
		), patch.object(
			qra.rounding,
			"integer_valuation_rate_from_amount",
			side_effect=lambda a, q, c: float(int(round(float(a) / float(q)))),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.purchase_receipt_valuation_stock_qty",
			return_value=1,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.purchase_receipt_stock_valuation_amount",
			return_value=4_200_000.4,
		):
			qra.align_purchase_receipt_item_amounts(doc)

		# USD commercial rate keeps 2-dp FX precision path (not IRR integer 100).
		self.assertEqual(row.rate, 99.99)
		# Company-currency VR integerized.
		self.assertEqual(row.valuation_rate, 4_200_000.0)


if __name__ == "__main__":
	unittest.main()
