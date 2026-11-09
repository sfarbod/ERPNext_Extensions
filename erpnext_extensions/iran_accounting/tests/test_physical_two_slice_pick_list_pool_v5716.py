# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.16 — Pick List SABE must not deplete Physical Two-Slice batch pools."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.domain.physical_two_slice import AllocationError
from erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle import (
	POOL_EXCLUDED_VOUCHER_TYPES,
	read_batch_pool,
	rewrite_outgoing_bundle_after_core,
)


class TestPickListExcludedFromBatchPoolV5716(unittest.TestCase):
	def test_pool_excludes_pick_list_constant(self):
		self.assertIn("Pick List", POOL_EXCLUDED_VOUCHER_TYPES)

	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.db.sql"
	)
	def test_read_batch_pool_sql_excludes_pick_list(self, sql):
		sql.return_value = [frappe._dict({"qty": 8, "svd": 16018544})]
		q, v = read_batch_pool("20100006", "BATCH-A", "WH-A")
		self.assertEqual(q, Decimal("8"))
		self.assertEqual(v, Decimal("16018544"))
		query, params = sql.call_args[0][0], sql.call_args[0][1]
		self.assertIn("voucher_type", query)
		self.assertIn("Pick List", params)
		# item, batch, warehouse, then excluded voucher type
		self.assertEqual(params[:4], ("20100006", "BATCH-A", "WH-A", "Pick List"))

	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.db.sql"
	)
	def test_pick_list_plus_dn_does_not_double_count_in_query(self, sql):
		"""Regression shape for MAT-DN-2026-00315 / STO-PICK-2026-00005.

		Without exclusion: SE(+241)+DN(-233)+Pick(-11) = -3.
		With exclusion: SE(+241)+DN(-233) = 8 — enough for out_qty=8.
		"""
		sql.return_value = [frappe._dict({"qty": 8, "svd": 16018544})]
		q, _v = read_batch_pool(
			"20100006",
			"1834-20100006-mr255013d01",
			"انبار محصولات نهایی  نزد ارکید فارمد - E",
			exclude_voucher_detail_no="tst08hijoe",
		)
		self.assertEqual(q, Decimal("8"))
		self.assertGreaterEqual(q, Decimal("8"))
		params = sql.call_args[0][1]
		# voucher_type filter binds before voucher_detail_no exclude
		self.assertEqual(params[3], "Pick List")
		self.assertEqual(params[4], "tst08hijoe")

	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.apply_plan_to_bundle",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.allocate_physical_two_slice"
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.read_batch_pool",
		return_value=(Decimal("8"), Decimal("16018544")),
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.get_all",
		return_value=[
			frappe._dict(
				{
					"name": "e1",
					"batch_no": "1834-20100006-mr255013d01",
					"qty": -8,
					"warehouse": "WH-A",
					"item_code": "20100006",
				}
			)
		],
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
		return_value=True,
	)
	def test_rewrite_succeeds_when_pool_covers_out_qty(
		self, _irr, _get_all, _pool, alloc, _apply
	):
		from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
			AllocationPlan,
			ValuationSlice,
		)

		alloc.return_value = AllocationPlan(
			pool_qty=Decimal("8"),
			pool_value=Decimal("16018544"),
			out_qty_abs=Decimal("8"),
			out_value_abs=Decimal("16018544"),
			base_rate=2002318,
			slices=(
				ValuationSlice(
					qty=Decimal("-8"), rate=2002318, amount=Decimal("-16018544")
				),
			),
			mode="full_deplete",
		)
		sle = frappe._dict(
			{
				"company": "اسپاد فارمد دارو",
				"voucher_type": "Delivery Note",
				"voucher_no": "MAT-DN-2026-00315",
				"voucher_detail_no": "tst08hijoe",
				"actual_qty": -8,
				"serial_and_batch_bundle": "BUNDLE-1",
				"warehouse": "WH-A",
				"item_code": "20100006",
			}
		)
		uea = frappe._dict(company="اسپاد فارمد دارو")
		plan = rewrite_outgoing_bundle_after_core(uea, sle, doc=None)
		self.assertIsNotNone(plan)
		alloc.assert_called_once()
		self.assertEqual(alloc.call_args[0][0], Decimal("8"))
		self.assertEqual(alloc.call_args[0][2], Decimal("8"))

	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe.get_all",
		return_value=[
			frappe._dict(
				{
					"name": "e1",
					"batch_no": "BATCH-A",
					"qty": -8,
					"warehouse": "WH-A",
					"item_code": "ITEM-A",
				}
			)
		],
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.read_batch_pool",
		return_value=(Decimal("-3"), Decimal("16018544")),
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
		return_value=True,
	)
	def test_rewrite_still_fails_closed_on_genuine_short_pool(self, _irr, _pool, _get_all):
		"""Do not weaken availability — true short pools must still raise."""
		sle = frappe._dict(
			{
				"company": "Co",
				"voucher_type": "Delivery Note",
				"voucher_no": "DN-X",
				"voucher_detail_no": "d1",
				"actual_qty": -8,
				"serial_and_batch_bundle": "B1",
				"warehouse": "WH-A",
				"item_code": "ITEM-A",
			}
		)
		uea = frappe._dict(company="Co")
		frappe.flags.through_repost_item_valuation = False
		with self.assertRaises(AllocationError) as ctx:
			rewrite_outgoing_bundle_after_core(uea, sle, doc=None)
		self.assertIn("empty/short pool", str(ctx.exception))
		self.assertIn("pool_qty=-3", str(ctx.exception))


if __name__ == "__main__":
	unittest.main()
