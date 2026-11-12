# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Costing scope = Item Primary UOMs; Batch×Pool primary; Applied Share formula."""

from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	allocate_pools_to_batches,
	build_batches,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import (
	ProductBucket,
	configured_item_codes,
	fetch_eligible_fg_rows,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import ResolvedPeriod
from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import apply_row_factors, resolve_conversion


class TestConfiguredItemEligibility(unittest.TestCase):
	def test_only_configured_items_participate(self):
		period = ResolvedPeriod(
			company="C",
			persian_year=1405,
			persian_month=1,
			persian_month_name="فروردین",
			period_key="j01_1405",
			start_date="2026-03-21",
			end_date="2026-04-20",
			calendar_system="jalali",
		)
		candidates = [
			SimpleNamespace(
				stock_entry="SE-1",
				stock_entry_detail="SED-1",
				work_order="WO-1",
				posting_date="2026-03-25",
				item_code="INJ-1",
				item_name="Injectable",
				item_group="تزریقی",
				batch_no="B1",
				qty=100,
				stock_uom="Nos",
				uom="Nos",
			),
			SimpleNamespace(
				stock_entry="SE-1",
				stock_entry_detail="SED-2",
				work_order="WO-2",
				posting_date="2026-03-25",
				item_code="ORAL-1",
				item_name="Oral",
				item_group="خوراکی",
				batch_no="B2",
				qty=50,
				stock_uom="Nos",
				uom="Nos",
			),
			SimpleNamespace(
				stock_entry="SE-2",
				stock_entry_detail="SED-3",
				work_order="WO-3",
				posting_date="2026-03-26",
				item_code="OTHER-1",
				item_name="Other",
				item_group="سایر",
				batch_no="B3",
				qty=10,
				stock_uom="Nos",
				uom="Nos",
			),
		]
		# User configured injectable + oral (not OTHER)
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract.configured_item_codes",
			return_value={"INJ-1", "ORAL-1"},
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract._fetch_manufacture_fg_candidates",
			return_value=candidates,
		):
			eligible, excluded, summary = fetch_eligible_fg_rows("C", period, settings_name="S")

		self.assertEqual({r.item_code for r in eligible}, {"INJ-1", "ORAL-1"})
		self.assertEqual(len(excluded), 1)
		self.assertEqual(excluded[0].item_code, "OTHER-1")
		self.assertEqual(summary["configured_item_count"], 2)

	def test_empty_config_excludes_all(self):
		period = ResolvedPeriod(
			company="C",
			persian_year=1405,
			persian_month=1,
			persian_month_name="فروردین",
			period_key="j01_1405",
			start_date="2026-03-21",
			end_date="2026-04-20",
			calendar_system="jalali",
		)
		candidates = [
			SimpleNamespace(
				stock_entry="SE-1",
				stock_entry_detail="SED-1",
				work_order="WO-1",
				posting_date="2026-03-25",
				item_code="INJ-1",
				item_name="Injectable",
				item_group="تزریقی",
				batch_no="B1",
				qty=100,
				stock_uom="Nos",
				uom="Nos",
			),
		]
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract.configured_item_codes",
			return_value=set(),
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract._fetch_manufacture_fg_candidates",
			return_value=candidates,
		):
			eligible, excluded, summary = fetch_eligible_fg_rows("C", period)
		self.assertEqual(eligible, [])
		self.assertEqual(len(excluded), 1)
		self.assertEqual(summary["configured_item_count"], 0)


