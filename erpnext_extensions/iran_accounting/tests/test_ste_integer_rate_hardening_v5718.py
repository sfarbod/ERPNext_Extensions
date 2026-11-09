# Copyright (c) 2026, ERPNext Extensions contributors
"""5.7.18 — STE integer IRR rate hardening (SVD/qty, native write, sync fail-safe)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	integer_valuation_rate_from_amount,
	round_monetary_rate,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	enforce_irr_sle_unit_rates_integer,
)


class TestSteIntegerRateHardeningV5718(unittest.TestCase):
	def test_po_job_shape_svd_div_qty_integerizes(self):
		# Integer SVD, float IR = SVD/qty without HALF_UP (PO-JOB07515 shape).
		svd = 4_400_547_964.0
		qty = 3000.0
		raw = svd / qty
		self.assertAlmostEqual(raw, 1_466_849.321333333, places=6)
		ir = float(integer_valuation_rate_from_amount(svd, qty, "IRR"))
		self.assertEqual(ir, 1_466_849.0)
		residual = svd - qty * ir
		self.assertEqual(residual, 964.0)

	def test_enforce_irr_sle_unit_rates_integer_preserves_svd(self):
		sle = SimpleNamespace(
			actual_qty=3000.0,
			incoming_rate=1_466_849.321333333,
			outgoing_rate=0.0,
			valuation_rate=1_466_849.321333333,
			stock_value_difference=4_400_547_964.0,
		)
		enforce_irr_sle_unit_rates_integer(sle, "IRR")
		self.assertEqual(sle.incoming_rate, 1_466_849.0)
		self.assertEqual(sle.valuation_rate, 1_466_849.0)
		self.assertEqual(sle.stock_value_difference, 4_400_547_964.0)

	def test_transfer_mfg_fg_expected_integer_from_amount(self):
		# Manufacture FG absorb example.
		amount = 2_040_161_462.72
		qty = 300.0
		ir = float(integer_valuation_rate_from_amount(amount, qty, "IRR"))
		self.assertEqual(ir, 6_800_538.0)
		self.assertAlmostEqual(amount - qty * ir, 62.72, places=2)

	def test_repack_expected_integer(self):
		amount = 1_087_591_494.88
		qty = 659.0
		ir = float(integer_valuation_rate_from_amount(amount, qty, "IRR"))
		self.assertEqual(ir, 1_650_366.0)

	def test_non_irr_align_stock_entry_skips_company(self):
		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
			align_stock_entry_item_amounts,
		)

		row = SimpleNamespace(
			basic_rate=12.3456,
			valuation_rate=12.3456,
			basic_amount=12.3456,
			amount=12.3456,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			transfer_qty=1,
			qty=1,
			t_warehouse="T",
			s_warehouse=None,
			get=lambda k, d=None: getattr(row, k, d),
		)
		doc = SimpleNamespace(company="USD Co", items=[row], get=lambda k, d=None: getattr(doc, k, d))
		with patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=False,
		):
			align_stock_entry_item_amounts(doc)
		self.assertEqual(row.valuation_rate, 12.3456)

	def test_manufacture_native_preview_integerizes_under_irr(self):
		from erpnext_extensions.iran_accounting.historical_stock import manufacture_native as mn

		fg = {
			"expected_rate": 6_800_538.209066666,
			"qty": 300.0,
			"fg_additional_cost": 0.0,
			"current_basic_rate": 6_800_538.209066666,
			"current_sle_rate": 6_800_538.209066666,
			"item": "20100096",
			"warehouse": "WH",
			"voucher_detail": "row1",
			"sle": "sle1",
		}
		ev = {
			"eligible": True,
			"classification": mn.EXACT,
			"se_matches_residual": False,
			"fg_rows": [fg],
			"reason": "test",
		}
		with patch.object(mn, "reconstruct_manufacture_valuation", return_value=ev), patch(
			"erpnext_extensions.iran_accounting.historical_stock.manufacture_native.frappe"
		) as mock_frappe:
			mock_frappe.db.get_value.return_value = "اسپاد فارمد دارو"
			with patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			), patch(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				return_value="IRR",
			):
				out = mn.repair_manufacture_valuation("MAT-STE-TEST", dry_run=True)
		self.assertTrue(out.get("ok"))
		self.assertTrue(out.get("dry_run"))
		self.assertEqual(out["preview"]["write_basic_rate"], 6_800_538.0)
		self.assertEqual(out["preview"]["write_sle_rate"], 6_800_538.0)
		# amount ROUND_HALF_UP(300×6800538.209066666)=2040161463; residual vs int rate.
		self.assertEqual(out["preview"]["write_sle_amount"], 2_040_161_463.0)
		self.assertEqual(out["preview"]["class_a_residual"], 63.0)

	def test_asset_cohort_rejects_outsider(self):
		from erpnext_extensions.iran_accounting.financial_correction_campaign.cohort import (
			CampaignError,
			reject_if_outside_asset_cohort,
		)

		with self.assertRaises(CampaignError):
			reject_if_outside_asset_cohort(["MAT-PRE-2026-01907", "MAT-PRE-OUTSIDER"])

	def test_denylist_contains_finance_je(self):
		from erpnext_extensions.iran_accounting.financial_correction_campaign import (
			DENYLIST_JOURNAL_ENTRIES,
		)

		self.assertIn("ACC-JV-1405-169046", DENYLIST_JOURNAL_ENTRIES)


if __name__ == "__main__":
	unittest.main()
