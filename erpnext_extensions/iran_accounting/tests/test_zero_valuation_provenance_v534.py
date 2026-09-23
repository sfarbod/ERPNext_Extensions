# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.4 — zero valuation provenance, leftover-MA replay, runtime guard."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	LEFTOVER_MA_READY,
	ZP_PROVEN_LEGITIMATE_ZERO,
	ZP_UNPROVEN_ZERO,
	ZP_VALUED_STOCK_ZERO_STAMP,
	ZP_ZERO_INBOUND_ON_VALUED_POSITION,
)
from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
	already_matches,
	replay_leftover_ma_series,
	series_safety,
)
from erpnext_extensions.iran_accounting.historical_stock.runtime_guard import (
	assert_outgoing_rates_not_silently_zeroed,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_provenance import classify_zero_provenance


def _sle(**kw):
	row = {
		"name": kw.get("name", "sle"),
		"voucher_no": kw.get("voucher_no", "V"),
		"voucher_type": "Stock Entry",
		"posting_datetime": kw.get("posting_datetime", "2026-01-01 00:00:00"),
		"actual_qty": kw.get("actual_qty", 0),
		"qty_after_transaction": kw.get("qty_after_transaction", 0),
		"incoming_rate": kw.get("incoming_rate", 0),
		"outgoing_rate": kw.get("outgoing_rate", 0),
		"valuation_rate": kw.get("valuation_rate", 0),
		"stock_value": kw.get("stock_value", 0),
		"stock_value_difference": kw.get("stock_value_difference", 0),
	}
	row.update(kw)
	return row


class TestProvenanceClassification(unittest.TestCase):
	def test_a_depleted_then_free_receipt(self):
		rows = [
			_sle(voucher_no="IN", actual_qty=8, qty_after_transaction=8, incoming_rate=100, valuation_rate=100, stock_value=800, stock_value_difference=800),
			_sle(voucher_no="OUT", actual_qty=-8, qty_after_transaction=0, outgoing_rate=100, valuation_rate=100, stock_value=0, stock_value_difference=-800),
			_sle(voucher_no="FREE", actual_qty=2, qty_after_transaction=2, incoming_rate=0, valuation_rate=0, stock_value=0, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="A", warehouse="W", rows=rows)
		self.assertEqual(prov["provenance"], ZP_PROVEN_LEGITIMATE_ZERO)
		self.assertTrue(prov["allow_zero_outgoing"])
		self.assertFalse(prov["block_zero_outgoing"])
		self.assertTrue(prov["depleted_old_value_history"])
		self.assertEqual(prov["last_depletion_voucher"], "OUT")
		self.assertEqual(prov["zero_inbound_voucher"], "FREE")

	def test_b_zero_inbound_on_valued_stock(self):
		rows = [
			_sle(voucher_no="IN", actual_qty=522, qty_after_transaction=522, incoming_rate=5072466, valuation_rate=5072466, stock_value=2647827252, stock_value_difference=2647827252),
			_sle(
				voucher_no="FREE",
				actual_qty=7,
				qty_after_transaction=529,
				incoming_rate=0,
				valuation_rate=0,
				stock_value=2647827252,
				stock_value_difference=0,
			),
		]
		prov = classify_zero_provenance(item="B", warehouse="W", rows=rows)
		self.assertEqual(prov["provenance"], ZP_VALUED_STOCK_ZERO_STAMP)
		self.assertFalse(prov["allow_zero_outgoing"])
		self.assertTrue(prov["block_zero_outgoing"])
		self.assertEqual(prov["secondary_provenance"], ZP_ZERO_INBOUND_ON_VALUED_POSITION)
		self.assertAlmostEqual(prov["zero_on_valued"]["expected_ma"], 2647827252 / 529, places=4)

	def test_c_old_rate_not_current_evidence(self):
		rows = [
			_sle(voucher_no="IN", actual_qty=8, qty_after_transaction=8, incoming_rate=241875000, valuation_rate=241875000, stock_value=1935000000, stock_value_difference=1935000000),
			_sle(voucher_no="OUT", actual_qty=-8, qty_after_transaction=0, outgoing_rate=241875000, valuation_rate=241875000, stock_value=0, stock_value_difference=-1935000000),
			_sle(voucher_no="FREE", actual_qty=2, qty_after_transaction=2, incoming_rate=0, valuation_rate=0, stock_value=0, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="C", warehouse="W", rows=rows)
		self.assertTrue(prov["depleted_old_value_history"])
		self.assertTrue(prov["allow_zero_outgoing"])

	def test_d_qty_and_value_outgoing_zero_is_corrupt(self):
		rows = [
			_sle(voucher_no="IN", actual_qty=10, qty_after_transaction=10, incoming_rate=100, valuation_rate=100, stock_value=1000, stock_value_difference=1000),
			_sle(voucher_no="ISSUE", actual_qty=-1, qty_after_transaction=9, outgoing_rate=0, valuation_rate=0, stock_value=1000, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="D", warehouse="W", rows=rows)
		self.assertEqual(prov["provenance"], ZP_VALUED_STOCK_ZERO_STAMP)
		self.assertTrue(prov["block_zero_outgoing"])

	def test_e_opening_state_issue_is_unproven(self):
		rows = [
			_sle(voucher_no="ISSUE", actual_qty=-1, qty_after_transaction=2, outgoing_rate=0, valuation_rate=100, stock_value=200, stock_value_difference=-100),
		]
		prov = classify_zero_provenance(item="E", warehouse="W", rows=rows)
		self.assertEqual(prov["provenance"], ZP_UNPROVEN_ZERO)
		self.assertFalse(prov["allow_zero_outgoing"])
		self.assertEqual(prov["reason"], "OPENING_STATE_INCOMPLETE")


class TestLeftoverMaReplay(unittest.TestCase):
	def test_b_receipt_stays_zero_ma_stays_nonzero(self):
		rows = [
			{"name": "1", "voucher_no": "FREE", "actual_qty": 7, "incoming_rate": 0, "stock_value_difference": 0, "allow_zero_valuation_rate": 1},
			{"name": "2", "voucher_no": "ISSUE", "actual_qty": -2, "incoming_rate": 0, "stock_value_difference": 0},
		]
		series = replay_leftover_ma_series(rows, 522, 2647827252, honor_zero_vouchers={"FREE"})
		self.assertEqual(flt(series[0]["incoming_rate"]), 0)
		self.assertEqual(flt(series[0]["stock_value_difference"]), 0)
		self.assertAlmostEqual(flt(series[0]["qty_after_transaction"]), 529)
		self.assertAlmostEqual(flt(series[0]["stock_value"]), 2647827252)
		expected_ma = 2647827252 / 529
		self.assertAlmostEqual(flt(series[0]["valuation_rate"]), expected_ma, places=4)
		self.assertAlmostEqual(flt(series[1]["outgoing_rate"]), expected_ma, places=4)
		self.assertAlmostEqual(flt(series[1]["stock_value"]), 2647827252 - 2 * expected_ma, places=2)
		self.assertTrue(series_safety(series)["ok"])

	def test_f_idempotency_already_matches(self):
		rows = [
			{"name": "1", "voucher_no": "FREE", "actual_qty": 7, "incoming_rate": 0, "stock_value_difference": 0, "qty_after_transaction": 529, "stock_value": 2647827252, "valuation_rate": 2647827252 / 529},
		]
		series = replay_leftover_ma_series(rows, 522, 2647827252, honor_zero_vouchers={"FREE"})
		self.assertTrue(already_matches(rows, series))

	def test_g_safety_flags_negative_qty(self):
		bad = [{"qty_after_transaction": -1, "stock_value": 0, "incoming_rate": 0}]
		self.assertFalse(series_safety(bad)["ok"])
		self.assertEqual(series_safety(bad)["neg_qty"], 1)

	def test_g_safety_flags_i4(self):
		bad = [{"qty_after_transaction": 0, "stock_value": 500, "incoming_rate": 0}]
		self.assertFalse(series_safety(bad)["ok"])
		self.assertEqual(series_safety(bad)["i4"], 1)


class TestRuntimeGuardProvenance(unittest.TestCase):
	def _doc(self, item="X"):
		row = {
			"name": "d1",
			"item_code": item,
			"qty": 1,
			"transfer_qty": 1,
			"s_warehouse": "WH",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
			"batch_no": None,
		}
		doc = SimpleNamespace(
			doctype="Stock Entry",
			name="MAT-STE-TEST",
			docstatus=0,
			purpose="Material Issue",
			posting_date="2026-09-23",
			posting_time="12:43:04",
			flags=SimpleNamespace(in_submit=True, get=lambda *a, **k: True),
			items=[row],
		)
		doc.get = lambda k, d=None: doc.items if k == "items" else getattr(doc, k, d)
		return doc

	def _guard_ctx(self, prov):
		import frappe

		frappe.flags.stock_entry_before_submit = True
		frappe.flags.historical_stock_repair = False
		frappe.flags.skip_lost_rate_guard = False
		return patch(
			"erpnext_extensions.iran_accounting.historical_stock.runtime_guard.provenance_as_of",
			return_value=prov,
		)

	def test_a_allows_proven_legitimate_zero(self):
		rows = [
			_sle(voucher_no="IN", actual_qty=8, qty_after_transaction=8, incoming_rate=100, valuation_rate=100, stock_value=800, stock_value_difference=800),
			_sle(voucher_no="OUT", actual_qty=-8, qty_after_transaction=0, outgoing_rate=100, valuation_rate=100, stock_value=0, stock_value_difference=-800),
			_sle(voucher_no="FREE", actual_qty=2, qty_after_transaction=2, incoming_rate=0, valuation_rate=0, stock_value=0, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="A", warehouse="WH", rows=rows)
		import frappe

		try:
			with self._guard_ctx(prov):
				assert_outgoing_rates_not_silently_zeroed(self._doc("A"))
		finally:
			frappe.flags.stock_entry_before_submit = False

	def test_c_old_depleted_rate_does_not_block(self):
		rows = [
			_sle(voucher_no="IN", actual_qty=8, qty_after_transaction=8, incoming_rate=241875000, valuation_rate=241875000, stock_value=1935000000, stock_value_difference=1935000000),
			_sle(voucher_no="OUT", actual_qty=-8, qty_after_transaction=0, outgoing_rate=241875000, valuation_rate=241875000, stock_value=0, stock_value_difference=-1935000000),
			_sle(voucher_no="FREE", actual_qty=2, qty_after_transaction=2, incoming_rate=0, valuation_rate=0, stock_value=0, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="C", warehouse="WH", rows=rows)
		import frappe

		try:
			with self._guard_ctx(prov), patch(
				"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._previous_healthy_sle_rate",
				return_value=241875000,
			):
				assert_outgoing_rates_not_silently_zeroed(self._doc("C"))
		finally:
			frappe.flags.stock_entry_before_submit = False

	def test_d_blocks_valued_stock_zero_outgoing(self):
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import ValuationIntegrityError

		rows = [
			_sle(voucher_no="IN", actual_qty=10, qty_after_transaction=10, incoming_rate=100, valuation_rate=100, stock_value=1000, stock_value_difference=1000),
			_sle(voucher_no="POISON", actual_qty=7, qty_after_transaction=17, incoming_rate=0, valuation_rate=0, stock_value=1000, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="D", warehouse="WH", rows=rows)
		import frappe

		try:
			with self._guard_ctx(prov), patch(
				"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._batch_inward_rate",
				return_value=0,
			):
				self.assertRaises(ValuationIntegrityError, assert_outgoing_rates_not_silently_zeroed, self._doc("D"))
		finally:
			frappe.flags.stock_entry_before_submit = False

	def test_allow_zero_on_outgoing_does_not_bypass_valued_stock(self):
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import ValuationIntegrityError

		rows = [
			_sle(voucher_no="IN", actual_qty=10, qty_after_transaction=10, incoming_rate=100, valuation_rate=100, stock_value=1000, stock_value_difference=1000),
			_sle(voucher_no="POISON", actual_qty=7, qty_after_transaction=17, incoming_rate=0, valuation_rate=0, stock_value=1000, stock_value_difference=0),
		]
		prov = classify_zero_provenance(item="D", warehouse="WH", rows=rows)
		import frappe

		doc = self._doc("D")
		doc.items[0]["allow_zero_valuation_rate"] = 1
		try:
			with self._guard_ctx(prov), patch(
				"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._batch_inward_rate",
				return_value=0,
			):
				self.assertRaises(ValuationIntegrityError, assert_outgoing_rates_not_silently_zeroed, doc)
		finally:
			frappe.flags.stock_entry_before_submit = False


class TestPlannerLeftoverMa(unittest.TestCase):
	def test_ready_exact_is_eligible(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES, evaluate_row

		decision = evaluate_row(
			{
				"topic": "LEFTOVER_MA",
				"repair_class": "LEFTOVER_MA_REPAIR",
				"leftover_ma_status": LEFTOVER_MA_READY,
				"confidence": "EXACT",
				"sql_updates": 4,
				"replay_count": 3,
			}
		)
		self.assertTrue(decision["eligible"])
		self.assertIn(decision["planner_status"], READY_STATUSES)

	def test_stamp_preserves_ready_sql(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

		stamped = stamp_scan_result(
			{
				"count": 1,
				"rows": [
					{
						"topic": "LEFTOVER_MA",
						"repair_class": "LEFTOVER_MA_REPAIR",
						"leftover_ma_status": LEFTOVER_MA_READY,
						"confidence": "EXACT",
						"sql_updates": 4,
						"replay_count": 3,
						"item": "X",
						"warehouse": "W",
						"voucher": "STE-1",
					}
				],
			}
		)
		row = stamped["rows"][0]
		self.assertTrue(row["eligible"])
		self.assertEqual(row["sql_updates"], 4)
		self.assertEqual(stamped.get("repairable"), 1)

	def test_e_ambiguous_not_ready(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row

		decision = evaluate_row(
			{
				"topic": "LEFTOVER_MA",
				"repair_class": "LEFTOVER_MA_REPAIR",
				"leftover_ma_status": "MANUAL_LEFTOVER_MA",
				"confidence": "AMBIGUOUS",
				"sql_updates": 4,
			}
		)
		self.assertFalse(decision["eligible"])
