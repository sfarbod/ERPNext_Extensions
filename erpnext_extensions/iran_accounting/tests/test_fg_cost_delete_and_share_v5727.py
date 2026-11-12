# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: delete clears revision orphans; Applied Share uses currency rounding (v5.7.27)."""

from __future__ import annotations

import unittest
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch, call

from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	allocate_pools_to_batches,
	build_batches,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot
from erpnext_extensions.iran_accounting.fg_cost_allocation.persist import clear_details
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket


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


class TestClearDetailsRemovesRevisions(unittest.TestCase):
	def test_full_clear_deletes_revision_rows(self):
		executed = []

		def fake_sql(query, values=None):
			executed.append((query, values))

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.persist.frappe"
		) as mock_frappe:
			mock_frappe.db.sql.side_effect = fake_sql
			clear_details("MFGCC-TEST-1")

		rev_deletes = [
			q for q, _v in executed if "MFG Cost Calculation Revision" in q and "DELETE" in q
		]
		self.assertEqual(len(rev_deletes), 1)
		self.assertEqual(executed[-1][1], ("MFGCC-TEST-1",))

	def test_per_revision_clear_keeps_revision_log(self):
		executed = []

		def fake_sql(query, values=None):
			executed.append((query, values))

		with patch(
			"erpnext_extensions.iran_accounting.fg_cost_allocation.persist.frappe"
		) as mock_frappe:
			mock_frappe.db.sql.side_effect = fake_sql
			clear_details("MFGCC-TEST-1", revision=3)

		rev_deletes = [
			q for q, _v in executed if "MFG Cost Calculation Revision" in q and "DELETE" in q
		]
		self.assertEqual(rev_deletes, [])


class TestAppliedShareCurrencyRounding(unittest.TestCase):
	def test_allocated_amount_matches_pool_times_share_money_precision(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 1),
			("B2", "B"): _product("B2", "B", 100, 1),
		}
		batches, _month_norm, _var = build_batches(products, share_precision=8)
		pool = ExpensePoolSnapshot(
			settings_rule="R1",
			account="611233",
			cost_center="",
			department="Dept",
			monthly_net_expense=Decimal("15181147960"),
		)
		results = allocate_pools_to_batches([pool], batches, currency="IRR")
		b1 = next(b for b in batches if b.batch_no == "B1")
		row = next(r for r in results if r.batch_no == "B1")
		# IRR precision 0: Allocated Amount = round(Pool × Applied Share)
		expected = (pool.monthly_net_expense * b1.applied_share).quantize(
			Decimal("1"), rounding=ROUND_HALF_UP
		)
		self.assertEqual(row.allocated_amount, expected)

	def test_manual_override_does_not_rebalance_other_batch(self):
		products = {
			("B1", "A"): _product("B1", "A", 100, 1),
			("B2", "B"): _product("B2", "B", 300, 1),
		}
		batches, _m, variance = build_batches(
			products, share_precision=8, overrides={"B1": Decimal("0.10")}
		)
		b1 = next(b for b in batches if b.batch_no == "B1")
		b2 = next(b for b in batches if b.batch_no == "B2")
		self.assertEqual(b1.applied_share, Decimal("0.10000000"))
		self.assertEqual(b2.applied_share, b2.calculated_share)
		self.assertNotEqual(variance, Decimal("0"))


if __name__ == "__main__":
	unittest.main()
