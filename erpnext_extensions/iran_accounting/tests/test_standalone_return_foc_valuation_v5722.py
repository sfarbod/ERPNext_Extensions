# Copyright (c) 2026, ERPNext Extensions contributors
"""5.7.22/5.7.23 — Sales return valuation hierarchy (opening stock fallback).

Covers RTN-1405-00035 style cases and the mandated priority chain:
original reference → valid batch/stock rate → earliest Opening Stock → error.
Selling rate must never value inventory.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.standalone_return_valuation import (
	apply_sales_return_valuation_hierarchy,
	resolve_sales_return_incoming_rate,
	select_opening_rate_from_candidates,
)


def _row(**kwargs):
	row = SimpleNamespace(**kwargs)
	row.get = lambda k, d=None, _r=row: getattr(_r, k, d)
	return row


def _doc(**kwargs):
	doc = SimpleNamespace(**kwargs)
	doc.get = lambda k, d=None, _d=doc: getattr(_d, k, d)
	return doc


class TestSalesReturnValuationHierarchyV5723(unittest.TestCase):
	def test_priority1_original_return_against_rate(self):
		with patch(
			"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.is_irr_company",
			return_value=False,
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				return_against="SINV-1",
				original_rate=1_111_111,
				batch_no="ORPHAN",
			)
		self.assertEqual(rate, 1_111_111)

	def test_priority2_valid_batch_rate_beats_opening(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=1_126_016.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			) as opening,
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100042",
				warehouse="WH",
				company="Co",
				batch_no="7466-VALID",
				qty=-20,
			)
		self.assertEqual(rate, 1_126_016.0)
		opening.assert_not_called()

	def test_priority3_orphan_batch_uses_opening_stock(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				batch_no="9281-ORPHAN",
				qty=-1220,
			)
		self.assertEqual(rate, 2_369_208.0)

	def test_missing_batch_uses_opening_not_selling_rate(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				batch_no=None,
				qty=-100,
			)
		self.assertEqual(rate, 2_369_208.0)

	def test_nonexistent_batch_uses_opening(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				batch_no="DOES-NOT-EXIST",
				qty=-50,
			)
		self.assertEqual(rate, 2_369_208.0)

	def test_foc_zero_selling_rate_still_gets_opening(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				batch_no="9280-ORPHAN",
				qty=-380,
			)
		self.assertEqual(rate, 2_369_208.0)

	def test_non_foc_does_not_keep_selling_rate(self):
		"""Paid orphan-batch row must not keep Core's item.rate copy."""
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				batch_no="9258-ORPHAN",
				qty=-2950,
				original_rate=None,
			)
		self.assertEqual(rate, 2_369_208.0)
		self.assertNotEqual(rate, 4_412_826)

	def test_opening_prefers_same_warehouse_then_earliest(self):
		rows = [
			{
				"voucher_no": "SR-OTHER",
				"posting_date": "2026-03-01",
				"posting_time": "10:00:00",
				"creation": "2026-03-01 10:00:00",
				"warehouse": "OTHER-WH",
				"rate": 1_000_000.0,
			},
			{
				"voucher_no": "SR-SAME-LATER",
				"posting_date": "2026-03-21",
				"posting_time": "10:00:00",
				"creation": "2026-03-21 10:00:00",
				"warehouse": "WH",
				"rate": 2_369_208.0,
			},
			{
				"voucher_no": "SR-SAME-EARLY",
				"posting_date": "2026-03-20",
				"posting_time": "09:00:00",
				"creation": "2026-03-20 09:00:00",
				"warehouse": "WH",
				"rate": 2_200_000.0,
			},
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.is_irr_company",
			return_value=False,
		):
			rate = select_opening_rate_from_candidates(rows, warehouse="WH", company="Co")
		self.assertEqual(rate, 2_200_000.0)

	def test_opening_falls_back_to_other_warehouse(self):
		rows = [
			{
				"voucher_no": "SR-OTHER",
				"posting_date": "2026-03-21",
				"posting_time": "10:00:00",
				"creation": "2026-03-21 10:00:00",
				"warehouse": "OTHER-WH",
				"rate": 2_369_208.0,
			}
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.is_irr_company",
			return_value=False,
		):
			rate = select_opening_rate_from_candidates(rows, warehouse="WH-MISSING", company="Co")
		self.assertEqual(rate, 2_369_208.0)

	def test_no_opening_throws_when_required(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=0.0,
			),
			patch("frappe.throw", side_effect=Exception("missing valuation")) as thrower,
		):
			with self.assertRaises(Exception) as ctx:
				resolve_sales_return_incoming_rate(
					item_code="20100043",
					warehouse="WH",
					company="Co",
					batch_no="ORPHAN",
					throw_on_missing=True,
				)
		self.assertIn("missing valuation", str(ctx.exception))
		thrower.assert_called()

	def test_allow_zero_valuation_rate_permits_zero(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=0.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				allow_zero_valuation_rate=1,
				throw_on_missing=True,
			)
		self.assertEqual(rate, 0.0)

	def test_apply_overwrites_selling_rate_on_orphan_rows(self):
		foc = _row(
			item_code="20100043",
			qty=-1220,
			stock_qty=-1220,
			warehouse="WH",
			incoming_rate=0.0,
			rate=0.0,
			is_free_item=1,
			batch_no="9281",
			allow_zero_valuation_rate=0,
			name="r1",
		)
		paid = _row(
			item_code="20100043",
			qty=-2716,
			stock_qty=-2716,
			warehouse="WH",
			incoming_rate=4_412_826.0,
			rate=4_412_826.0,
			is_free_item=0,
			batch_no="9281",
			allow_zero_valuation_rate=0,
			name="r2",
		)
		valid = _row(
			item_code="20100042",
			qty=-20,
			stock_qty=-20,
			warehouse="WH",
			incoming_rate=1_126_016.0,
			rate=0.0,
			is_free_item=1,
			batch_no="7466",
			allow_zero_valuation_rate=0,
			name="r3",
		)
		doc = _doc(
			doctype="Sales Invoice",
			name="RTN-1405-00035",
			company="Co",
			is_return=1,
			return_against=None,
			update_stock=1,
			posting_date="2026-10-22",
			posting_time="15:05:48",
			items=[paid, foc, valid],
			packed_items=[],
			_action="save",
		)

		def _stock_rate(**kwargs):
			if kwargs.get("item_code") == "20100042":
				return 1_126_016.0
			return 0.0

		with (
			patch("frappe.get_cached_value", return_value=1),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				side_effect=lambda **kw: _stock_rate(**kw),
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			apply_sales_return_valuation_hierarchy(doc, throw_on_missing=False)

		self.assertEqual(flt(foc.incoming_rate), 2_369_208.0)
		self.assertEqual(flt(paid.incoming_rate), 2_369_208.0)
		self.assertEqual(flt(valid.incoming_rate), 1_126_016.0)

	def test_apply_preserves_linked_return_rates(self):
		row = _row(
			item_code="20100043",
			qty=-100,
			stock_qty=-100,
			warehouse="WH",
			incoming_rate=3_000_000.0,
			rate=4_412_826.0,
			batch_no="B1",
			allow_zero_valuation_rate=0,
			name="r1",
		)
		doc = _doc(
			doctype="Sales Invoice",
			name="RTN-1",
			company="Co",
			is_return=1,
			return_against="SINV-1",
			update_stock=1,
			items=[row],
			packed_items=[],
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.frappe.get_cached_value",
				return_value=1,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			) as opening,
		):
			apply_sales_return_valuation_hierarchy(doc, throw_on_missing=False)
		self.assertEqual(flt(row.incoming_rate), 3_000_000.0)
		opening.assert_not_called()

	def test_multiple_rows_same_item_different_batches(self):
		rows = [
			_row(
				item_code="20100043",
				qty=q,
				stock_qty=q,
				warehouse="WH",
				incoming_rate=0.0,
				rate=0.0,
				batch_no=b,
				allow_zero_valuation_rate=0,
				name=f"r{i}",
			)
			for i, (q, b) in enumerate([(-1220, "9281"), (-380, "9280")], start=1)
		]
		doc = _doc(
			doctype="Sales Invoice",
			name="RTN-1",
			company="Co",
			is_return=1,
			return_against=None,
			update_stock=1,
			posting_date="2026-10-22",
			posting_time="15:05:48",
			items=rows,
			packed_items=[],
		)
		with (
			patch("frappe.get_cached_value", return_value=1),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			apply_sales_return_valuation_hierarchy(doc, throw_on_missing=False)
		self.assertEqual([flt(r.incoming_rate) for r in rows], [2_369_208.0, 2_369_208.0])

	def test_integer_irr_opening_rate(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_valid_stock_valuation_rate",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.iran_accounting.domain.standalone_return_valuation.get_earliest_opening_stock_valuation_rate",
				return_value=2_369_208.0,
			),
		):
			rate = resolve_sales_return_incoming_rate(
				item_code="20100043",
				warehouse="WH",
				company="Co",
				batch_no="ORPHAN",
			)
		self.assertEqual(rate, 2_369_208.0)
		self.assertEqual(rate, float(int(rate)))


if __name__ == "__main__":
	unittest.main()
