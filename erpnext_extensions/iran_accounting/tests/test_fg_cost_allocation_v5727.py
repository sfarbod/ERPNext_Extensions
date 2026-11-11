# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for FG Cost Allocation engine (v5.7.27)."""

from __future__ import annotations

from decimal import Decimal
import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	allocate_pools_to_batches,
	allocate_products,
	build_batches,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.contract import (
	FGCostAllocationNotConsumable,
	assert_consumable,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot, GLLineSnapshot
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket
from erpnext_extensions.iran_accounting.fg_cost_allocation.reconcile import reconcile


def _product(batch, item, qty, factor, uom="Nos"):
	norm = Decimal(qty) * Decimal(factor)
	return ProductBucket(
		batch_no=batch,
		item_code=item,
		item_name=item,
		actual_qty=Decimal(qty),
		uom=uom,
		normalization_factor=Decimal(factor),
		normalization_uom="Unit",
		normalized_qty=norm,
	)


class TestMonthWideBatchShare(unittest.TestCase):
	def test_month_wide_denominator(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 2),
			("B2", "B"): _product("B2", "B", 150, 1),
			("B3", "C"): _product("B3", "C", 300, 1),
		}
		batches, month_norm, variance = build_batches(products, share_precision=8)
		self.assertEqual(month_norm, Decimal("650"))
		shares = {b.batch_no: b.calculated_share for b in batches}
		self.assertEqual(sum(shares.values()), Decimal("1.00000000"))
		self.assertEqual(variance, Decimal("0"))
		# Largest batch absorbs quantization residual
		self.assertEqual(shares["B3"], Decimal("1") - shares["B1"] - shares["B2"])

	def test_manual_override_no_silent_renorm(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 2),
			("B2", "B"): _product("B2", "B", 150, 1),
			("B3", "C"): _product("B3", "C", 300, 1),
		}
		batches, _month_norm, variance = build_batches(
			products,
			share_precision=8,
			overrides={"B1": Decimal("0.50")},
		)
		b1 = next(b for b in batches if b.batch_no == "B1")
		b2 = next(b for b in batches if b.batch_no == "B2")
		self.assertTrue(b1.is_manual_override)
		self.assertEqual(b1.applied_share, Decimal("0.50000000"))
		# B2 remains calculated — not silently reduced
		self.assertEqual(b2.applied_share, b2.calculated_share)
		self.assertNotEqual(variance, Decimal("0"))


class TestAllocationResiduals(unittest.TestCase):
	def test_pool_to_batches_and_products(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 2),
			("B1", "D"): _product("B1", "D", 80, 1),  # multi-product batch: 200+80? wait D factor1 qty80 =80; A=200; batch=280
			("B2", "B"): _product("B2", "B", 150, 1),
			("B3", "C"): _product("B3", "C", 300, 1),
		}
		# Fix example: A 120*1 + D 80 = 200 in B1 for cleaner math
		products = {
			("B1", "A"): _product("B1", "A", 120, 1),
			("B1", "D"): _product("B1", "D", 80, 1),
			("B2", "B"): _product("B2", "B", 150, 1),
			("B3", "C"): _product("B3", "C", 300, 1),
		}
		batches, month_norm, variance = build_batches(products, share_precision=8)
		self.assertEqual(month_norm, Decimal("650"))
		pool = ExpensePoolSnapshot(
			settings_rule="r1",
			account="611104 - X",
			cost_center="CC",
			department="DEPT",
			monthly_net_expense=Decimal("1000000"),
			gl_entry_count=1,
			lines=[
				GLLineSnapshot(
					gl_entry="GLE-1",
					voucher_type="Journal Entry",
					voucher_no="JV-1",
					posting_date="2026-04-01",
					account="611104 - X",
					cost_center="CC",
					department="DEPT",
					debit=Decimal("1000000"),
					credit=Decimal("0"),
					net_amount=Decimal("1000000"),
				)
			],
		)
		bx = allocate_pools_to_batches([pool], batches, currency="IRR")
		self.assertEqual(sum(x.allocated_amount for x in bx), Decimal("1000000"))
		product_rows, px = allocate_products(batches, bx, currency="IRR", share_precision=8)
		b1 = next(b for b in batches if b.batch_no == "B1")
		b1_products = [r for r in product_rows if r[0].batch_no == "B1"]
		self.assertEqual(len(b1_products), 2)
		self.assertEqual(sum(r[3] for r in b1_products), b1.total_allocated_expense)
		self.assertEqual(sum(p.product_pool_allocated_amount for p in px), Decimal("1000000"))

		recon = reconcile(
			pools=[pool],
			batches=batches,
			batch_expenses=bx,
			product_rows=product_rows,
			product_expenses=px,
			month_norm=month_norm,
			share_variance=variance,
			share_precision=8,
			currency="IRR",
			blocking_exceptions=[],
		)
		self.assertTrue(recon.ok)


class TestConsumableContract(unittest.TestCase):
	def test_assert_consumable_rejects_incomplete(self):
		# Lightweight: patch get_doc
		class Dummy:
			status = "Incomplete"
			is_consistent = 0

		with patch("erpnext_extensions.iran_accounting.fg_cost_allocation.contract.frappe.get_doc", return_value=Dummy()):
			with self.assertRaises(FGCostAllocationNotConsumable):
				assert_consumable("X")

	def test_assert_consumable_accepts_locked_consistent(self):
		class Dummy:
			status = "Locked"
			is_consistent = 1

		with patch("erpnext_extensions.iran_accounting.fg_cost_allocation.contract.frappe.get_doc", return_value=Dummy()):
			doc = assert_consumable("X")
			self.assertEqual(doc.status, "Locked")


class TestGLExactMatchHelpers(unittest.TestCase):
	def test_expense_pool_line_sum(self):
		pool = ExpensePoolSnapshot(
			settings_rule="r",
			account="A",
			cost_center="",
			department="",
			monthly_debit=Decimal("100"),
			monthly_credit=Decimal("10"),
			monthly_net_expense=Decimal("90"),
			lines=[
				GLLineSnapshot("1", "JV", "1", "2026-01-01", "A", "", "", Decimal("60"), Decimal("0"), Decimal("60")),
				GLLineSnapshot("2", "JV", "2", "2026-01-02", "A", "", "", Decimal("40"), Decimal("10"), Decimal("30")),
			],
		)
		self.assertEqual(sum(l.net_amount for l in pool.lines), pool.monthly_net_expense)
