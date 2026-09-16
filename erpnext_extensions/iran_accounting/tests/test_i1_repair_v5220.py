# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — I1 negative incoming rate repair (v5.2.20)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import (
	I1_MANUAL,
	I1_NEGATIVE_RATE_REPAIR,
	I1_READY,
	I1_WAITING,
	TOPIC_I1,
)
from erpnext_extensions.iran_accounting.historical_stock.i1_repair import (
	classify_i1_voucher,
	document_issue_rates,
)
from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_READY_I1,
	PLAN_WAITING_I1,
	evaluate_row,
)


def _out(item, qty, rate, idx=1):
	"""Outgoing (issued) row."""
	return {
		"name": f"row-out-{idx}",
		"idx": idx,
		"item_code": item,
		"qty": qty,
		"transfer_qty": qty,
		"basic_rate": rate,
		"basic_amount": qty * rate,
		"valuation_rate": rate,
		"amount": qty * rate,
		"additional_cost": 0,
		"landed_cost_voucher_amount": 0,
		"s_warehouse": "WH-Prod",
		"t_warehouse": None,
		"is_finished_item": 0,
		"secondary_item_type": None,
		"serial_and_batch_bundle": None,
		"batch_no": None,
	}


def _in(item, qty, rate, idx=50, finished=0):
	"""Incoming row — finished good or secondary return."""
	return {
		"name": f"row-in-{idx}",
		"idx": idx,
		"item_code": item,
		"qty": qty,
		"transfer_qty": qty,
		"basic_rate": rate,
		"basic_amount": qty * rate,
		"valuation_rate": rate,
		"amount": qty * rate,
		"additional_cost": 0,
		"landed_cost_voucher_amount": 0,
		"s_warehouse": None,
		"t_warehouse": "WH-FG" if finished else "WH-Store",
		"is_finished_item": finished,
		"secondary_item_type": None,
		"serial_and_batch_bundle": None,
		"batch_no": None,
	}


_HEADER = {
	"name": "STE-1",
	"purpose": "Manufacture",
	"company": "Espad",
	"posting_date": "2026-08-25",
	"posting_time": "10:00:00",
	"work_order": "WO-1",
	"docstatus": 1,
}


def _classify(rows, header=None):
	with patch(
		"erpnext_extensions.iran_accounting.historical_stock.i1_repair.frappe.db.get_value",
		return_value=dict(header or _HEADER),
	), patch(
		"erpnext_extensions.iran_accounting.historical_stock.i1_repair._fetch_se_rows",
		return_value=rows,
	):
		return classify_i1_voucher("STE-1")


class TestDocumentIssueRates(unittest.TestCase):
	def test_weighted_rate_across_multiple_issue_rows(self):
		rows = [_out("RM-1", 100, 10, idx=1), _out("RM-1", 100, 20, idx=2)]
		self.assertAlmostEqual(document_issue_rates(rows)["RM-1"], 15.0)

	def test_incoming_rows_never_contribute_to_issue_rate(self):
		rows = [_out("RM-1", 10, 50, idx=1), _in("RM-1", 5, 999999, idx=9)]
		self.assertAlmostEqual(document_issue_rates(rows)["RM-1"], 50.0)

	def test_zero_qty_row_is_ignored(self):
		self.assertEqual(document_issue_rates([_out("RM-1", 0, 10, idx=1)]), {})


