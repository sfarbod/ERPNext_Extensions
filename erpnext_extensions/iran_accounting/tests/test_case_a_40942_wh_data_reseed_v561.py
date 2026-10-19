# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1: CASE A — reseed desynced wh_data before consume-to-zero (MAT-STE-2026-40942)."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt


class TestCaseA40942WhDataReseed(unittest.TestCase):
	def test_reseed_when_wh_qty_desynced_from_previous(self):
		from erpnext_extensions.iran_accounting.domain import sle_persistence as sp

		engine = mock.Mock()
		engine.wh_data = frappe._dict(
			qty_after_transaction=15721.0,
			stock_value=548612420.0,
			valuation_rate=34896.0,
			prev_stock_value=548612420.0,
		)
		sle = frappe._dict(
			name="MAT-SLE-2026-308219",
			item_code="13200244",
			warehouse="انبار پایکار خط تولید اسپاد فارمد",
			actual_qty=-1.0,
			qty_after_transaction=0.0,
			posting_datetime="2026-08-28 12:00:00",
			creation="2026-10-07 13:05:38",
		)
		with mock.patch(
			"frappe.db.sql",
			return_value=[
				frappe._dict(
					qty_after_transaction=1.0,
					stock_value=58983128.0,
					valuation_rate=58983128.0,
				)
			],
		):
			changed = sp.reseed_wh_data_from_chronological_previous(engine, sle)
		self.assertTrue(changed)
		self.assertEqual(flt(engine.wh_data.qty_after_transaction), 1.0)
		self.assertEqual(flt(engine.wh_data.stock_value), 58983128.0)
		self.assertEqual(flt(engine.wh_data.prev_stock_value), 58983128.0)

	def test_reseed_noop_when_already_aligned(self):
		from erpnext_extensions.iran_accounting.domain import sle_persistence as sp

		engine = mock.Mock()
		engine.wh_data = frappe._dict(
			qty_after_transaction=1.0,
			stock_value=58983128.0,
			valuation_rate=58983128.0,
			prev_stock_value=58983128.0,
		)
		sle = frappe._dict(
			name="SLE",
			item_code="I",
			warehouse="W",
			actual_qty=-1.0,
			qty_after_transaction=0.0,
			posting_datetime="2026-08-28 12:00:00",
			creation="2026-10-07 13:05:38",
		)
		with mock.patch(
			"frappe.db.sql",
			return_value=[
				frappe._dict(
					qty_after_transaction=1.0,
					stock_value=58983128.0,
					valuation_rate=58983128.0,
				)
			],
		):
			changed = sp.reseed_wh_data_from_chronological_previous(engine, sle)
		self.assertFalse(changed)

	def test_restore_sets_prev_stock_value(self):
		from erpnext_extensions.iran_accounting.domain import sle_persistence as sp

		engine = mock.Mock()
		engine.wh_data = frappe._dict(
			qty_after_transaction=99.0,
			stock_value=1.0,
			valuation_rate=1.0,
			prev_stock_value=999999.0,
		)
		sle = frappe._dict(name="X")
		pre = {
			"qty_after_transaction": 1.0,
			"stock_value": 58983128.0,
			"valuation_rate": 58983128.0,
			"stock_value_difference": -100.0,
			"incoming_rate": 0.0,
			"outgoing_rate": 100.0,
		}
		self.assertTrue(sp.restore_out_of_scope_sle_ledger_state(engine, sle, pre))
		self.assertEqual(flt(engine.wh_data.prev_stock_value), 58983128.0)
		self.assertEqual(flt(engine.wh_data.qty_after_transaction), 1.0)

	def test_reseed_repairs_negative_previous_stock_value(self):
		from erpnext_extensions.iran_accounting.domain import sle_persistence as sp

		engine = mock.Mock()
		engine.wh_data = frappe._dict(
			qty_after_transaction=15721.0,
			stock_value=548612420.0,
			valuation_rate=34896.0,
			prev_stock_value=548612420.0,
		)
		sle = frappe._dict(
			name="MAT-SLE-2026-308219",
			item_code="13200244",
			warehouse="WH",
			actual_qty=-1.0,
			qty_after_transaction=0.0,
			posting_datetime="2026-08-28 12:00:00",
			creation="2026-10-07 13:05:38",
		)
		with mock.patch(
			"frappe.db.sql",
			return_value=[
				frappe._dict(
					qty_after_transaction=1.0,
					stock_value=-95984263.0,  # mid-chain poison
					valuation_rate=58983128.0,
				)
			],
		):
			changed = sp.reseed_wh_data_from_chronological_previous(engine, sle)
		self.assertTrue(changed)
		self.assertEqual(flt(engine.wh_data.qty_after_transaction), 1.0)
		# Must not install negative poison into wh_data.
		self.assertGreaterEqual(flt(engine.wh_data.stock_value), 0.0)
		self.assertEqual(flt(engine.wh_data.stock_value), 58983128.0)


if __name__ == "__main__":
	unittest.main()
