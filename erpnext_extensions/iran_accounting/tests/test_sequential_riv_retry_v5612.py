# Copyright (c) 2026, ERPNext Extensions contributors
"""Resilient sequential RIV retry + reconciliation contracts (v5.6.12)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry import (
	SEQ_RETRY_DELAY_SECONDS,
	SEQ_RETRY_MAX_ATTEMPTS,
	configure_retry_clock,
	is_non_retryable_business_error,
	is_retryable_transient,
	mark_identity_done_once,
	prepare_production_session_reconcile,
	reset_retry_clock,
	run_resilient_riv_execution,
	wait_for_retry_delay,
)
from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
	DB_1020_RETRY,
	DB_DEADLOCK_RETRY,
	DB_LOCK_TIMEOUT_RETRY,
	classify_db_concurrency_error,
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


class TestRetryClassificationV5612(unittest.TestCase):
	def test_retryable_1020_1205_1213(self):
		self.assertEqual(
			classify_db_concurrency_error(
				frappe.QueryDeadlockError(
					(1020, "Record has changed since last read in table 'tabRepost Item Valuation'")
				)
			),
			DB_1020_RETRY,
		)
		self.assertEqual(
			classify_db_concurrency_error(Exception((1205, "Lock wait timeout exceeded"))),
			DB_LOCK_TIMEOUT_RETRY,
		)
		self.assertEqual(
			classify_db_concurrency_error(Exception((1213, "Deadlock found when trying to get lock"))),
			DB_DEADLOCK_RETRY,
		)
		self.assertTrue(is_retryable_transient(frappe.QueryDeadlockError((1020, "x"))))

	def test_non_retryable_i5_and_allocation(self):
		self.assertTrue(is_non_retryable_business_error("Stock valuation integrity (I5)"))
		self.assertTrue(
			is_non_retryable_business_error(
				"AllocationError: historical fractional pool_value=1.5"
			)
		)
		self.assertFalse(is_retryable_transient("Stock valuation integrity (I5)"))
		self.assertFalse(is_retryable_transient("AllocationError: empty/short pool"))

	def test_defaults(self):
		self.assertEqual(SEQ_RETRY_MAX_ATTEMPTS, 5)
		self.assertEqual(SEQ_RETRY_DELAY_SECONDS, 120)


class TestWaitAndStopV5612(unittest.TestCase):
	def tearDown(self):
		reset_retry_clock()

	def test_wait_uses_configured_delay_without_real_sleep(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_retry_delay(delay_s=120, poll_s=30)
		self.assertFalse(out["interrupted_by_stop"])
		self.assertAlmostEqual(sum(clock.sleeps), 120.0, places=3)
		self.assertGreaterEqual(len(clock.sleeps), 4)

	def test_stop_during_wait(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		flags = {"n": 0}

		def stop():
			flags["n"] += 1
			return flags["n"] >= 2

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_retry_delay(delay_s=120, stop_check=stop, poll_s=10)
		self.assertTrue(out["interrupted_by_stop"])
		self.assertLess(sum(clock.sleeps), 120.0)


class TestIdentityProgressV5612(unittest.TestCase):
	def test_mark_done_once_no_double_count(self):
		sess = {"identities_done": [], "riv_completed": [], "riv_failed": ["R1"]}
		a = mark_identity_done_once(sess, identity_key="I|W", riv_name="R1")
		self.assertTrue(a["advanced"])
		b = mark_identity_done_once(sess, identity_key="I|W", riv_name="R1")
		self.assertFalse(b["advanced"])
		self.assertEqual(sess["identities_done"], ["I|W"])
		self.assertEqual(sess["riv_completed"], ["R1"])
		self.assertEqual(sess["riv_failed"], [])
		self.assertIn("R1", sess["riv_failed_history"])


class TestResilientExecutionV5612(unittest.TestCase):
	def tearDown(self):
		reset_retry_clock()

	def _patch_riv(self, statuses: list[str], *, error_log: str = ""):
		""" successive read_riv_status results by status list (cycles last). """
		calls = {"i": 0}

		def reader(name):
			idx = min(calls["i"], len(statuses) - 1)
			calls["i"] += 1
			return SimpleNamespace(
				name=name,
				status=statuses[idx],
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				error_log=error_log,
				modified="2026-10-24",
			)

		return reader, calls

	def test_1020_then_success_on_retry(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		# read before attempt1, read after 1020, read before attempt2, read after success
		states = ["Queued", "Queued", "Queued", "Completed"]
		reader, _ = self._patch_riv(states)
		exec_calls = {"n": 0}

		def execute_fn(_name):
			exec_calls["n"] += 1
			if exec_calls["n"] == 1:
				raise frappe.QueryDeadlockError(
					(1020, "Record has changed since last read in table 'tabRepost Item Valuation'")
				)

		sess = {"identities_done": [], "riv_completed": [], "riv_failed": [], "status": "Running"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			f.db.commit.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 3},
				delay_s=120,
				max_attempts=5,
			)
		self.assertTrue(out["ok"])
		self.assertEqual(exec_calls["n"], 2)
		self.assertAlmostEqual(sum(clock.sleeps), 120.0, places=3)
		self.assertEqual(sess["identities_done"], ["I|W"])

	def test_1205_then_success(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		reader, _ = self._patch_riv(["Queued", "Queued", "Queued", "Completed"])
		n = {"c": 0}

		def execute_fn(_):
			n["c"] += 1
			if n["c"] == 1:
				raise Exception((1205, "Lock wait timeout exceeded"))

		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			f.db.commit.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 1},
				delay_s=120,
				max_attempts=5,
			)
		self.assertTrue(out["ok"])

	def test_1213_then_success(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		reader, _ = self._patch_riv(["Queued", "Queued", "Queued", "Completed"])
		n = {"c": 0}

		def execute_fn(_):
			n["c"] += 1
			if n["c"] == 1:
				raise Exception((1213, "Deadlock found when trying to get lock"))

		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			f.db.commit.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 1},
				delay_s=120,
				max_attempts=5,
			)
		self.assertTrue(out["ok"])

	def test_five_attempts_exhausted(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		reader, _ = self._patch_riv(["Queued"] * 20)

		def execute_fn(_):
			raise frappe.QueryDeadlockError((1020, "Record has changed"))

		sess = {"identities_done": [], "riv_completed": [], "riv_failed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False},
				delay_s=120,
				max_attempts=5,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("attempts_exhausted") or out.get("retry_reason"))
		# 4 waits between 5 attempts
		self.assertAlmostEqual(sum(clock.sleeps), 480.0, places=3)

	def test_completed_while_waiting_after_1020(self):
		"""Production race: 1020 raised, then RIV becomes Completed before retry execute."""
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		# First read Queued → execute raises → read after exc returns Completed
		completed = SimpleNamespace(
			name="R1",
			status="Completed",
			item_code="I",
			warehouse="W",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			error_log="1020 stale",
			modified="y",
		)
		seq = iter(
			[
				SimpleNamespace(
					name="R1",
					status="Queued",
					item_code="I",
					warehouse="W",
					posting_date="2026-01-01",
					posting_time="00:00:00",
					error_log="",
					modified="x",
				),
				completed,  # re-read after 1020
				completed,  # reconcile_completed_riv re-read
			]
		)

		def execute_fn(_):
			raise frappe.QueryDeadlockError((1020, "Record has changed"))

		sess = {"identities_done": [], "riv_completed": [], "riv_failed": ["R1"]}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=lambda n: next(seq),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 7},
				delay_s=120,
				max_attempts=5,
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out.get("reconciled_after_transient") or out.get("reconciled"))
		self.assertEqual(sess["identities_done"], ["I|W"])
		self.assertEqual(sess["riv_failed"], [])
		self.assertEqual(sum(clock.sleeps), 0.0)

	def test_in_progress_waits_without_execute(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		# Always In Progress for 2 loops then Completed
		statuses = ["In Progress", "In Progress", "Completed"]
		reader, _ = self._patch_riv(statuses)
		exec_n = {"n": 0}

		def execute_fn(_):
			exec_n["n"] += 1

		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 1},
				delay_s=120,
				max_attempts=5,
			)
		self.assertTrue(out["ok"])
		self.assertEqual(exec_n["n"], 0)
		self.assertAlmostEqual(sum(clock.sleeps), 240.0, places=3)

	def test_already_counted_completed_riv(self):
		reader, _ = self._patch_riv(["Completed"])
		sess = {
			"identities_done": ["I|W"],
			"riv_completed": ["R1"],
			"riv_failed": [],
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		):
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=lambda n: None,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 1},
				max_attempts=5,
				delay_s=120,
			)
		self.assertTrue(out["ok"])
		self.assertFalse(out.get("advanced"))
		self.assertEqual(sess["identities_done"], ["I|W"])

	def test_completed_not_yet_counted(self):
		reader, _ = self._patch_riv(["Completed"])
		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		):
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=lambda n: None,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 2},
				max_attempts=5,
				delay_s=120,
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out.get("advanced"))
		self.assertEqual(sess["identities_done"], ["I|W"])

	def test_completed_but_invalid_chain_fail_closed(self):
		reader, _ = self._patch_riv(["Completed"])
		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		):
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=lambda n: None,
				verify_chain=lambda *a, **k: {
					"dirty": True,
					"issue_count": 1,
					"issues": ["qty>0 stock_value<0"],
				},
				max_attempts=5,
				delay_s=120,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("fail_closed") or out.get("verify_failed"))
		self.assertEqual(sess["identities_done"], [])

	def test_stop_during_retry_delay(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		reader, _ = self._patch_riv(["Queued"] * 10)
		stop_after = {"n": 0}

		def stop():
			stop_after["n"] += 1
			return stop_after["n"] >= 2

		def execute_fn(_):
			raise frappe.QueryDeadlockError((1020, "Record has changed"))

		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False},
				stop_check=stop,
				delay_s=120,
				max_attempts=5,
			)
		self.assertTrue(out.get("stopped"))
		self.assertFalse(out["ok"])

	def test_non_retryable_i5(self):
		reader, _ = self._patch_riv(["Queued"])

		def execute_fn(_):
			raise Exception("Stock valuation integrity (I5)")

		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False},
				delay_s=120,
				max_attempts=5,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("non_retryable"))
		self.assertEqual(sum([]), 0)

	def test_non_retryable_allocation_error(self):
		reader, _ = self._patch_riv(["Queued"])

		def execute_fn(_):
			raise Exception("AllocationError: historical fractional pool_value=9.1")

		sess = {"identities_done": [], "riv_completed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False},
				max_attempts=5,
			)
		self.assertTrue(out.get("non_retryable"))

	def test_production_reconcile_recommendation(self):
		payload = {
			"identities_done": ["13100018|WH"] * 1,
			"riv_completed": ["1oce2p3k9j"],
			"riv_failed": ["p432aoq6v9"],
		}
		# 44 done simulation
		payload["identities_done"] = [f"x{i}|w" for i in range(44)]
		out = prepare_production_session_reconcile(
			session_payload=payload,
			riv_name="p432aoq6v9",
			item_code="13100018",
			warehouse="انبار approved اقلام بسته بندی اولیه اسپاد",
		)
		self.assertEqual(out["identities_done_count"], 44)
		self.assertTrue(out["riv_in_failed"])
		self.assertFalse(out["identity_in_done"])


class TestSessionPersistenceV5612(unittest.TestCase):
	def test_save_prefers_json_revision(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr

		from pathlib import Path
		src = Path(sr.__file__).read_text(encoding="utf-8")
		self.assertIn("revision", src)
		self.assertIn("disk_rev > cache_rev", src)
		self.assertIn("run_resilient_riv_execution", src)

	def test_executor_lock_refuses_second_session(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr

		fake_cache = {}

		class Cache:
			def get_value(self, k):
				return fake_cache.get(k)

			def set_value(self, k, v, expires_in_sec=None):
				fake_cache[k] = v

			def delete_value(self, k):
				fake_cache.pop(k, None)

		with mock.patch.object(sr.frappe, "cache", return_value=Cache()), mock.patch.object(
			sr.frappe, "throw", side_effect=Exception("dup")
		):
			sr._acquire_executor_lock("HRC-X", "s1")
			with self.assertRaises(Exception):
				sr._acquire_executor_lock("HRC-X", "s2")


class TestNoRegressionPhysicalTwoSliceV5612(unittest.TestCase):
	def test_allocator_still_refuses_fractional_pool(self):
		from decimal import Decimal

		from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
			AllocationError,
			allocate_physical_two_slice,
		)

		with self.assertRaises(AllocationError):
			allocate_physical_two_slice(10, Decimal("100.5"), 1)

	def test_historical_repair_module_present(self):
		from erpnext_extensions.iran_accounting.domain import historical_fractional_sabe_repair as m

		self.assertTrue(hasattr(m, "integerize_bundle_sabe"))
		self.assertTrue(hasattr(m, "assert_bundle_integer_irr"))


if __name__ == "__main__":
	unittest.main()
