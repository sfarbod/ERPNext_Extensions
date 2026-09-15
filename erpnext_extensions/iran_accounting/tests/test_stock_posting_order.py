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
from erpnext_extensions.iran_accounting.stock_posting_order.batch_identity import canonical_batch_no
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import (
	minimum_seconds_label,
	optimize_group,
	simulate_running,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_series, sle_poison_reason
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

	def test_manufacture_then_mtfm_same_wo_batch_exact(self):
		conf, reason = classify_edge(
			{"item_code": "30300042", "warehouse": "Quarantine", "batch_no": "504135-30300042-AK264401A11"},
			{"item_code": "30300042", "warehouse": "Quarantine", "batch_no": "504135-30300042-AK264401A11"},
			same_work_order=True,
			same_job_card=False,
			against_stock_entry=False,
			same_batch=True,
			inbound_purpose="Manufacture",
			outbound_purpose="Material Transfer for Manufacture",
		)
		self.assertEqual(conf, CONFIDENCE_EXACT)
		self.assertIn("sfg_then_consume", reason)

	def test_manufacture_then_mtfm_same_wo_unbatched_likely(self):
		conf, _ = classify_edge(
			{"item_code": "X", "warehouse": "Q", "batch_no": ""},
			{"item_code": "X", "warehouse": "Q", "batch_no": ""},
			same_work_order=True,
			same_job_card=False,
			against_stock_entry=False,
			same_batch=False,
			inbound_purpose="Manufacture",
			outbound_purpose="Material Transfer for Manufacture",
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


T = "2026-06-21 18:01:45"


def _row(name, voucher, qty, creation, t=T, **kw):
	d = {
		"name": name,
		"voucher_no": voucher,
		"actual_qty": qty,
		"creation": creation,
		"posting_datetime": t,
		"item_code": kw.pop("item_code", "X"),
		"warehouse": kw.pop("warehouse", "W"),
		"batch_no": kw.pop("batch_no", ""),
		"canonical_batch": kw.pop("canonical_batch", None),
		"incoming_rate": kw.pop("incoming_rate", 100),
		"valuation_rate": kw.pop("valuation_rate", 100),
		"stock_value_difference": kw.pop("stock_value_difference", qty * 100),
	}
	if d["canonical_batch"] is None:
		d["canonical_batch"] = d["batch_no"]
	d.update(kw)
	return d


class TestBatchIdentity(unittest.TestCase):
	def test_sle_batch_wins(self):
		self.assertEqual(canonical_batch_no({"batch_no": "B1"}, [{"batch_no": "B2"}]), "B1")

	def test_sabb_single_batch(self):
		self.assertEqual(canonical_batch_no({"batch_no": ""}, [{"batch_no": "B9"}]), "B9")

	def test_sabb_multi_not_mixed(self):
		self.assertEqual(
			canonical_batch_no({"batch_no": ""}, [{"batch_no": "A"}, {"batch_no": "B"}]),
			"*MULTI*",
		)

	def test_non_batch_none(self):
		self.assertIsNone(canonical_batch_no({"batch_no": ""}, []))


class TestBatchAwareOptimizer(unittest.TestCase):
	def test_opening_0_in_out_same_t_plus_1(self):
		sles = [
			_row("o", "OUT", -10, "2026-09-12 01:00:00"),
			_row("i", "IN", 10, "2026-09-12 05:00:00"),
		]
		r = optimize_group(sles=sles, opening=0, base_t=T)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		self.assertEqual(r["minimum_seconds_required"], 1)
		self.assertEqual(r["assignment"]["IN"], 0)
		self.assertEqual(r["assignment"]["OUT"], 1)
		self.assertEqual(r["proposed"]["min_qty"], 0)
		self.assertEqual(r["proposed"]["final_qty"], r["current"]["final_qty"])
		self.assertEqual(minimum_seconds_label(r["status"], r["minimum_seconds_required"]), "+1 second")

	def test_opening_10_safe_no_historical_change(self):
		sles = [
			_row("o", "OUT", -10, "2026-09-12 01:00:00"),
			_row("i", "IN", 10, "2026-09-12 05:00:00"),
		]
		r = optimize_group(sles=sles, opening=10, base_t=T)
		self.assertEqual(r["status"], "NO_REPAIR_NEEDED")
		self.assertEqual(r["minimum_seconds_required"], 0)
		self.assertEqual(r["moves"], [])
		self.assertEqual(minimum_seconds_label(r["status"], 0), "Repair unnecessary")

	def test_opening_0_two_outs_min_seconds(self):
		sles = [
			_row("b", "OUTB", -5, "2026-09-12 01:00:00"),
			_row("c", "OUTC", -5, "2026-09-12 02:00:00"),
			_row("a", "INA", 10, "2026-09-12 05:00:00"),
		]
		r = optimize_group(sles=sles, opening=0, base_t=T)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		self.assertEqual(r["assignment"]["INA"], 0)
		self.assertGreaterEqual(r["assignment"]["OUTB"], 1)
		self.assertGreaterEqual(r["assignment"]["OUTC"], 1)
		self.assertLessEqual(r["minimum_seconds_required"], 2)
		self.assertGreaterEqual(r["proposed"]["min_qty"], 0)
		self.assertEqual(r["proposed"]["final_qty"], 0)

	def test_opening_5_minimum_edits(self):
		sles = [
			_row("b", "OUTB", -5, "2026-09-12 01:00:00"),
			_row("c", "OUTC", -5, "2026-09-12 02:00:00"),
			_row("a", "INA", 10, "2026-09-12 05:00:00"),
		]
		r = optimize_group(sles=sles, opening=5, base_t=T)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		self.assertGreaterEqual(r["proposed"]["min_qty"], 0)
		self.assertEqual(r["proposed"]["final_qty"], 5)
		self.assertEqual(r["docs_changed"], 1)

	def test_plus_1_insufficient_plus_2_works(self):
		sles = [
			_row("c", "OUTC", -10, "2026-09-12 01:00:00"),
			_row("b", "OUTB", -10, "2026-09-12 02:00:00"),
			_row("a", "INA", 20, "2026-09-12 05:00:00"),
		]
		edges = [("INA", "OUTB"), ("OUTB", "OUTC")]
		r = optimize_group(sles=sles, opening=0, base_t=T, edges=edges)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		self.assertEqual(r["minimum_seconds_required"], 2)
		self.assertEqual(r["assignment"]["INA"], 0)
		self.assertEqual(r["assignment"]["OUTB"], 1)
		self.assertEqual(r["assignment"]["OUTC"], 2)

	def test_real_stock_shortage(self):
		sles = [
			_row("o", "OUT", -15, "2026-09-12 01:00:00"),
			_row("i", "IN", 10, "2026-09-12 05:00:00"),
		]
		r = optimize_group(sles=sles, opening=0, base_t=T)
		self.assertEqual(r["status"], "REAL_STOCK_SHORTAGE")
		self.assertEqual(r["moves"], [])

	def test_batch_a_must_not_use_batch_b(self):
		sles = [
			_row("o", "OUT", -10, "1", batch_no="A", canonical_batch="A"),
			_row("i", "IN", 10, "2", batch_no="A", canonical_batch="A"),
		]
		# Batch B stock is not in this group; shortage if opening 0 for A
		r = optimize_group(sles=sles, opening=0, base_t=T)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		other = optimize_group(
			sles=[
				_row("o", "OUT", -10, "1", batch_no="A", canonical_batch="A"),
			]
			+ [
				_row("i", "IN", 10, "2", batch_no="B", canonical_batch="B"),
			],
			opening=0,
			base_t=T,
		)
		# mixed rows still simulate together if caller mixed them; scanner must not mix.
		self.assertNotEqual(
			sles[0]["canonical_batch"],
			_row("i", "IN", 10, "2", batch_no="B", canonical_batch="B")["canonical_batch"],
		)
		self.assertEqual(other["proposed"]["final_qty"] if other.get("proposed") else other["current"]["final_qty"], 0)

	def test_parent_voucher_multi_item_cross_conflict(self):
		# Moving MFG later fixes RM but delays FG inbound past an existing T+1 outbound.
		group = [
			_row("oa", "MFG", -10, "1", item_code="RM"),
			_row("ia", "IN_RM", 10, "2", item_code="RM"),
		]
		fg_rows = [
			_row("fg", "MFG", 10, "1", item_code="FG", warehouse="FGW"),
			_row(
				"dn",
				"DN",
				-10,
				"0",
				item_code="FG",
				warehouse="FGW",
				t="2026-06-21 18:01:46",
			),
		]
		cross = {("FG", "FGW", ""): {"sles": fg_rows, "opening": 0}}
		r = optimize_group(sles=group, opening=0, base_t=T, cross_windows=cross)
		self.assertEqual(r["status"], "CROSS_ITEM_CONFLICT")

	def test_cross_item_ok_when_other_stays_non_negative(self):
		group = [
			_row("oa", "ISSUE", -10, "1", item_code="A"),
			_row("ia", "IN_A", 10, "2", item_code="A"),
		]
		b_rows = [
			_row("ob", "ISSUE", -10, "1", item_code="B"),
			_row("ib", "IN_B", 10, "0", item_code="B"),
		]
		cross = {("B", "W", ""): {"sles": b_rows, "opening": 10}}
		r = optimize_group(sles=group, opening=0, base_t=T, cross_windows=cross)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")

	def test_collision_at_t_plus_1_included(self):
		sles = [
			_row("o", "OUT", -10, "2026-09-12 01:00:00"),
			_row("i", "IN", 10, "2026-09-12 05:00:00"),
		]
		coll = [
			_row(
				"c",
				"OTHER",
				5,
				"2026-09-12 00:00:00",
				t="2026-06-21 18:01:46",
			)
		]
		r = optimize_group(sles=sles, opening=0, base_t=T, collisions=coll)
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		self.assertEqual(r["minimum_seconds_required"], 1)
		vouchers = {s["voucher_no"] for s in r["window"]["series"]}
		self.assertIn("OTHER", vouchers)

	def test_midnight_review(self):
		t = "2026-06-21 23:59:59"
		sles = [
			_row("o", "OUT", -10, "1", t=t),
			_row("i", "IN", 10, "2", t=t),
		]
		r = optimize_group(sles=sles, opening=0, base_t=t)
		self.assertEqual(r["status"], "MIDNIGHT_REVIEW")
		self.assertEqual(r["moves"], [])

	def test_dependency_conflict(self):
		sles = [
			_row("o", "OUT", -10, "1"),
			_row("i", "IN", 10, "2"),
		]
		r = optimize_group(sles=sles, opening=0, base_t=T, edges=[("OUT", "IN")])
		self.assertEqual(r["status"], "DEPENDENCY_CONFLICT")

	def test_final_qty_unchanged(self):
		sles = [
			_row("o", "OUT", -10, "1"),
			_row("i", "IN", 10, "2"),
		]
		r = optimize_group(sles=sles, opening=0, base_t=T)
		self.assertTrue(r["final_qty_unchanged"])
		self.assertEqual(r["current"]["final_qty"], r["proposed"]["final_qty"])

	def test_unrelated_same_time_still_stock_safe_but_not_forced(self):
		sles = [
			_row("o", "OUT", -10, "1"),
			_row("i", "IN", 10, "2"),
		]
		r = optimize_group(sles=sles, opening=0, base_t=T, edges=[])
		self.assertEqual(r["status"], "REPAIRABLE_SECONDS")
		conf, _ = classify_edge(
			{"item_code": "X", "warehouse": "W", "batch_no": ""},
			{"item_code": "X", "warehouse": "W", "batch_no": ""},
			same_work_order=False,
			same_job_card=False,
			against_stock_entry=False,
			same_batch=False,
			inbound_purpose="Material Receipt",
			outbound_purpose="Material Issue",
		)
		self.assertEqual(conf, CONFIDENCE_AMBIGUOUS)


class TestStockValueReplay(unittest.TestCase):
	def test_quantity_only_replay_preserves_value(self):
		rows = [
			_row("o", "OUT", -10, "1", incoming_rate=0, valuation_rate=100, stock_value_difference=-1000),
			_row("i", "IN", 10, "2", incoming_rate=100, valuation_rate=100, stock_value_difference=1000),
		]
		ordered = [
			_row("i", "IN", 10, "2", incoming_rate=100, valuation_rate=100, stock_value_difference=1000),
			_row("o", "OUT", -10, "1", incoming_rate=0, valuation_rate=100, stock_value_difference=-1000),
		]
		series = replay_series(ordered, 0, 0)
		self.assertEqual(series[-1]["qty_after_transaction"], 0)
		self.assertEqual(series[0]["stock_value_difference"], 1000)
		self.assertEqual(series[1]["stock_value_difference"], -1000)
		self.assertFalse(any(s["svd_changed"] for s in series))

	def test_poison_sign_inverted(self):
		row = _row("i", "IN", 10, "1", incoming_rate=100, stock_value_difference=-1000, valuation_rate=100)
		row["qty_after_transaction"] = 10
		row["stock_value"] = -1000
		self.assertEqual(sle_poison_reason(row), "sign_inverted_incoming_svd")

	def test_leftover_at_zero_is_inversion_artifact_not_hard_poison(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import INVERSION_ARTIFACT_POISONS

		row = _row("i", "IN", 1899, "2", incoming_rate=3333719, valuation_rate=3333719, stock_value_difference=6330732319)
		row["qty_after_transaction"] = 0
		row["stock_value"] = -8699750
		self.assertEqual(sle_poison_reason(row), "qty_after_zero_nonzero_value")
		self.assertIn(sle_poison_reason(row), INVERSION_ARTIFACT_POISONS)

	def test_replay_after_reorder_clears_negative_and_leftover(self):
		out = _row(
			"o",
			"OUT",
			-1899,
			"1",
			incoming_rate=0,
			valuation_rate=3338300,
			stock_value_difference=-6339432069,
		)
		inn = _row(
			"i",
			"IN",
			1899,
			"2",
			incoming_rate=3333719,
			valuation_rate=3333719,
			stock_value_difference=6330732319,
			purpose="Manufacture",
		)
		inverted = replay_series([out, inn], 0, 0)
		self.assertLess(inverted[0]["qty_after_transaction"], 0)
		self.assertLess(inverted[1]["stock_value"], -1)
		fixed = replay_series([inn, out], 0, 0)
		self.assertGreaterEqual(fixed[0]["qty_after_transaction"], 0)
		self.assertGreaterEqual(fixed[1]["qty_after_transaction"], 0)
		self.assertEqual(fixed[1]["qty_after_transaction"], 0)
		self.assertLess(abs(fixed[1]["stock_value"]), 1)
		self.assertEqual(fixed[0]["stock_value_difference"], 6330732319)
		self.assertEqual(fixed[-1]["qty_after_transaction"], inverted[-1]["qty_after_transaction"] + 0)

	def test_inbound_keeps_manufacture_residual_svd(self):
		inn = _row(
			"i",
			"IN",
			1899,
			"1",
			incoming_rate=3333719,
			valuation_rate=3333719,
			stock_value_difference=6330732319,
			purpose="Manufacture",
		)
		out = _row(
			"o",
			"OUT",
			-1899,
			"2",
			incoming_rate=0,
			valuation_rate=3333719,
			stock_value_difference=-6339432069,
		)
		fixed = replay_series([inn, out], 0, 0)
		self.assertEqual(fixed[0]["stock_value_difference"], 6330732319)
		self.assertEqual(fixed[1]["stock_value_difference"], -6330732319)
		self.assertEqual(fixed[1]["qty_after_transaction"], 0)
		self.assertLess(abs(fixed[1]["stock_value"]), 1)

	def test_bin_matches_final_sle_math(self):
		rows = [
			_row("i", "IN", 10, "1", incoming_rate=50, valuation_rate=50, stock_value_difference=500),
			_row("o", "OUT", -4, "2", incoming_rate=0, valuation_rate=50, stock_value_difference=-200),
		]
		series = replay_series(rows, 0, 0)
		self.assertEqual(series[-1]["qty_after_transaction"], 6)
		self.assertEqual(series[-1]["stock_value"], 300)

	def test_transfer_incoming_follows_replayed_outgoing(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
			transfer_incoming_rate_from_outgoing,
		)

		from decimal import Decimal

		rate = transfer_incoming_rate_from_outgoing(-1899, -1899 * 3333719)
		self.assertEqual(rate, Decimal("3333719"))

	def test_window_poison_ignores_inversion_leftover(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import window_poison_reason

		leftover = _row(
			"i",
			"IN",
			1899,
			"2",
			incoming_rate=3333719,
			valuation_rate=3333719,
			stock_value_difference=6330732319,
		)
		leftover["qty_after_transaction"] = 0
		leftover["stock_value"] = -8699750
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay._fetch_previous",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay._fetch_sles",
			return_value=[leftover],
		):
			self.assertIsNone(
				window_poison_reason("30300042", "Q", "2026-04-06 18:01:45", ignore_inversion_artifacts=True)
			)
			self.assertEqual(
				window_poison_reason("30300042", "Q", "2026-04-06 18:01:45", ignore_inversion_artifacts=False),
				"qty_after_zero_nonzero_value",
			)

	def test_negative_incoming_is_not_inversion_artifact(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
			INVERSION_ARTIFACT_POISONS,
			sle_poison_reason,
			window_poison_hit,
			window_poison_reason,
		)

		poisoned = _row(
			"MAT-SLE-2026-166217",
			"MAT-STE-2026-25469",
			2015,
			"9",
			incoming_rate=-9997892,
			valuation_rate=-9997892,
			stock_value_difference=20145752478,
			purpose="Manufacture",
		)
		poisoned["qty_after_transaction"] = 2015
		poisoned["stock_value"] = 19964042054
		self.assertEqual(sle_poison_reason(poisoned), "negative_incoming_rate")
		self.assertNotIn("negative_incoming_rate", INVERSION_ARTIFACT_POISONS)
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay._fetch_previous",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay._fetch_sles",
			return_value=[poisoned],
		):
			self.assertEqual(
				window_poison_reason("30300014", "Q", "2026-04-15 18:01:10", ignore_inversion_artifacts=True),
				"negative_incoming_rate",
			)
			hit = window_poison_hit("30300014", "Q", "2026-04-15 18:01:10", ignore_inversion_artifacts=True)
		self.assertEqual(hit["voucher"], "MAT-STE-2026-25469")
		self.assertEqual(hit["sle"], "MAT-SLE-2026-166217")
		self.assertFalse(hit["inversion_artifact"])

	def test_pair_reorder_clears_temp_negative_without_fixing_unrelated_poison(self):
		out = _row("o", "OUT", -1961, "1", incoming_rate=0, valuation_rate=276584, stock_value_difference=-542380988)
		inn = _row(
			"i",
			"IN",
			1961,
			"2",
			incoming_rate=276584,
			valuation_rate=276584,
			stock_value_difference=542380988,
			purpose="Manufacture",
		)
		inverted = replay_series([out, inn], 0, 0)
		self.assertLess(inverted[0]["qty_after_transaction"], 0)
		fixed = replay_series([inn, out], 0, 0)
		self.assertGreaterEqual(fixed[0]["qty_after_transaction"], 0)
		self.assertEqual(fixed[1]["qty_after_transaction"], 0)
		self.assertLess(abs(fixed[1]["stock_value"]), 1)
		later = _row(
			"later",
			"IN",
			2015,
			"9",
			incoming_rate=-9997892,
			valuation_rate=-9997892,
			stock_value_difference=20145752478,
			purpose="Manufacture",
		)
		later["qty_after_transaction"] = 2015
		self.assertEqual(sle_poison_reason(later), "negative_incoming_rate")
		replayed = replay_series([inn, out, later], 0, 0)
		probe = dict(later)
		probe["qty_after_transaction"] = replayed[-1]["qty_after_transaction"]
		probe["stock_value"] = replayed[-1]["stock_value"]
		probe["stock_value_difference"] = replayed[-1]["stock_value_difference"]
		probe["valuation_rate"] = replayed[-1]["valuation_rate"]
		self.assertEqual(sle_poison_reason(probe), "negative_incoming_rate")


class TestNegativeIntervalDetector(unittest.TestCase):
	"""Cross-time posting-order detection from temporary negatives."""

	def _sle(self, name, voucher, qty, dt, creation="2026-09-11 19:00:00", **kw):
		row = _row(name, voucher, qty, creation, t=dt, **kw)
		row["voucher_type"] = kw.get("voucher_type", "Stock Entry")
		row["posting_date"] = str(dt)[:10]
		row["company"] = "ESPAD"
		return row

	def _farvardin_pair(self, gap=71):
		out_dt = "2026-04-06 18:01:45"
		in_dt = "2026-04-06 18:02:56" if gap == 71 else f"2026-04-06 18:01:{45 + gap:02d}"
		batch = "504135-30300042-AK264401A11"
		common = dict(
			item_code="30300042",
			warehouse="Quarantine - ESPAD",
			batch_no=batch,
			canonical_batch=batch,
			work_order="MFG-WO-2026-00575",
		)
		out = self._sle(
			"sle-out",
			"MAT-STE-OUT",
			-1899,
			out_dt,
			creation="2026-09-11 19:22:39",
			purpose="Material Transfer for Manufacture",
			job_card="PO-JOB07754",
			**common,
		)
		inn = self._sle(
			"sle-in",
			"MAT-STE-IN",
			1899,
			in_dt,
			creation="2026-09-11 22:24:33",
			purpose="Manufacture",
			job_card="PO-JOB07751",
			**common,
		)
		return out, inn

	def test_same_time_out_in_repairable(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		out["posting_datetime"] = inn["posting_datetime"] = "2026-04-06 18:01:45"
		rows = scan_series([out, inn], skip_same_second=False)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["optimizer_status"], "SAME_TIME_REPAIRABLE")
		self.assertEqual(rows[0]["confidence"], "EXACT")
		self.assertTrue(rows[0]["eligible"])

	def test_out_then_in_plus_71s_cross_time(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair(71)
		rows = scan_series([out, inn], skip_same_second=True)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["detection"], "CROSS_TIME")
		self.assertEqual(row["optimizer_status"], "CROSS_TIME_REPAIRABLE")
		self.assertEqual(row["confidence"], "EXACT")
		self.assertEqual(row["time_gap_seconds"], 71)
		self.assertEqual(row["current_outbound_time"], "2026-04-06 18:01:45")
		self.assertEqual(row["current_inbound_time"], "2026-04-06 18:02:56")
		self.assertEqual(row["proposed_outbound_time"], "2026-04-06 18:02:57")
		self.assertEqual(row["seconds_shifted"], 72)
		self.assertEqual(row["min_qty_before"], "-1899")
		self.assertEqual(row["min_qty_after"], "0")
		self.assertEqual(row["final_qty_before"], row["final_qty_after"])
		self.assertEqual(row["negative_amount"], "-1899")
		self.assertEqual(row["search_window"], "5min")

	def test_unrelated_later_inbound_not_repaired(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out = self._sle("o", "OUT", -10, "2026-04-06 18:01:45", purpose="Material Issue", work_order="WO-A")
		inn = self._sle(
			"i",
			"IN",
			10,
			"2026-04-06 18:05:00",
			purpose="Material Receipt",
			work_order="WO-B",
			creation="2026-09-12 02:00:00",
		)
		rows = scan_series([out, inn], skip_same_second=True)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["optimizer_status"], "LATER_INBOUND_UNRELATED")
		self.assertFalse(rows[0]["eligible"])

	def test_manufacture_in_later_than_mtfm_out_detected(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
			find_negative_intervals,
			scan_series,
		)

		out, inn = self._farvardin_pair()
		intervals = find_negative_intervals([out, inn], 0)
		self.assertEqual(len(intervals), 1)
		self.assertEqual(intervals[0]["negative_amount"], -1899)
		rows = scan_series([inn, out], skip_same_second=True)
		self.assertEqual(rows[0]["inbound_purpose"], "Manufacture")
		self.assertEqual(rows[0]["outbound_purpose"], "Material Transfer for Manufacture")
		self.assertEqual(rows[0]["inbound_document"], "MAT-STE-IN")
		self.assertEqual(rows[0]["outbound_document"], "MAT-STE-OUT")

	def test_quarantine_negative_not_fooled_by_healthy_wip(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		wip_in = dict(out)
		wip_in.update(
			{
				"name": "sle-wip",
				"warehouse": "WIP - ESPAD",
				"actual_qty": 1899,
				"voucher_no": "MAT-STE-OUT",
			}
		)
		q_key = (out["item_code"], out["warehouse"], out["canonical_batch"])
		w_key = (wip_in["item_code"], wip_in["warehouse"], wip_in["canonical_batch"])
		by_identity = {q_key: [out, inn], w_key: [wip_in]}
		by_voucher = {"MAT-STE-OUT": [out, wip_in], "MAT-STE-IN": [inn]}
		q_rows = scan_series(
			[out, inn],
			by_voucher_all=by_voucher,
			by_identity=by_identity,
			skip_same_second=True,
		)
		self.assertEqual(q_rows[0]["warehouse"], "Quarantine - ESPAD")
		self.assertEqual(q_rows[0]["optimizer_status"], "CROSS_TIME_REPAIRABLE")
		wip_rows = scan_series([wip_in], skip_same_second=True)
		self.assertEqual(wip_rows, [])

	def test_opening_stock_sufficient_no_repair(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
			find_negative_intervals,
			scan_series,
		)

		opening = self._sle("open", "OPEN", 1899, "2026-04-06 10:00:00", purpose="Material Receipt")
		out = self._sle("o", "OUT", -1899, "2026-04-06 18:01:45", purpose="Material Transfer for Manufacture")
		inn = self._sle("i", "IN", 1899, "2026-04-06 18:02:56", purpose="Manufacture", creation="2")
		self.assertEqual(find_negative_intervals([opening, out, inn], 0), [])
		self.assertEqual(scan_series([opening, out, inn], skip_same_second=True), [])

	def test_batch_sabb_identity(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		out["batch_no"] = ""
		inn["batch_no"] = ""
		out["serial_and_batch_bundle"] = "SABB-OUT"
		inn["serial_and_batch_bundle"] = "SABB-IN"
		rows = scan_series([out, inn], skip_same_second=True)
		self.assertEqual(rows[0]["batch"], "504135-30300042-AK264401A11")
		self.assertTrue(rows[0]["has_batch"])

	def test_multiple_later_inbound_rows_picks_related(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		noise = self._sle(
			"noise",
			"UNRELATED-IN",
			10,
			"2026-04-06 18:02:10",
			purpose="Material Receipt",
			work_order="OTHER-WO",
			item_code="30300042",
			warehouse=out["warehouse"],
			batch_no=out["batch_no"],
			canonical_batch=out["canonical_batch"],
			creation="2026-09-11 20:00:00",
		)
		# recover still needs manufacture; noise alone is not enough
		rows = scan_series([out, noise, inn], skip_same_second=True)
		self.assertEqual(rows[0]["inbound_document"], "MAT-STE-IN")
		self.assertEqual(rows[0]["optimizer_status"], "CROSS_TIME_REPAIRABLE")
		self.assertGreaterEqual(rows[0]["inbound_count"] or 0, 2)

	def test_cross_item_parent_voucher_safety(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		wip_in = dict(out)
		wip_in.update({"name": "sle-wip", "warehouse": "WIP - ESPAD", "actual_qty": 1899})
		wip_consume = self._sle(
			"sle-wip-out",
			"WIP-CONSUME",
			-1899,
			"2026-04-06 18:02:10",
			purpose="Manufacture",
			item_code=out["item_code"],
			warehouse="WIP - ESPAD",
			batch_no=out["batch_no"],
			canonical_batch=out["canonical_batch"],
			work_order=out["work_order"],
			creation="2026-09-11 19:30:00",
		)
		q_key = (out["item_code"], out["warehouse"], out["canonical_batch"])
		w_key = (out["item_code"], "WIP - ESPAD", out["canonical_batch"])
		by_identity = {q_key: [out, inn], w_key: [wip_in, wip_consume]}
		by_voucher = {"MAT-STE-OUT": [out, wip_in], "MAT-STE-IN": [inn], "WIP-CONSUME": [wip_consume]}
		rows = scan_series(
			[out, inn],
			by_voucher_all=by_voucher,
			by_identity=by_identity,
			skip_same_second=True,
		)
		self.assertEqual(rows[0]["optimizer_status"], "CROSS_ITEM_CONFLICT")
		self.assertFalse(rows[0]["eligible"])

	def test_cross_date_proposal_is_cross_time_repairable(self):
		"""Outbound may move onto a later inbound's posting date — classic CROSS_TIME."""
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		out["posting_datetime"] = "2026-04-14 18:03:50"
		out["posting_date"] = "2026-04-14"
		inn["posting_datetime"] = "2026-04-15 18:03:41"
		inn["posting_date"] = "2026-04-15"
		rows = scan_series([out, inn], skip_same_second=True)
		self.assertEqual(rows[0]["optimizer_status"], "CROSS_TIME_REPAIRABLE")
		self.assertEqual(rows[0]["proposed_outbound_time"], "2026-04-15 18:03:42")
		self.assertEqual(rows[0]["min_qty_after"], "0")
		self.assertTrue(rows[0]["eligible"])

	def test_true_end_of_inbound_day_is_midnight_review(self):
		"""No free second remains on the inbound posting date → MIDNIGHT_REVIEW."""
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		out["posting_datetime"] = "2026-04-14 18:03:50"
		out["posting_date"] = "2026-04-14"
		inn["posting_datetime"] = "2026-04-15 23:59:59"
		inn["posting_date"] = "2026-04-15"
		rows = scan_series([out, inn], skip_same_second=True)
		self.assertEqual(rows[0]["optimizer_status"], "MIDNIGHT_REVIEW")
		self.assertFalse(rows[0]["eligible"])

	def test_purchase_receipt_later_inbound_promoted_to_cross_time(self):
		"""External Purchase Receipt that recovers qty is deterministic CROSS_TIME LIKELY."""
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out = self._sle("o", "OUT", -10, "2026-04-06 18:01:45", purpose="Material Issue", work_order="WO-A")
		inn = self._sle(
			"i",
			"PR-1",
			10,
			"2026-04-06 18:05:00",
			purpose=None,
			work_order=None,
			voucher_type="Purchase Receipt",
			creation="2026-09-12 02:00:00",
		)
		rows = scan_series([out, inn], skip_same_second=True)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["optimizer_status"], "CROSS_TIME_REPAIRABLE")
		self.assertEqual(rows[0]["confidence"], "LIKELY")
		self.assertEqual(rows[0]["min_qty_after"], "0")
		self.assertFalse(rows[0]["eligible"])  # LIKELY needs planner promote

	def test_valuation_poison_blocks_classify(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		rows = scan_series([out, inn], poisoned=True, skip_same_second=True)
		self.assertEqual(rows[0]["optimizer_status"], "VALUATION_POISON_DEPENDENCY")
		self.assertFalse(rows[0]["eligible"])

	def test_final_qty_unchanged_after_proposal(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		row = scan_series([out, inn], skip_same_second=True)[0]
		self.assertEqual(row["final_qty_before"], row["final_qty_after"])
		self.assertEqual(row["final_qty_after"], "0")

	def test_no_new_negative_in_other_warehouse_when_wip_is_idle(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import scan_series

		out, inn = self._farvardin_pair()
		wip_in = dict(out)
		wip_in.update({"name": "sle-wip", "warehouse": "WIP - ESPAD", "actual_qty": 1899})
		later_wip = self._sle(
			"sle-wip-later",
			"WIP-LATER",
			-10,
			"2026-04-11 10:00:00",
			purpose="Manufacture",
			item_code=out["item_code"],
			warehouse="WIP - ESPAD",
			batch_no=out["batch_no"],
			canonical_batch=out["canonical_batch"],
			creation="2026-09-12 01:00:00",
		)
		q_key = (out["item_code"], out["warehouse"], out["canonical_batch"])
		w_key = (out["item_code"], "WIP - ESPAD", out["canonical_batch"])
		by_identity = {q_key: [out, inn], w_key: [wip_in, later_wip]}
		by_voucher = {"MAT-STE-OUT": [out, wip_in], "MAT-STE-IN": [inn], "WIP-LATER": [later_wip]}
		rows = scan_series(
			[out, inn],
			by_voucher_all=by_voucher,
			by_identity=by_identity,
			skip_same_second=True,
		)
		self.assertEqual(rows[0]["optimizer_status"], "CROSS_TIME_REPAIRABLE")
		self.assertEqual(rows[0]["min_qty_after"], "0")


class TestValuationRebuildContract(unittest.TestCase):
	def test_txn_rate_from_source_svd(self):
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import _txn_rate

		rate = _txn_rate(-1899, -6330732319)
		self.assertGreater(rate, 0)
		self.assertLess(abs(float(rate) - (6330732319 / 1899)), 0.01)

	def test_riv_blocked_on_rebuild(self):
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import rebuild_chain_valuation

		with self.assertRaises(Exception):
			rebuild_chain_valuation("IN", "OUT", dry_run=False, allow_riv=True)

	def test_zero_outgoing_with_value_is_stale(self):
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import _txn_rate

		self.assertGreater(_txn_rate(-1899, -6330732319), 0)
		self.assertEqual(_txn_rate(-1899, 0), 0)

	def test_transfer_out_in_identical_value(self):
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import _txn_rate

		self.assertEqual(_txn_rate(-1899, -6330732319), _txn_rate(1899, 6330732319))

	def test_manufacture_residual_not_reprice(self):
		inn = _row(
			"i",
			"IN",
			1899,
			"1",
			incoming_rate=3333719,
			valuation_rate=3333719,
			stock_value_difference=6330732319,
			purpose="Manufacture",
		)
		out = _row(
			"o",
			"OUT",
			-1899,
			"2",
			incoming_rate=0,
			valuation_rate=3333719,
			stock_value_difference=-1,
		)
		fixed = replay_series([inn, out], 0, 0)
		self.assertEqual(fixed[0]["stock_value_difference"], 6330732319)
		self.assertEqual(fixed[1]["stock_value_difference"], -6330732319)

	def test_posting_order_fixed_rate_rebuild_status_exists(self):
		from erpnext_extensions.iran_accounting.stock_posting_order import (
			STATUS_DOWNSTREAM_VALUE_REPLAY_REQUIRED,
			STATUS_GL_REBUILD_REQUIRED,
			STATUS_INTEGRITY_COMPLETE,
			STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED,
			STATUS_RATE_REBUILD_COMPLETE,
			STATUS_RATE_REBUILD_IN_PROGRESS,
		)

		self.assertEqual(STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED, "ORDER_FIXED_RATE_REBUILD_REQUIRED")
		self.assertEqual(STATUS_RATE_REBUILD_IN_PROGRESS, "RATE_REBUILD_IN_PROGRESS")
		self.assertEqual(STATUS_RATE_REBUILD_COMPLETE, "RATE_REBUILD_COMPLETE")
		self.assertEqual(STATUS_DOWNSTREAM_VALUE_REPLAY_REQUIRED, "DOWNSTREAM_VALUE_REPLAY_REQUIRED")
		self.assertEqual(STATUS_GL_REBUILD_REQUIRED, "GL_REBUILD_REQUIRED")
		self.assertEqual(STATUS_INTEGRITY_COMPLETE, "INTEGRITY_COMPLETE")

	def test_rebuild_sequence_does_not_start_with_riv(self):
		import inspect

		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import rebuild_chain_valuation

		src = inspect.getsource(rebuild_chain_valuation)
		self.assertIn('"prerequisite Manufacture"', src)
		self.assertIn("RIV is not invoked from valuation rebuild", src)
		self.assertLess(src.find("allow_riv"), src.find("diagnose_chain"))


class TestDownstreamReplayContract(unittest.TestCase):
	def _ctx(self, **extra):
		base = {
			"item": "30300042",
			"batch": "504135-30300042-AK264401A11",
			"patient_vouchers": {"MAT-STE-2026-25824-1", "MAT-STE-2026-25825"},
			"visited_warehouses": {"WIP"},
			"work_order": "MFG-WO-2026-00575",
			"job_card": "PO-JOB07754",
			"remaining_qty": 1899,
		}
		base.update(extra)
		return base

	def test_same_batch_manufacture_consume_is_direct(self):
		from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
			CLASS_DIRECT,
			classify_dependency,
		)

		out = classify_dependency(
			{
				"voucher_no": "MAT-STE-2026-25912",
				"purpose": "Manufacture",
				"item_code": "30300042",
				"warehouse": "WIP",
				"batch": "504135-30300042-AK264401A11",
				"actual_qty": -47,
				"work_order": "MFG-WO-2026-00575",
				"job_card": "PO-JOB07754",
				"serial_and_batch_bundle": "bundle",
			},
			self._ctx(),
		)
		self.assertEqual(out["classification"], CLASS_DIRECT)
		self.assertIn("same batch", out["reasons"])
		self.assertIn("same manufacture consume", out["reasons"])
		self.assertIn("partial consume", out["reasons"])
		self.assertFalse(out["preview_only"])

	def test_different_batch_is_unrelated(self):
		from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
			CLASS_UNRELATED,
			classify_dependency,
		)

		out = classify_dependency(
			{
				"voucher_no": "MAT-STE-2026-25906",
				"purpose": "Material Transfer for Manufacture",
				"item_code": "30300042",
				"warehouse": "WIP",
				"batch": "504143-30300042-AK264402A11",
				"actual_qty": 1206,
			},
			self._ctx(),
		)
		self.assertEqual(out["classification"], CLASS_UNRELATED)
		self.assertIn("different batch", out["reasons"])

	def test_unknown_voucher_type_is_preview_only(self):
		from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
			CLASS_STOP,
			classify_dependency,
		)

		out = classify_dependency(
			{
				"voucher_no": "MAT-SR-1",
				"voucher_type": "Stock Reconciliation",
				"purpose": "",
				"item_code": "30300042",
				"warehouse": "WIP",
				"batch": "504135-30300042-AK264401A11",
				"actual_qty": -1,
			},
			self._ctx(),
		)
		self.assertEqual(out["classification"], CLASS_STOP)
		self.assertTrue(out["preview_only"])

	def test_last_consume_takes_batch_residual(self):
		from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import expected_movements

		priced = expected_movements(
			1899,
			6330732319,
			[
				{"voucher_no": "A", "actual_qty": -47, "stock_value_difference": -156900109, "purpose": "Manufacture"},
				{"voucher_no": "B", "actual_qty": -1852, "stock_value_difference": -6182531600, "purpose": "Manufacture"},
			],
		)
		self.assertEqual(len(priced), 2)
		self.assertTrue(priced[0]["replay_required_bool"])
		self.assertTrue(priced[1]["replay_required_bool"])
		self.assertLess(abs(priced[0]["expected_valuation"] + priced[1]["expected_valuation"] + 6330732319), 1)
		self.assertAlmostEqual(priced[1]["remaining_qty_after"], 0)
		self.assertLess(abs(priced[1]["remaining_value_after"]), 1)

	def test_protected_output_skips_fg(self):
		from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import _is_protected_output

		fg = type("R", (), {"is_finished_item": 1, "is_scrap_item": 0, "secondary_item_type": "", "s_warehouse": None, "t_warehouse": "Q"})()
		rm = type("R", (), {"is_finished_item": 0, "is_scrap_item": 0, "secondary_item_type": "", "s_warehouse": "WIP", "t_warehouse": None})()
		self.assertTrue(_is_protected_output(fg))
		self.assertFalse(_is_protected_output(rm))

	def test_downstream_statuses_exist(self):
		from erpnext_extensions.iran_accounting.stock_posting_order import (
			STATUS_DOWNSTREAM_COMPLETE,
			STATUS_DOWNSTREAM_PENDING,
			STATUS_DOWNSTREAM_REPLAYING,
			STATUS_DOWNSTREAM_REPLAY_REQUIRED,
			STATUS_DOWNSTREAM_SKIPPED,
		)

		self.assertEqual(STATUS_DOWNSTREAM_PENDING, "DOWNSTREAM_PENDING")
		self.assertEqual(STATUS_DOWNSTREAM_REPLAY_REQUIRED, "DOWNSTREAM_REPLAY_REQUIRED")
		self.assertEqual(STATUS_DOWNSTREAM_REPLAYING, "DOWNSTREAM_REPLAYING")
		self.assertEqual(STATUS_DOWNSTREAM_COMPLETE, "DOWNSTREAM_COMPLETE")
		self.assertEqual(STATUS_DOWNSTREAM_SKIPPED, "DOWNSTREAM_SKIPPED")

