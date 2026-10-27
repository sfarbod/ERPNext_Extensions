# Copyright (c) 2026, ERPNext Extensions contributors

"""Unit tests for v5.6.25 purchase valuation HRC repair assessment."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair import (
	assess_repair,
)


class TestPurchaseValuationRepairAssessV5625(unittest.TestCase):
	def test_missing_pr_blocked(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair._bootstrap"
		), patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair.frappe"
		) as mf:
			mf.db.exists.return_value = False
			out = assess_repair("PR-MISSING")
		self.assertFalse(out["eligible"])
		self.assertEqual(out["blocked_reason"], "purchase_receipt_not_found")

	def test_riv_alone_false_when_vr_lags_expected(self):
		fp = {
			"purchase_receipt": "PR-1",
			"items": [
				{
					"item_row": "r1",
					"item_code": "I",
					"warehouse": "W",
					"expected": 26_900_332_000.0,
					"sle_svd": 1_000_000_000.0,
					"difference": -25_900_332_000.0,
					"valuation_rate": 1_000_000.0,
					"pi_diff": 25_900_332_000.0,
					"lcv": 0.0,
				}
			],
			"at": "t",
		}
		finding = {
			"voucher_no": "PR-1",
			"root_cause_category": "A_INVOICE_BASED_VALUATION",
		}
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair._bootstrap"
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair.frappe"
			) as mf,
			patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair.scan_purchase_receipt_valuation",
				return_value=[finding],
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair._fingerprint_pr",
				return_value=fp,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair._related_vouchers",
				return_value=["PR-1", "PI-1"],
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair._grni_net",
				return_value=25_900_332_000.0,
			),
		):
			mf.db.exists.return_value = True
			mf.db.get_value.side_effect = lambda *a, **k: (
				"CO" if a and a[0] == "Purchase Receipt" else 1000.0
			)
			out = assess_repair("PR-1", company="CO")
		self.assertTrue(out["eligible"])
		self.assertFalse(out["riv_alone_sufficient"])
		self.assertEqual(out["recommended_action"], "adjust_incoming_rate_then_riv")


if __name__ == "__main__":
	unittest.main()
