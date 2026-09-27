# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.22 — Material Issue from proven legitimate zero lot is NO_ACTION."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import (
	NO_ACTION_REQUIRED,
	Z0_LEGITIMATE_ZERO,
	ZP_PROVEN_LEGITIMATE_ZERO,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row


class TestLegitimateZeroIssueV5322(unittest.TestCase):
	def test_material_issue_from_proven_zero_lot(self):
		row = {
			"purpose": "Material Issue",
			"item_code": "ITEM-Z",
			"qty": 1,
			"basic_rate": 0,
			"amount": 0,
			"s_warehouse": "WH",
			"parent": "STE-ISSUE",
			"name": "d1",
			"posting_date": "2026-06-21",
			"posting_time": "23:59:00",
			"company": "C",
		}
		prov = {
			"provenance": ZP_PROVEN_LEGITIMATE_ZERO,
			"allow_zero_outgoing": True,
			"current_stock_value": 0.0,
			"current_qty": 3.0,
			"reason": "DEPLETED_THEN_ZERO_INBOUND_ONLY",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_provenance.classify_zero_provenance",
			return_value=prov,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_scrap_reject_waste_warehouse",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.scrap_valuation_role",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._row_posting_datetime",
			return_value="2026-06-21 23:59:00",
		):
			out = classify_zero_row(row)
		self.assertEqual(out["zero_class"], Z0_LEGITIMATE_ZERO)
		self.assertEqual(out["status"], NO_ACTION_REQUIRED)
		self.assertIsNone(out.get("patient_zero"))
		self.assertEqual(out.get("source_of_truth"), "proven_legitimate_zero_lot")


if __name__ == "__main__":
	unittest.main()