class TestBatchPoolPrimaryAllocation(unittest.TestCase):
	def test_allocated_amount_equals_pool_times_applied_share(self):
		products = {
			("B1", "A"): ProductBucket(
				batch_no="B1",
				item_code="A",
				item_name="A",
				actual_qty=Decimal("50"),
				uom="Nos",
				normalization_factor=Decimal("1"),
				normalized_qty=Decimal("50"),
			),
			("B2", "A"): ProductBucket(
				batch_no="B2",
				item_code="A",
				item_name="A",
				actual_qty=Decimal("50"),
				uom="Nos",
				normalization_factor=Decimal("1"),
				normalized_qty=Decimal("50"),
			),
		}
		batches, month_norm, variance = build_batches(products, share_precision=8)
		self.assertEqual(month_norm, Decimal("100"))
		self.assertEqual(abs(variance), Decimal("0"))
		# Same item in two batches keeps separate batch shares
		self.assertEqual(len(batches), 2)
		self.assertEqual({b.batch_no for b in batches}, {"B1", "B2"})

		pools = [
			ExpensePoolSnapshot(
				settings_rule="r1",
				account="6110",
				cost_center="",
				department="",
				monthly_net_expense=Decimal("1000"),
				monthly_debit=Decimal("1000"),
				monthly_credit=Decimal("0"),
				gl_entry_count=1,
				lines=[],
			)
		]
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.allocate.get_currency_precision",
			return_value=0,
		):
			bx = allocate_pools_to_batches(pools, batches, currency="IRR")
		self.assertEqual(len(bx), 2)
		for row in bx:
			self.assertEqual(row.calculated_batch_share, row.applied_batch_share)
			expected = (row.pool_monthly_net_expense * row.applied_batch_share).quantize(Decimal("1"))
			# residual may move 1 unit to largest batch
			self.assertTrue(abs(row.allocated_amount - expected) <= Decimal("1"))

	def test_manual_override_preserves_calculated_share(self):
		products = {
			("B1", "A"): ProductBucket(
				batch_no="B1",
				item_code="A",
				item_name="A",
				actual_qty=Decimal("75"),
				uom="Nos",
				normalization_factor=Decimal("1"),
				normalized_qty=Decimal("75"),
			),
			("B2", "A"): ProductBucket(
				batch_no="B2",
				item_code="A",
				item_name="A",
				actual_qty=Decimal("25"),
				uom="Nos",
				normalization_factor=Decimal("1"),
				normalized_qty=Decimal("25"),
			),
		}
		batches, _, variance = build_batches(
			products, share_precision=8, overrides={"B1": Decimal("0.80")}
		)
		b1 = next(b for b in batches if b.batch_no == "B1")
		b2 = next(b for b in batches if b.batch_no == "B2")
		self.assertEqual(b1.calculated_share, Decimal("0.75000000"))
		self.assertEqual(b1.applied_share, Decimal("0.80000000"))
		self.assertTrue(b1.is_manual_override)
		self.assertEqual(b2.applied_share, b2.calculated_share)
		# Variance from 100% — incomplete; no silent redistribution
		self.assertNotEqual(variance, Decimal("0"))


class TestUomManualOverride(unittest.TestCase):
	def test_manual_override_wins_without_touching_item_master(self):
		row = SimpleNamespace(
			item="ITEM-1",
			primary_uom="سرنگ",
			manual_override_factor=2.5,
			override_reason="costing audit",
			conversion_factor=0,
			conversion_status="Missing",
			conversion_source="Missing",
			remarks="",
			source_conversion_factor=0,
			stock_uom="Nos",
		)
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.resolve_conversion",
			return_value=(None, "Missing", "No UOM Conversion Detail"),
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="Nos",
		):
			apply_row_factors(row)
		self.assertEqual(row.conversion_status, "Manual Override")
		self.assertEqual(row.conversion_factor, 2.5)
		self.assertEqual(row.conversion_source, "Manual Override")

	def test_zero_source_factor_is_invalid(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="Nos",
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.frappe.get_all",
			return_value=[{"name": "x", "conversion_factor": 0}],
		):
			factor, status, _ = resolve_conversion("ITEM", "Box")
		self.assertIsNone(factor)
		self.assertEqual(status, "Invalid")


class TestPersianPeriodFormat(unittest.TestCase):
	def test_yyyy_mm(self):
		self.assertEqual(f"{1405:04d}-{1:02d}", "1405-01")
		self.assertEqual(f"{1405:04d}-{6:02d}", "1405-06")


if __name__ == "__main__":
	unittest.main()
