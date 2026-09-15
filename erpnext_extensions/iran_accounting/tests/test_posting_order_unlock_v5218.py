# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Posting Order LIKELY promotion + warehouse escalation routing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.planner import (
	READY_STATUSES,
	evaluate_row,
)


class TestPostingOrderLikelyPromotion(unittest.TestCase):
	def test_likely_sim_cleared_cross_time_promotes(self):
		row = {
			"inbound_document": "IN-1",
			"outbound_document": "OUT-1",
			"item": "ITEM",
			"warehouse": "WH",
			"batch": "B1",
			"optimizer_status": "CROSS_TIME_REPAIRABLE",
			"confidence": "LIKELY",
			"min_qty_before": -10,
			"min_qty_after": 0,
			"current_inbound_time": "2026-01-01 10:00:00",
			"current_outbound_time": "2026-01-01 09:00:00",
			"proposed_inbound_time": "2026-01-01 08:00:00",
			"proposed_outbound_time": "2026-01-01 09:00:00",
			"moves": [
				{"document": "IN-1", "new": "2026-01-01 08:00:00"},
				{"document": "OUT-1", "new": "2026-01-01 09:00:00"},
			],
		}

		def fake_scope(r, *, cache=None):
			return {
				"window_loaded": True,
				"status": "READY_BATCH_SCOPED_REPAIR",
				"reason": "READY_BATCH_SCOPED_REPAIR — test",
				"escalation_required": False,
				"dependency_type": "BATCH_DEPENDENCY",
			}

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scope.evaluate_minimal_scope",
			side_effect=fake_scope,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._write_counts",
			return_value={"se": 2, "sle": 4, "sabb": 0, "sbe": 0, "bin": 1, "sql": 7},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._posting_revalidate",
			return_value=None,
		):
			d = evaluate_row(row)
		self.assertIn(d["planner_status"], READY_STATUSES)
		self.assertTrue(d["eligible"])
		self.assertEqual(d.get("dependency"), "promoted_likely_sim_cleared")

	def test_likely_without_sim_clear_stays_manual(self):
		row = {
			"inbound_document": "IN-1",
			"outbound_document": "OUT-1",
			"optimizer_status": "CROSS_TIME_REPAIRABLE",
			"confidence": "LIKELY",
			"min_qty_before": -10,
			"min_qty_after": -5,
			"current_inbound_time": "2026-01-01 10:00:00",
			"current_outbound_time": "2026-01-01 09:00:00",
			"moves": [{"document": "IN-1", "new": "2026-01-01 08:00:00"}],
		}
		d = evaluate_row(row)
		self.assertEqual(d["planner_status"], "MANUAL")
		self.assertFalse(d["eligible"])

	def test_no_repair_needed_is_not_actionable(self):
		row = {
			"inbound_document": "IN-1",
			"outbound_document": "OUT-1",
			"optimizer_status": "NO_REPAIR_NEEDED",
			"confidence": "AMBIGUOUS",
			"min_qty_before": 0,
			"min_qty_after": 0,
			"moves": [],
		}
		d = evaluate_row(row)
		self.assertEqual(d["planner_status"], "NO_REPAIR_PATH")
		self.assertFalse(d["eligible"])


class TestExternalInboundPreviewFresh(unittest.TestCase):
	"""Purchase Receipt anchors must not be forced through Stock Entry Link lookups."""

	def test_voucher_doctype_prefers_sle_type(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.repair import _voucher_doctype

		with patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.repair.frappe.db.get_value",
			return_value="Purchase Receipt",
		):
			self.assertEqual(_voucher_doctype("MAT-PRE-1"), "Purchase Receipt")

	def test_assert_preview_fresh_accepts_purchase_receipt(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.repair import _assert_preview_fresh

		row = {
			"inbound_document": "MAT-PRE-1",
			"outbound_document": "MAT-STE-OUT",
			"inbound_modified": "2026-01-01 00:00:00.000000",
			"outbound_modified": "2026-01-01 00:00:01.000000",
		}

		def fake_get_value(doctype, name=None, fieldname=None, **kwargs):
			# frappe.db.get_value(doctype, filters, fieldname)
			field = fieldname
			filters = name
			if doctype == "Stock Ledger Entry" and isinstance(filters, dict):
				vn = filters.get("voucher_no")
				if field == "voucher_type":
					return "Purchase Receipt" if vn == "MAT-PRE-1" else "Stock Entry"
				if field == "modified":
					return row["inbound_modified"]
			if doctype == "Purchase Receipt" and filters == "MAT-PRE-1":
				if field == "modified":
					return row["inbound_modified"]
				if field == "docstatus":
					return 1
			if doctype == "Stock Entry" and filters == "MAT-STE-OUT":
				if field == "modified":
					return row["outbound_modified"]
				if field == "docstatus":
					return 1
			return None

		with patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.repair.frappe.db.get_value",
			side_effect=fake_get_value,
		):
			_assert_preview_fresh(row)


