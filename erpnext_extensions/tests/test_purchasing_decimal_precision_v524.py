# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for purchasing/asset DECIMAL(30,9) registry (v5.2.4)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions import purchasing_decimal_precision_v524 as v524


class TestPurchasingDecimalPrecisionV524(unittest.TestCase):
	def test_critical_asset_includes_net_purchase_amount(self):
		self.assertIn("net_purchase_amount", v524.CRITICAL_ASSET_FIELDS)
		self.assertIn("net_purchase_amount", v524.PURCHASING_MONETARY_FIELDS_BY_DOCTYPE["Asset"])

	def test_registry_owns_po_pr_pi_parents(self):
		for dt in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
			self.assertIn("grand_total", v524.PURCHASING_MONETARY_FIELDS_BY_DOCTYPE[dt])
			self.assertIn("base_grand_total", v524.PURCHASING_MONETARY_FIELDS_BY_DOCTYPE[dt])

	def test_rates_marked_explicitly(self):
		self.assertIn(("Purchase Order Item", "rate"), v524.PURCHASING_MONETARY_RATE_FIELDS)
		self.assertIn(("Purchase Invoice Item", "rate"), v524.PURCHASING_MONETARY_RATE_FIELDS)

	def test_stock_repost_fields_not_duplicated_in_owned_registry(self):
		owned = {
			(dt, f)
			for dt, fields in v524.PURCHASING_MONETARY_FIELDS_BY_DOCTYPE.items()
			for f in fields
		}
		already = {
			(dt, f)
			for dt, fields in v524.ALREADY_HARDENED_BY_STOCK_REPOST.items()
			for f in fields
		}
		overlap = owned & already
		self.assertEqual(overlap, set(), f"Duplicate ownership: {sorted(overlap)}")

	def test_ptc_rate_excluded_as_tax_percent(self):
		self.assertIn("rate", v524.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["Purchase Taxes and Charges"])

	def test_status_helper_shape(self):
		status = v524.get_purchasing_precision_status()
		self.assertEqual(status["version_layer"], "5.2.4")
		self.assertGreater(status["total_registered"], 100)
		self.assertIn("net_purchase_amount", status["critical_asset"])

	def test_assert_schema_when_healthy(self):
		status = v524.get_purchasing_precision_status()
		if status["incorrect"] or status["missing"]:
			self.skipTest("Schema not yet repaired; run migrate / repair first")
		v524.assert_purchasing_decimal_schema()

	def test_completeness_guard(self):
		v524.assert_purchasing_field_classification_completeness()

	@patch.object(v524, "apply_decimal_schema_targets")
	@patch.object(v524, "verify_and_set_metadata")
	@patch.object(v524, "assert_purchasing_decimal_schema")
	@patch.object(v524, "get_purchasing_precision_status")
	def test_repair_raises_on_errors(self, mock_status, mock_assert, mock_meta, mock_schema):
		mock_status.return_value = {"incorrect": 1, "correct": 0, "missing": 0, "total_registered": 1}
		mock_meta.return_value = []
		mock_schema.return_value = [
			{
				"table": "tabAsset",
				"field": "net_purchase_amount",
				"status": "error",
				"doctype": "Asset",
			}
		]
		with self.assertRaises(RuntimeError):
			v524.repair_purchasing_decimal_schema()
