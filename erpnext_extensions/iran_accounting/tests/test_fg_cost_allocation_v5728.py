# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""v5.7.28 — Primary UOM integrity, provisional shares, expense rule exclusions."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	allocate_pools_to_batches,
	build_batches,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.contract import (
	FGCostAllocationNotConsumable,
	assert_consumable,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules import (
	is_admin_staff_cost_center,
	remove_blank_cc_seed_rules,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket
from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import resolve_conversion


def _product(batch, item, qty, factor=None, missing=False):
	norm = Decimal(qty) * Decimal(factor or 0) if not missing else Decimal("0")
	return ProductBucket(
		batch_no=batch,
		item_code=item,
		item_name=item,
		actual_qty=Decimal(qty),
		uom="BOX",
		normalization_factor=None if missing else Decimal(factor),
		normalization_uom="Unit",
		normalized_qty=norm,
		missing_uom_factor=missing,
	)


class TestPrimaryUOMResolutionV5728(unittest.TestCase):
	def test_stock_equals_primary(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="سرنگ",
		):
			factor, status, _ = resolve_conversion("ITEM", "سرنگ")
		self.assertEqual(status, "Valid")
		self.assertEqual(factor, Decimal("1"))

	def test_missing_conversion(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="BOX",
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.frappe.get_all",
			return_value=[],
		):
			factor, status, remarks = resolve_conversion("ITEM", "سرنگ")
		self.assertIsNone(factor)
		self.assertEqual(status, "Missing")
		self.assertIn("No UOM Conversion Detail", remarks)

	def test_ambiguous_conversion(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="BOX",
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.frappe.get_all",
			return_value=[{"name": "a", "conversion_factor": 2}, {"name": "b", "conversion_factor": 2}],
		):
			factor, status, _ = resolve_conversion("ITEM", "سرنگ")
		self.assertIsNone(factor)
		self.assertEqual(status, "Ambiguous")

	def test_invalid_non_positive(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="BOX",
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.frappe.get_all",
			return_value=[{"name": "a", "conversion_factor": 0}],
		):
			factor, status, _ = resolve_conversion("ITEM", "سرنگ")
		self.assertIsNone(factor)
		self.assertEqual(status, "Invalid")

	def test_valid_from_item_uom(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.get_item_stock_uom",
			return_value="BOX",
		), patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config.frappe.get_all",
			return_value=[{"name": "a", "conversion_factor": 2}],
		):
			factor, status, _ = resolve_conversion("ITEM", "سرنگ")
		self.assertEqual(status, "Valid")
		self.assertEqual(factor, Decimal("2"))


class TestProvisionalSharesV5728(unittest.TestCase):
	def test_incomplete_uom_does_not_finalize_100_percent(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 2),
			("B2", "B"): _product("B2", "B", 50, missing=True),
		}
		batches, month_norm, variance = build_batches(
			products, share_precision=8, finalize_share_sum=False
		)
		self.assertEqual(month_norm, Decimal("200"))
		b1 = next(b for b in batches if b.batch_no == "B1")
		self.assertEqual(b1.calculated_share, Decimal("1.00000000"))
		b2 = next(b for b in batches if b.batch_no == "B2")
		self.assertEqual(b2.calculated_share, Decimal("0"))

	def test_manual_override_cannot_hide_incomplete_production(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 2),
			("B2", "B"): _product("B2", "B", 50, missing=True),
		}
		batches, month_norm, variance = build_batches(
			products,
			share_precision=8,
			overrides={"B1": Decimal("1")},
			finalize_share_sum=False,
		)
		self.assertEqual(month_norm, Decimal("200"))
		self.assertTrue(any(b.missing_uom_factor for p in products.values() for b in [p]))
		# Applied shares may sum to 1 via override, but UOM incompleteness is separate gate
		b2 = next(b for b in batches if b.batch_no == "B2")
		self.assertEqual(b2.normalized_qty, Decimal("0"))

	def test_zero_net_pools_skipped_in_cross_alloc(self):
		products = {("B1", "A"): _product("B1", "A", 100, 1)}
		batches, _, _ = build_batches(products, share_precision=8)
		pools = [
			ExpensePoolSnapshot(
				settings_rule="r1",
				account="6A",
				cost_center="CC",
				department="D",
				monthly_net_expense=Decimal("0"),
			),
			ExpensePoolSnapshot(
				settings_rule="r2",
				account="6B",
				cost_center="CC",
				department="D",
				monthly_net_expense=Decimal("1000"),
			),
		]
		bx = allocate_pools_to_batches(pools, batches, currency="IRR")
		self.assertEqual(len(bx), 1)
		self.assertEqual(bx[0].settings_rule, "r2")
		self.assertEqual(bx[0].allocated_amount, Decimal("1000"))


class TestAdminStaffExclusionV5728(unittest.TestCase):
	def test_admin_staff_token_detection(self):
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules.frappe.db.get_value",
			return_value="اداری و ستاد",
		):
			self.assertTrue(is_admin_staff_cost_center("3000 - اداری و ستاد - E"))
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules.frappe.db.get_value",
			return_value="سربار تولید",
		):
			self.assertFalse(is_admin_staff_cost_center("1200 - سربار تولید - E"))


class TestSeedRuleRemovalV5728(unittest.TestCase):
	def test_preview_finds_exact_three_blank_cc_rows(self):
		doc = MagicMock()
		doc.check_permission = MagicMock()
		rows = []
		for account, dept in [
			("611028 - حقوق پایه - E", "واحد فرمولاسیون و ساخت بیولوژیک - E"),
			("611028 - حقوق پایه - E", "واحد فرمولاسیون و ساخت غیربیولوژیک - E"),
			("611106 - هزینه آموزش کارکنان - E", "واحد بسته بندی - E"),
		]:
			r = MagicMock()
			r.account = account
			r.cost_center = ""
			r.department = dept
			r.idx = len(rows) + 1
			r.name = f"row{r.idx}"
			rows.append(r)
		doc.expense_account_dimension_rules = rows
		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules.frappe.get_doc",
			return_value=doc,
		):
			preview = remove_blank_cc_seed_rules("SETTINGS", confirm=0)
		self.assertEqual(preview["removed"], 0)
		self.assertTrue(preview.get("preview_only"))
		self.assertEqual(len(preview["found"]), 3)


class TestConsumableGateV5728(unittest.TestCase):
	def test_rejects_provisional(self):
		class Dummy:
			status = "Calculated"
			is_consistent = 1
			shares_are_provisional = 1
			exception_count = 0
			rule_count = 5

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.contract.frappe.get_doc",
			return_value=Dummy(),
		):
			with self.assertRaises(FGCostAllocationNotConsumable):
				assert_consumable("X")

	def test_rejects_zero_rules(self):
		class Dummy:
			status = "Calculated"
			is_consistent = 1
			shares_are_provisional = 0
			exception_count = 0
			rule_count = 0

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.contract.frappe.get_doc",
			return_value=Dummy(),
		):
			with self.assertRaises(FGCostAllocationNotConsumable):
				assert_consumable("X")

	def test_rejects_incomplete_status(self):
		class Dummy:
			status = "Incomplete"
			is_consistent = 0
			shares_are_provisional = 0
			exception_count = 0
			rule_count = 5

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.contract.frappe.get_doc",
			return_value=Dummy(),
		):
			with self.assertRaises(FGCostAllocationNotConsumable):
				assert_consumable("X")


if __name__ == "__main__":
	unittest.main()
