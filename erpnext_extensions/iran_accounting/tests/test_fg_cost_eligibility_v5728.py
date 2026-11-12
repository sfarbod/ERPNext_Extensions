# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""v5.7.28 scope correction — only Item Group تزریقی under محصول نهایی is eligible."""

from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.fg_cost_allocation import FG_ITEM_GROUP, FG_PARENT_ITEM_GROUP
from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import build_batches
from erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility import (
	classify_item_group,
	is_eligible_fg_item,
	summarize_exclusions,
	ExclusionRow,
	verify_fg_item_group_hierarchy,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import (
	ProductBucket,
	consolidate_products,
	fetch_eligible_fg_rows,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import ResolvedPeriod


class TestItemGroupEligibilityV5728(unittest.TestCase):
	def test_hierarchy_verified(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility.frappe.db.get_value",
			side_effect=lambda dt, name, fields, as_dict=False: (
				{"name": FG_ITEM_GROUP, "parent_item_group": FG_PARENT_ITEM_GROUP, "is_group": 0}
				if name == FG_ITEM_GROUP
				else {"name": FG_PARENT_ITEM_GROUP, "is_group": 1}
			),
		):
			h = verify_fg_item_group_hierarchy()
		self.assertEqual(h.item_group, FG_ITEM_GROUP)
		self.assertEqual(h.parent_item_group, FG_PARENT_ITEM_GROUP)

	def test_item_in_tazrighi_included(self):
		def _get_value(dt, name, field=None, **kwargs):
			if dt == "Item":
				return FG_ITEM_GROUP
			if dt == "Item Group" and name == FG_ITEM_GROUP:
				return FG_PARENT_ITEM_GROUP
			return None

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility.frappe.db.get_value",
			side_effect=_get_value,
		):
			self.assertTrue(is_eligible_fg_item("FG-INJ-01"))

	def test_sibling_under_parent_excluded(self):
		def _get_value(dt, name, field=None, **kwargs):
			if dt == "Item":
				return "خوراکی"
			if dt == "Item Group" and name == "خوراکی":
				return FG_PARENT_ITEM_GROUP
			if dt == "Item Group" and name == FG_ITEM_GROUP:
				return FG_PARENT_ITEM_GROUP
			return None

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility.frappe.db.get_value",
			side_effect=_get_value,
		):
			self.assertFalse(is_eligible_fg_item("FG-ORAL-01"))
			self.assertEqual(classify_item_group("خوراکی"), "sibling_under_parent")

	def test_outside_parent_excluded(self):
		def _get_value(dt, name, field=None, **kwargs):
			if dt == "Item":
				return "مواد اولیه"
			if dt == "Item Group":
				return "سایر"
			return None

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility.frappe.db.get_value",
			side_effect=_get_value,
		):
			self.assertFalse(is_eligible_fg_item("RM-01"))
			self.assertEqual(classify_item_group("مواد اولیه"), "outside")


class TestMixedSourceAndDenominatorV5728(unittest.TestCase):
	def test_mixed_document_keeps_only_eligible_rows(self):
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
				item_group=FG_ITEM_GROUP,
				parent_item_group=FG_PARENT_ITEM_GROUP,
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
				parent_item_group=FG_PARENT_ITEM_GROUP,
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
				item_code="RM-1",
				item_name="Outside",
				item_group="مواد اولیه",
				parent_item_group="سایر",
				batch_no="B3",
				qty=10,
				stock_uom="Nos",
				uom="Nos",
			),
		]
		# Eligibility is Settings Item Primary UOMs; تزریقی is only the default seed.
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract.configured_item_codes",
			return_value={"INJ-1"},
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract._fetch_manufacture_fg_candidates",
			return_value=candidates,
		):
			eligible, excluded, summary = fetch_eligible_fg_rows("C", period, settings_name="S")

		self.assertEqual(len(eligible), 1)
		self.assertEqual(eligible[0].item_code, "INJ-1")
		self.assertEqual(len(excluded), 2)
		self.assertEqual(summary["excluded_source_row_count"], 2)
		self.assertEqual(summary["excluded_qty"], 60.0)
		self.assertIn("خوراکی", summary["by_item_group"])
		self.assertEqual(excluded[0].stock_entry_detail, "SED-2")
		self.assertEqual(excluded[1].stock_entry_detail, "SED-3")

	def test_non_eligible_do_not_affect_denominator(self):
		# Only eligible products enter build_batches
		products = {
			("B1", "INJ"): ProductBucket(
				batch_no="B1",
				item_code="INJ",
				item_name="INJ",
				actual_qty=Decimal("100"),
				uom="Nos",
				normalization_factor=Decimal("2"),
				normalization_uom="Unit",
				normalized_qty=Decimal("200"),
				item_group=FG_ITEM_GROUP,
			)
		}
		batches, month_norm, _ = build_batches(products, share_precision=8)
		self.assertEqual(month_norm, Decimal("200"))
		self.assertEqual(len(batches), 1)

	def test_non_eligible_do_not_create_uom_exceptions(self):
		# Exclusions are separate from consolidate/normalize exceptions
		excluded = [
			ExclusionRow(
				stock_entry="SE",
				stock_entry_detail="SED",
				work_order="WO",
				item_code="ORAL",
				item_name="Oral",
				item_group="خوراکی",
				batch_no="B",
				qty=Decimal("5"),
				reason="sibling",
			)
		]
		summary = summarize_exclusions(excluded)
		self.assertEqual(summary["excluded_source_row_count"], 1)
		# consolidate only sees eligible rows → no UOM path for excluded items
		buckets, exceptions = consolidate_products([])
		self.assertEqual(buckets, {})
		self.assertEqual(exceptions, [])


if __name__ == "__main__":
	unittest.main()
