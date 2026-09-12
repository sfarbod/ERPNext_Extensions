# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests for production posting-order DAG, simulation, classification."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from erpnext_extensions.iran_accounting.stock_posting_order import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	STATUS_CYCLE,
	STATUS_MIDNIGHT,
)
from erpnext_extensions.iran_accounting.stock_posting_order.dag import (
	CycleError,
	assign_minimum_offsets,
	topological_levels,
)
from erpnext_extensions.iran_accounting.stock_posting_order.dependency import classify_edge
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	add_seconds,
	crosses_posting_date,
	sle_sort_key,
	sort_sles,
)
from erpnext_extensions.iran_accounting.stock_posting_order.prevention import (
	ensure_dependent_stock_posting_after,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import (
	classify_valuation_impact,
	running_qty,
	simulate_repair,
)


def _sle(**kw):
	return kw


class TestOrderingTieBreak(unittest.TestCase):
	def test_creation_tie_break_not_insertion_name(self):
		a = _sle(posting_datetime="2026-06-21 18:01:20", creation="2026-09-12 05:00:00", name="Z")
		b = _sle(posting_datetime="2026-06-21 18:01:20", creation="2026-09-12 01:00:00", name="A")
		ordered = sort_sles([a, b])
		self.assertEqual(ordered[0]["creation"], "2026-09-12 01:00:00")
		self.assertLess(sle_sort_key(b), sle_sort_key(a))


class TestDependencyClassification(unittest.TestCase):
	def test_unrelated_same_item_same_t_not_paired(self):
		conf, reason = classify_edge(
			{"item_code": "X", "warehouse": "W", "batch_no": ""},
			{"item_code": "X", "warehouse": "W", "batch_no": ""},
			same_work_order=False,
			same_job_card=False,
			against_stock_entry=False,
			same_batch=False,
			inbound_purpose="Material Receipt",
			outbound_purpose="Material Transfer",
		)
		self.assertEqual(conf, CONFIDENCE_AMBIGUOUS)

	def test_same_item_different_warehouse_not_paired(self):
		conf, _ = classify_edge(
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B1"},
			{"item_code": "X", "warehouse": "FG", "batch_no": "B1"},
			same_work_order=True,
			same_job_card=True,
			against_stock_entry=False,
			same_batch=True,
			inbound_purpose="Material Transfer for Manufacture",
			outbound_purpose="Manufacture",
		)
		self.assertEqual(conf, CONFIDENCE_AMBIGUOUS)

	def test_same_item_warehouse_different_batch_not_paired(self):
		conf, _ = classify_edge(
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B1"},
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B2"},
			same_work_order=True,
			same_job_card=True,
			against_stock_entry=False,
			same_batch=False,
			inbound_purpose="Material Transfer for Manufacture",
			outbound_purpose="Manufacture",
		)
		self.assertEqual(conf, CONFIDENCE_AMBIGUOUS)

	def test_same_batch_exact_job_card_paired(self):
		conf, reason = classify_edge(
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B1"},
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B1"},
			same_work_order=True,
			same_job_card=True,
			against_stock_entry=False,
			same_batch=True,
			inbound_purpose="Material Transfer for Manufacture",
			outbound_purpose="Manufacture",
		)
		self.assertEqual(conf, CONFIDENCE_EXACT)
		self.assertIn("same_job_card", reason)

	def test_same_batch_cross_wo_is_likely(self):
		conf, _ = classify_edge(
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B1"},
			{"item_code": "X", "warehouse": "WIP", "batch_no": "B1"},
			same_work_order=False,
			same_job_card=False,
			against_stock_entry=False,
			same_batch=True,
			inbound_purpose="Material Transfer for Manufacture",
			outbound_purpose="Manufacture",
		)
		self.assertEqual(conf, CONFIDENCE_LIKELY)


class TestDagOffsets(unittest.TestCase):
	def test_in_t_out_t_becomes_t_plus_1(self):
		t = datetime(2026, 6, 21, 18, 1, 45)
		plan = assign_minimum_offsets(
			{"IN": t, "OUT": t},
			[("IN", "OUT")],
		)
		self.assertTrue(plan["ok"])
		self.assertEqual(plan["proposed"]["IN"], "2026-06-21 18:01:45")
		self.assertEqual(plan["proposed"]["OUT"], "2026-06-21 18:01:46")

	def test_already_t_plus_1_unchanged(self):
		t = datetime(2026, 6, 21, 18, 1, 45)
		plan = assign_minimum_offsets(
			{"IN": t, "OUT": add_seconds(t, 1)},
			[("IN", "OUT")],
		)
		self.assertEqual(plan["moves"], [])
		self.assertEqual(plan["proposed"]["OUT"], "2026-06-21 18:01:46")

	def test_three_stage_chain(self):
		t = datetime(2026, 6, 21, 18, 0, 0)
		plan = assign_minimum_offsets(
			{"A": t, "B": t, "C": t},
			[("A", "B"), ("B", "C")],
		)
		self.assertEqual(plan["proposed"]["A"], "2026-06-21 18:00:00")
		self.assertEqual(plan["proposed"]["B"], "2026-06-21 18:00:01")
		self.assertEqual(plan["proposed"]["C"], "2026-06-21 18:00:02")

	def test_cycle_blocked(self):
		t = datetime(2026, 6, 21, 18, 0, 0)
		plan = assign_minimum_offsets({"A": t, "B": t}, [("A", "B"), ("B", "A")])
		self.assertFalse(plan["ok"])
		self.assertEqual(plan["status"], STATUS_CYCLE)
		with self.assertRaises(CycleError):
			topological_levels([("A", "B"), ("B", "A")])

	def test_midnight_manual_review(self):
		t = datetime(2026, 6, 21, 23, 59, 59)
		plan = assign_minimum_offsets({"IN": t, "OUT": t}, [("IN", "OUT")])
		self.assertFalse(plan["ok"])
		self.assertEqual(plan["status"], STATUS_MIDNIGHT)
		self.assertTrue(crosses_posting_date(t, add_seconds(t, 1)))

	def test_unrelated_occupied_slot_skips_to_t_plus_2(self):
		t = datetime(2026, 6, 21, 18, 0, 0)
		key = ("X", "WIP", "")
		plan = assign_minimum_offsets(
			{"IN": t, "OUT": t},
			[("IN", "OUT")],
			occupied={key: {add_seconds(t, 1)}},
			stock_key_of={"IN": key, "OUT": key},
		)
		self.assertEqual(plan["proposed"]["OUT"], "2026-06-21 18:00:02")


class TestQuantitySimulation(unittest.TestCase):
	def _pair(self, inverted=True):
		# opening 0, inbound +10, outbound -10
		inn = _sle(
			voucher_no="IN",
			actual_qty=10,
			posting_datetime="2026-06-21 18:01:45",
			creation="2026-09-12 05:00:00",
			name="sle-in",
			incoming_rate=100,
			valuation_rate=100,
		)
		out = _sle(
			voucher_no="OUT",
			actual_qty=-10,
			posting_datetime="2026-06-21 18:01:45",
			creation="2026-09-12 01:00:00",
			name="sle-out",
			incoming_rate=0,
			valuation_rate=100,
		)
		return [out, inn] if inverted else [inn, out]

	def test_min_running_qty_fixed_and_final_unchanged(self):
		sles = self._pair(inverted=True)
		current = running_qty(sorted(sles, key=lambda r: (r["posting_datetime"], r["creation"])))
		self.assertEqual(current["min_qty"], -10)
		self.assertEqual(current["final_qty"], 0)
		sim = simulate_repair(sles, {"IN": "2026-06-21 18:01:45", "OUT": "2026-06-21 18:01:46"})
		self.assertTrue(sim["eligible"])
		self.assertEqual(sim["proposed"]["min_qty"], 0)
		self.assertEqual(sim["proposed"]["final_qty"], sim["current"]["final_qty"])
		self.assertTrue(sim["final_qty_unchanged"])

	def test_real_insufficient_stock_no_repair(self):
		sles = [
			_sle(
				voucher_no="IN",
				actual_qty=4,
				posting_datetime="2026-06-21 18:01:45",
				creation="2",
				name="a",
			),
			_sle(
				voucher_no="OUT",
				actual_qty=-10,
				posting_datetime="2026-06-21 18:01:45",
				creation="1",
				name="b",
			),
		]
		sim = simulate_repair(
			sles,
			{"IN": "2026-06-21 18:01:45", "OUT": "2026-06-21 18:01:46"},
		)
		self.assertFalse(sim["eligible"])
		self.assertTrue(sim["insufficient_stock"])

	def test_valuation_impact_detection(self):
		cur = [
			_sle(voucher_no="OUT", actual_qty=-10, incoming_rate=0, valuation_rate=50),
			_sle(voucher_no="IN", actual_qty=10, incoming_rate=100, valuation_rate=100),
		]
		prop = list(reversed(cur))
		self.assertEqual(classify_valuation_impact(cur, prop), "VALUATION-IMPACTING")
		same_rate = [
			_sle(voucher_no="OUT", actual_qty=-10, incoming_rate=0, valuation_rate=100),
			_sle(voucher_no="IN", actual_qty=10, incoming_rate=100, valuation_rate=100),
		]
		self.assertEqual(
			classify_valuation_impact(same_rate, list(reversed(same_rate))),
			"QUANTITY-ONLY",
		)


class TestPreventionHelper(unittest.TestCase):
	def test_ensure_after_bumps_same_second(self):
		class SE:
			def __init__(self, name, posting_date, posting_time, items=None):
				self.doctype = "Stock Entry"
				self.name = name
				self.posting_date = posting_date
				self.posting_time = posting_time
				self.job_card = "JC-1"
				self.work_order = "WO-1"
				self.items = items or []
				self.set_posting_time = 0

			def get(self, k, default=None):
				return getattr(self, k, default)

			def reload(self):
				pass

		prereq = SE("IN", "2026-06-21", "18:01:45")
		dep = SE("OUT", "2026-06-21", "18:01:45")
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.prevention._as_doc",
			side_effect=lambda v, doctype="Stock Entry": v,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.prevention.lock_production_chain"
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.prevention._occupied_same_stock_seconds",
			return_value=set(),
		):
			result = ensure_dependent_stock_posting_after(prereq, dep, lock=True)
		self.assertTrue(result["changed"])
		self.assertEqual(dep.posting_time, "18:01:46")
		self.assertEqual(dep.set_posting_time, 1)

	def test_already_after_unchanged(self):
		class SE:
			def __init__(self, name, posting_date, posting_time):
				self.doctype = "Stock Entry"
				self.name = name
				self.posting_date = posting_date
				self.posting_time = posting_time
				self.job_card = "JC-1"
				self.work_order = "WO-1"
				self.items = []

			def get(self, k, default=None):
				return getattr(self, k, default)

			def reload(self):
				pass

		prereq = SE("IN", "2026-06-21", "18:01:45")
		dep = SE("OUT", "2026-06-21", "18:01:46")
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.prevention._as_doc",
			side_effect=lambda v, doctype="Stock Entry": v,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.prevention.lock_production_chain"
		):
			result = ensure_dependent_stock_posting_after(prereq, dep, lock=False)
		self.assertFalse(result["changed"])
		self.assertEqual(dep.posting_time, "18:01:46")


class TestConcurrencySlots(unittest.TestCase):
	def test_second_dependent_takes_next_free_second(self):
		t = datetime(2026, 6, 21, 18, 0, 0)
		key = ("X", "WIP", "")
		# first dependent claimed T+1
		plan = assign_minimum_offsets(
			{"IN": t, "OUT1": t, "OUT2": t},
			[("IN", "OUT1"), ("IN", "OUT2")],
			stock_key_of={"IN": key, "OUT1": key, "OUT2": key},
		)
		times = {plan["proposed"]["OUT1"], plan["proposed"]["OUT2"]}
		self.assertIn("2026-06-21 18:00:01", times)
		self.assertIn("2026-06-21 18:00:02", times)
		self.assertEqual(len(times), 2)