class TestClassifyI1Voucher(unittest.TestCase):
	def test_ready_reprices_secondary_from_document_issue_rate(self):
		"""MAT-STE-2026-24798 shape: returned material priced from a poisoned warehouse rate."""
		rows = [
			_out("RM-1", 4149, 67589, idx=1),
			_in("FG-1", 4100, -153836201, idx=7, finished=1),
			_in("RM-1", 49, 12902012539, idx=8),
		]
		row = _classify(rows)
		self.assertEqual(row["i1_status"], I1_READY)
		self.assertTrue(row["eligible"])
		self.assertEqual(row["topic"], TOPIC_I1)
		self.assertEqual(row["repair_class"], I1_NEGATIVE_RATE_REPAIR)
		# Secondary repriced at the issue rate of this same document.
		self.assertAlmostEqual(row["secondary_corrected_total"], 49 * 67589)
		# FG residual = outgoing pool - corrected secondary, and must be positive.
		self.assertAlmostEqual(row["corrected_fg_amount"], 4149 * 67589 - 49 * 67589)
		self.assertGreater(row["proposed_rate"], 0)
		self.assertAlmostEqual(row["proposed_rate"], row["corrected_fg_amount"] / 4100)
		self.assertEqual(row["source_of_truth"], "same_document_issue_rate")

	def test_waiting_when_secondary_item_is_not_issued_in_document(self):
		"""By-product with no in-document source must not be guessed from warehouse valuation."""
		rows = [
			_out("RM-1", 1000, 500, idx=1),
			_in("FG-1", 100, -900000, idx=7, finished=1),
			_in("SCRAP-9", 5, 1e9, idx=8),
		]
		row = _classify(rows)
		self.assertEqual(row["i1_status"], I1_WAITING)
		self.assertFalse(row["eligible"])
		self.assertEqual(row["blocked_item"], "SCRAP-9")

	def test_waiting_when_in_document_issue_rate_is_zero(self):
		rows = [
			_out("RM-1", 1000, 0, idx=1),
			_out("RM-2", 500, 800, idx=2),
			_in("FG-1", 100, -5000, idx=7, finished=1),
			_in("RM-1", 5, 1e9, idx=8),
		]
		row = _classify(rows)
		self.assertEqual(row["i1_status"], I1_WAITING)
		self.assertEqual(row["blocked_item"], "RM-1")

	def test_manual_when_multiple_finished_goods(self):
		rows = [
			_out("RM-1", 100, 10, idx=1),
			_in("FG-1", 10, -50, idx=7, finished=1),
			_in("FG-2", 10, -50, idx=8, finished=1),
		]
		self.assertEqual(_classify(rows)["i1_status"], I1_MANUAL)

	def test_manual_when_corrected_pool_is_still_negative(self):
		"""Understated consumption — repricing cannot rescue the pool; never write."""
		rows = [
			_out("RM-1", 1, 10, idx=1),
			_in("FG-1", 5, -100, idx=7, finished=1),
			_in("RM-1", 100, 1e6, idx=8),
		]
		row = _classify(rows)
		self.assertEqual(row["i1_status"], I1_MANUAL)
		self.assertFalse(row["eligible"])

	def test_not_i1_without_any_negative_incoming(self):
		rows = [_out("RM-1", 100, 10, idx=1), _in("FG-1", 10, 100, idx=7, finished=1)]
		self.assertEqual(_classify(rows)["i1_status"], "NOT_I1")

	def test_not_i1_when_purpose_is_not_manufacture(self):
		header = dict(_HEADER, purpose="Material Transfer")
		rows = [_out("RM-1", 100, 10, idx=1), _in("FG-1", 10, -100, idx=7, finished=1)]
		self.assertEqual(_classify(rows, header)["i1_status"], "NOT_I1")


class TestI1Planner(unittest.TestCase):
	def test_ready_i1_row_is_eligible(self):
		decision = evaluate_row(
			{
				"topic": TOPIC_I1,
				"repair_class": I1_NEGATIVE_RATE_REPAIR,
				"i1_status": I1_READY,
				"voucher": "STE-1",
				"sql_updates": 3,
				"replay_count": 2,
				"proposed_rate": 357775.0,
			}
		)
		self.assertEqual(decision["planner_status"], PLAN_READY_I1)
		self.assertTrue(decision["eligible"])
		self.assertGreater(decision["sql_updates"], 0)

	def test_ready_i1_without_positive_rate_is_refused(self):
		decision = evaluate_row(
			{
				"topic": TOPIC_I1,
				"repair_class": I1_NEGATIVE_RATE_REPAIR,
				"i1_status": I1_READY,
				"voucher": "STE-1",
				"sql_updates": 3,
				"proposed_rate": 0,
			}
		)
		self.assertFalse(decision["eligible"])

	def test_waiting_i1_is_not_eligible(self):
		decision = evaluate_row(
			{
				"topic": TOPIC_I1,
				"repair_class": I1_NEGATIVE_RATE_REPAIR,
				"i1_status": I1_WAITING,
				"voucher": "STE-1",
				"blocked_item": "SCRAP-9",
			}
		)
		self.assertEqual(decision["planner_status"], PLAN_WAITING_I1)
		self.assertFalse(decision["eligible"])
		self.assertEqual(decision["required_prerequisite"], "SCRAP-9")


class TestFailedRIVRoutesToI1(unittest.TestCase):
	def test_valuation_integrity_with_i1_root_waits_on_that_root(self):
		"""The 5.2.19 behaviour parked these as MANUAL; they now name a repairable root."""
		decision = evaluate_row(
			{
				"topic": "FAILED_RIV",
				"riv_name": "RIV-1",
				"riv_status": "VALUATION_INTEGRITY",
				"i1_root": "STE-1",
			}
		)
		self.assertEqual(decision["planner_status"], PLAN_WAITING_I1)
		self.assertEqual(decision["required_prerequisite"], "STE-1")
		self.assertFalse(decision["eligible"])

	def test_valuation_integrity_without_i1_root_stays_manual(self):
		decision = evaluate_row(
			{"topic": "FAILED_RIV", "riv_name": "RIV-1", "riv_status": "VALUATION_INTEGRITY"}
		)
		self.assertEqual(decision["planner_status"], "MANUAL")
		self.assertFalse(decision["eligible"])


if __name__ == "__main__":
	unittest.main()
