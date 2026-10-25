# Copyright (c) 2026, ERPNext Extensions contributors
"""Sequential RIV queue-aware waiting (v5.6.14)."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait import (
	SEQ_QUEUE_POLL_SECONDS,
	configure_queue_wait_clock,
	count_active_rivs,
	reset_queue_wait_clock,
	wait_for_riv_queue_drain,
)


class _FakeClock:
	def __init__(self):
		self.now = 0.0
		self.sleeps: list[float] = []

	def time(self) -> float:
		return self.now

	def sleep(self, s: float) -> None:
		self.sleeps.append(float(s))
		self.now += float(s)


class TestQueueWaitBasicsV5614(unittest.TestCase):
	def tearDown(self):
		reset_queue_wait_clock()

	def test_default_poll_interval(self):
		self.assertEqual(SEQ_QUEUE_POLL_SECONDS, 30)

	def test_zero_active_immediate(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		logs: list[str] = []
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(log=logs.append, poll_s=30)
		self.assertTrue(out["ok"])
		self.assertFalse(out["waited"])
		self.assertEqual(clock.sleeps, [])

	def test_one_queued_then_drain(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		counts = iter([1, 1, 0])
		sess: dict = {"session_id": "s1", "status": "Running"}
		saves: list[dict] = []

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			side_effect=lambda: next(counts),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[{"name": "R1", "status": "Queued"}],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(
				sess=sess,
				save_session=lambda s: saves.append(dict(s)),
				poll_s=30,
				log=lambda m: None,
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out["waited"])
		self.assertEqual(len(clock.sleeps), 2)
		self.assertAlmostEqual(sum(clock.sleeps), 60.0)
		self.assertEqual(sess["status"], "Running")
		self.assertTrue(any(s.get("status") == "Waiting for RIV Queue" for s in saves))

	def test_in_progress_then_drain(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		counts = iter([1, 0])
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			side_effect=lambda: next(counts),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[{"name": "R1", "status": "In Progress"}],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(poll_s=30, log=lambda m: None)
		self.assertTrue(out["ok"])
		self.assertEqual(len(clock.sleeps), 1)

	def test_two_active_decrease_to_zero(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		counts = iter([2, 1, 0])
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			side_effect=lambda: next(counts),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[{"name": "A", "status": "Queued"}, {"name": "B", "status": "In Progress"}],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(poll_s=30, log=lambda m: None)
		self.assertTrue(out["ok"])
		self.assertEqual(out["max_active_seen"], 2)
		self.assertEqual(len(clock.sleeps), 2)

	def test_extended_wait_many_polls(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		# 20 busy polls then clear
		seq = [3] * 20 + [0]
		idx = {"i": 0}

		def count():
			v = seq[min(idx["i"], len(seq) - 1)]
			idx["i"] += 1
			return v

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			side_effect=count,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(poll_s=30, log=lambda m: None)
		self.assertTrue(out["ok"])
		self.assertEqual(len(clock.sleeps), 20)
		self.assertAlmostEqual(sum(clock.sleeps), 600.0)

	def test_stop_during_wait(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		flags = {"n": 0}

		def stop():
			flags["n"] += 1
			return flags["n"] >= 2

		sess: dict = {"status": "Running"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			return_value=2,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(
				sess=sess,
				save_session=lambda s: None,
				stop_check=stop,
				poll_s=30,
				log=lambda m: None,
			)
		self.assertTrue(out.get("stopped"))
		self.assertFalse(out["ok"])
		self.assertEqual(sess["status"], "Paused")

	def test_lock_lost_during_wait(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		owns = {"ok": True}

		def lock_ok():
			# First check ok, after first sleep lose lock
			if clock.sleeps:
				return False
			return owns["ok"]

		sess: dict = {"status": "Running"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			return_value=1,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(
				sess=sess,
				save_session=lambda s: None,
				lock_ok=lock_ok,
				poll_s=30,
				log=lambda m: None,
			)
		self.assertTrue(out.get("lock_lost"))
		self.assertEqual(sess["status"], "Paused")

	def test_heartbeat_called_while_waiting(self):
		clock = _FakeClock()
		configure_queue_wait_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		beats = {"n": 0}
		counts = iter([1, 0])
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.count_active_rivs",
			side_effect=lambda: next(counts),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.list_active_rivs",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_riv_queue_drain(
				heartbeat=lambda: beats.__setitem__("n", beats["n"] + 1),
				poll_s=30,
				log=lambda m: None,
			)
		self.assertTrue(out["ok"])
		self.assertGreaterEqual(beats["n"], 1)


class TestSequentialWiringV5614(unittest.TestCase):
	def test_execute_one_identity_waits_not_throws(self):
		from pathlib import Path

		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr
		from erpnext_extensions.iran_accounting.historical_rebuild import (
			sequential_riv_queue_wait as qw,
		)

		src = Path(sr.__file__).read_text(encoding="utf-8")
		self.assertIn("_wait_until_riv_queue_clear", src)
		self.assertNotIn('frappe.throw(f"Active RIV present', src)
		self.assertIn("Waiting for RIV Queue", Path(qw.__file__).read_text(encoding="utf-8"))

	def test_no_regression_retry_barrier_present(self):
		from pathlib import Path

		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_riv_retry as retry

		src = Path(retry.__file__).read_text(encoding="utf-8")
		self.assertIn("final_completion_barrier", src)
		self.assertIn("SEQ_RETRY_DELAY_SECONDS = 120", src)

	def test_i5_still_non_retryable(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry import (
			is_non_retryable_business_error,
		)

		self.assertTrue(is_non_retryable_business_error("Stock valuation integrity (I5)"))


class TestCountActiveUsesDbStatusesV5614(unittest.TestCase):
	def test_count_filters_queued_in_progress_only(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait.frappe"
		) as f:
			f.db.count.return_value = 2
			n = count_active_rivs()
			self.assertEqual(n, 2)
			args, kwargs = f.db.count.call_args
			self.assertEqual(args[0], "Repost Item Valuation")
			filt = args[1] if len(args) > 1 else kwargs.get("filters") or kwargs
			# frappe.db.count(doctype, filters)
			self.assertEqual(filt["docstatus"], 1)
			self.assertEqual(filt["status"], ("in", ["Queued", "In Progress"]))


if __name__ == "__main__":
	unittest.main()
