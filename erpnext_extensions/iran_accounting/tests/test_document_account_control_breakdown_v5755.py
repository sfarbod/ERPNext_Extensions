# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Document Account Control breakdown on MFG Cost Product Allocation (v5.7.55)."""

from __future__ import annotations

import unittest
from decimal import Decimal

from erpnext_extensions.iran_accounting.fg_cost_allocation.document_breakdown import (
	build_document_breakdown,
	classify_account_control,
	sum_batch_expense_by_account_control,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import (
	ProductBucket,
	ProductionRow,
	consolidate_products,
)


class TestClassifyAccountControl(unittest.TestCase):
	def test_overhead_and_labor_prefixes(self):
		self.assertEqual(classify_account_control("211237 - کنترل سربار - E"), "overhead")
		self.assertEqual(classify_account_control("211238 - کنترل دستمزد - E"), "labor")

	def test_blank_and_unknown_are_not_inferred(self):
		self.assertIsNone(classify_account_control(""))
		self.assertIsNone(classify_account_control(None))
		self.assertIsNone(classify_account_control("611004 - حق نوبت کاری - E"))


class TestDocumentBreakdownCalc(unittest.TestCase):
	def test_multiple_documents_quantity_split(self):
		result = build_document_breakdown(
			stock_entry_qtys={
				"MAT-STE-2026-25915-2": Decimal("1158"),
				"MAT-STE-2026-25916-1": Decimal("47"),
			},
			batch_overhead=Decimal("6430823810"),
			batch_labor=Decimal("33462625292"),
			product_share=Decimal("1"),
			currency="IRR",
		)
		self.assertEqual(len(result.rows), 2)
		by_se = {r.source_document: r for r in result.rows}
		self.assertEqual(by_se["MAT-STE-2026-25915-2"].quantity, Decimal("1158"))
		self.assertEqual(by_se["MAT-STE-2026-25916-1"].quantity, Decimal("47"))
		self.assertEqual(result.total_quantity, Decimal("1205"))
		# Row total = oh + lab
		for r in result.rows:
			self.assertEqual(r.total_allocated_amount, r.overhead_amount + r.labor_amount)
		# Detail sums == parent totals
		self.assertEqual(result.total_overhead, sum(r.overhead_amount for r in result.rows))
		self.assertEqual(result.total_labor, sum(r.labor_amount for r in result.rows))
		self.assertEqual(
			result.total_allocated_amount, sum(r.total_allocated_amount for r in result.rows)
		)
		# Reconciles to product-level AC totals (share=1)
		self.assertEqual(result.total_overhead, Decimal("6430823810"))
		self.assertEqual(result.total_labor, Decimal("33462625292"))
		self.assertEqual(result.total_allocated_amount, Decimal("39893449102"))

	def test_product_share_scales_batch_amounts(self):
		result = build_document_breakdown(
			stock_entry_qtys={"SE-A": Decimal("10")},
			batch_overhead=Decimal("1000"),
			batch_labor=Decimal("4000"),
			product_share=Decimal("0.5"),
			currency="IRR",
		)
		self.assertEqual(result.rows[0].overhead_amount, Decimal("500"))
		self.assertEqual(result.rows[0].labor_amount, Decimal("2000"))
		self.assertEqual(result.rows[0].total_allocated_amount, Decimal("2500"))

	def test_only_overhead(self):
		result = build_document_breakdown(
			stock_entry_qtys={"SE-A": Decimal("3"), "SE-B": Decimal("1")},
			batch_overhead=Decimal("100"),
			batch_labor=Decimal("0"),
			product_share=Decimal("1"),
			currency="IRR",
		)
		self.assertEqual(result.total_labor, Decimal("0"))
		self.assertEqual(result.total_overhead, Decimal("100"))
		self.assertTrue(all(r.labor_amount == 0 for r in result.rows))

	def test_only_labor(self):
		result = build_document_breakdown(
			stock_entry_qtys={"SE-A": Decimal("2")},
			batch_overhead=Decimal("0"),
			batch_labor=Decimal("77"),
			product_share=Decimal("1"),
			currency="IRR",
		)
		self.assertEqual(result.total_overhead, Decimal("0"))
		self.assertEqual(result.total_labor, Decimal("77"))

	def test_missing_account_control_excluded(self):
		oh, lab, other = sum_batch_expense_by_account_control(
			[
				{"account_control": "211237 - کنترل سربار - E", "allocated_amount": 10},
				{"account_control": "", "allocated_amount": 99},
				{"account_control": "211238 - کنترل دستمزد - E", "allocated_amount": 5},
			]
		)
		self.assertEqual(oh, Decimal("10"))
		self.assertEqual(lab, Decimal("5"))
		self.assertEqual(other, Decimal("99"))

	def test_recalculation_deterministic(self):
		args = dict(
			stock_entry_qtys={"SE-B": Decimal("1"), "SE-A": Decimal("3")},
			batch_overhead=Decimal("100"),
			batch_labor=Decimal("50"),
			product_share=Decimal("1"),
			currency="IRR",
		)
		a = build_document_breakdown(**args)
		b = build_document_breakdown(**args)
		self.assertEqual([r.source_document for r in a.rows], [r.source_document for r in b.rows])
		self.assertEqual(
			[(r.overhead_amount, r.labor_amount) for r in a.rows],
			[(r.overhead_amount, r.labor_amount) for r in b.rows],
		)


class TestConsolidateTracksStockEntryQtys(unittest.TestCase):
	def test_stock_entry_qtys_accumulate(self):
		rows = [
			ProductionRow(
				stock_entry="SE-1",
				stock_entry_detail="d1",
				work_order="WO-1",
				posting_date="2026-04-01",
				item_code="FG",
				item_name="FG",
				batch_no="B1",
				qty=Decimal("10"),
				stock_uom="Nos",
				uom="Nos",
			),
			ProductionRow(
				stock_entry="SE-2",
				stock_entry_detail="d2",
				work_order="WO-1",
				posting_date="2026-04-02",
				item_code="FG",
				item_name="FG",
				batch_no="B1",
				qty=Decimal("5"),
				stock_uom="Nos",
				uom="Nos",
			),
		]
		buckets, _ = consolidate_products(rows)
		bucket: ProductBucket = buckets[("B1", "FG")]
		self.assertEqual(bucket.actual_qty, Decimal("15"))
		self.assertEqual(bucket.stock_entry_qtys["SE-1"], Decimal("10"))
		self.assertEqual(bucket.stock_entry_qtys["SE-2"], Decimal("5"))


if __name__ == "__main__":
	unittest.main()
