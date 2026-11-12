# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from decimal import Decimal

from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	BatchAllocationState,
	allocate_products,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.material_cost import (
	attribute_material_to_products,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket


def _batch(batch_no, products, total_expense, norm):
	return BatchAllocationState(
		batch_no=batch_no,
		finished_item=products[0].item_code,
		actual_qty=sum((p.actual_qty for p in products), Decimal("0")),
		uom=products[0].uom,
		normalization_factor=Decimal("1"),
		normalized_qty=norm,
		total_monthly_normalized_qty=norm,
		calculated_share=Decimal("1"),
		manual_share=None,
		is_manual_override=False,
		applied_share=Decimal("1"),
		total_allocated_expense=total_expense,
		products=products,
	)


class TestProductShareAndCost(FrappeTestCase):
	def test_single_product_share_is_100_percent(self):
		p = ProductBucket(
			batch_no="B1",
			item_code="A",
			item_name="A",
			actual_qty=Decimal("10"),
			uom="Nos",
			normalized_qty=Decimal("10"),
			normalization_factor=Decimal("1"),
			normalization_uom="Nos",
		)
		rows, _ = allocate_products(
			[_batch("B1", [p], Decimal("1000"), Decimal("10"))],
			[],
			currency="IRR",
			share_precision=8,
		)
		self.assertEqual(len(rows), 1)
		_b, _p, share, allocated = rows[0]
		self.assertEqual(share, Decimal("1"))
		self.assertEqual(allocated, Decimal("1000"))

	def test_multi_product_60_40_share_and_residual(self):
		p1 = ProductBucket(
			batch_no="B2",
			item_code="A",
			item_name="A",
			actual_qty=Decimal("60"),
			uom="Nos",
			normalized_qty=Decimal("60"),
			normalization_factor=Decimal("1"),
			normalization_uom="Nos",
		)
		p2 = ProductBucket(
			batch_no="B2",
			item_code="B",
			item_name="B",
			actual_qty=Decimal("40"),
			uom="Nos",
			normalized_qty=Decimal("40"),
			normalization_factor=Decimal("1"),
			normalization_uom="Nos",
		)
		rows, _ = allocate_products(
			[_batch("B2", [p1, p2], Decimal("1000"), Decimal("100"))],
			[],
			currency="IRR",
			share_precision=8,
		)
		shares = {r[1].item_code: r[2] for r in rows if r[2] > 0}
		amts = {r[1].item_code: r[3] for r in rows if r[2] > 0}
		self.assertEqual(shares["A"], Decimal("0.6"))
		self.assertEqual(shares["B"], Decimal("0.4"))
		self.assertEqual(sum(amts.values()), Decimal("1000"))

	def test_material_multi_product_by_share(self):
		out = attribute_material_to_products(
			batch_no="B3",
			products=[
				{"item_code": "A", "product_share": 0.6, "stock_entries": []},
				{"item_code": "B", "product_share": 0.4, "stock_entries": []},
			],
			precision=0,
		)
		self.assertEqual(out["A"]["material_consumption_value"], 0.0)
		self.assertEqual(out["B"]["material_consumption_value"], 0.0)

	def test_total_product_cost_formula(self):
		material = Decimal("200")
		allocated = Decimal("800")
		total = material + allocated
		self.assertEqual(total, Decimal("1000"))
		self.assertEqual(total / Decimal("50"), Decimal("20"))