class TestWarehouseEscalationRouting(unittest.TestCase):
	def test_warehouse_ready_routed_from_escalation(self):
		row = {
			"inbound_document": "IN-1",
			"outbound_document": "OUT-1",
			"item": "ITEM",
			"warehouse": "WH",
			"batch": "B1",
			"optimizer_status": "CROSS_TIME_REPAIRABLE",
			"confidence": "EXACT",
			"min_qty_before": -10,
			"min_qty_after": 0,
			"current_inbound_time": "2026-01-01 10:00:00",
			"current_outbound_time": "2026-01-01 09:00:00",
			"moves": [
				{"document": "IN-1", "new": "2026-01-01 08:00:00"},
				{"document": "OUT-1", "new": "2026-01-01 09:00:00"},
			],
		}

		def fake_scope(r, *, cache=None):
			return {
				"window_loaded": True,
				"status": "WAREHOUSE_ESCALATION_REQUIRED",
				"escalation_required": True,
				"escalation_reason": "WAREHOUSE_MA_DEPENDENCY: test",
				"reason": "WAREHOUSE_MA_DEPENDENCY: test",
				"dependency_type": "WAREHOUSE_MA_DEPENDENCY",
				"smallest_safe_scope": "WAREHOUSE_VALUATION_SCOPED",
				"local_poisons": [],
			}

		def fake_wh(r, *, cache=None):
			return {
				"planner_status": "READY_WAREHOUSE_REPLAY",
				"eligible": True,
				"reason": "READY_WAREHOUSE_REPLAY — test",
				"sql_updates": 12,
				"affected_vouchers": ["IN-1", "OUT-1", "OTHER"],
			}

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scope.evaluate_minimal_scope",
			side_effect=fake_scope,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner.plan_warehouse_repair",
			side_effect=fake_wh,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._posting_revalidate",
			return_value=None,
		):
			d = evaluate_row(row)
		self.assertEqual(d["planner_status"], "READY_WAREHOUSE_REPLAY")
		self.assertTrue(d["eligible"])
		self.assertIn("READY_WAREHOUSE_REPLAY", READY_STATUSES)

	def test_warehouse_real_shortage_blocked(self):
		row = {
			"inbound_document": "IN-1",
			"outbound_document": "OUT-1",
			"item": "ITEM",
			"warehouse": "WH",
			"optimizer_status": "CROSS_TIME_REPAIRABLE",
			"confidence": "EXACT",
			"min_qty_before": -10,
			"min_qty_after": 0,
			"current_inbound_time": "2026-01-01 10:00:00",
			"current_outbound_time": "2026-01-01 09:00:00",
			"moves": [{"document": "IN-1", "new": "2026-01-01 08:00:00"}],
		}

		def fake_scope(r, *, cache=None):
			return {
				"window_loaded": True,
				"status": "WAREHOUSE_ESCALATION_REQUIRED",
				"escalation_required": True,
				"escalation_reason": "WAREHOUSE_MA_DEPENDENCY",
				"reason": "WAREHOUSE_MA_DEPENDENCY",
				"smallest_safe_scope": "WAREHOUSE_VALUATION_SCOPED",
				"local_poisons": [],
			}

		def fake_wh(r, *, cache=None):
			return {
				"planner_status": "WAREHOUSE_REAL_SHORTAGE",
				"eligible": False,
				"reason": "warehouse qty still negative",
				"sql_updates": 0,
			}

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scope.evaluate_minimal_scope",
			side_effect=fake_scope,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner.plan_warehouse_repair",
			side_effect=fake_wh,
		):
			d = evaluate_row(row)
		self.assertEqual(d["planner_status"], "BLOCKED")
		self.assertEqual(d.get("dependency"), "WAREHOUSE_REAL_SHORTAGE")
		self.assertIn("negative", d["reason"])


if __name__ == "__main__":
	unittest.main()
