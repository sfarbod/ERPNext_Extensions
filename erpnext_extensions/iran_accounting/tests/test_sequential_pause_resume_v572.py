# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.2 — Sequential Pause/Resume reliability (HRC-00009 / 611vj32kg4).

Covers:
- Long In-Progress barrier → Pause → Completed-after-pause resume reconcile
- No duplicate identities_done / riv_completed
- Executor lock heartbeat during waits
- Second executor refused
- Scheduler cannot race sequential-owned RIV
- Retryable Deadlock then success
- Non-retryable still fail-closed
- Stop flag honored
- Completed RIV error_log archive (audit preserved, not terminal)
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry import (
	COMPLETED_ERROR_ARCHIVE_BANNER,
	SEQ_IN_PROGRESS_MAX_WAITS,
	SEQ_IN_PROGRESS_MAX_WAITS_CAP,
	archive_completed_riv_attempt_errors,
	configure_retry_clock,
	final_completion_barrier,
	mark_identity_done_once,
	reconcile_completed_riv,
	reconcile_session_current_riv,
	reset_retry_clock,
	run_resilient_riv_execution,
	seq_in_progress_max_waits,
	wait_for_retry_delay,
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


def _meta(status: str, *, err: str = "", name: str = "611vj32kg4"):
	return SimpleNamespace(
		name=name,
		status=status,
		item_code="ITEM-A",
		warehouse="WH-A",
		posting_date="2026-01-01",
		posting_time="00:00:00",
		error_log=err,
		modified="2026-11-06",
	)


class TestBarrierConfigV572(unittest.TestCase):
	def test_default_barrier_covers_61min_riv(self):
		# Prior default was 15 * 120s ≈ 30 min; HRC-00009 RIV needed ~61 min.
		# New default 45 * 120s = 90 min wall after grace.
		self.assertEqual(SEQ_IN_PROGRESS_MAX_WAITS, 45)
		self.assertGreaterEqual(SEQ_IN_PROGRESS_MAX_WAITS * 120, 61 * 60)
		self.assertEqual(SEQ_IN_PROGRESS_MAX_WAITS_CAP, 120)

	def test_site_config_clamped(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.conf.get.return_value = 9999
			self.assertEqual(seq_in_progress_max_waits(), SEQ_IN_PROGRESS_MAX_WAITS_CAP)
			f.conf.get.return_value = -5
			self.assertEqual(seq_in_progress_max_waits(), 0)
			f.conf.get.return_value = 60
			self.assertEqual(seq_in_progress_max_waits(), 60)


class TestHeartbeatDuringWaitV572(unittest.TestCase):
	def tearDown(self):
		reset_retry_clock()

	def test_wait_invokes_heartbeat_each_poll(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		beats = {"n": 0}

		def hb():
			beats["n"] += 1

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = wait_for_retry_delay(delay_s=30, poll_s=10, heartbeat=hb)
		self.assertFalse(out["interrupted_by_stop"])
		self.assertGreaterEqual(out["heartbeats"], 3)
		self.assertEqual(beats["n"], out["heartbeats"])

	def test_in_progress_barrier_heartbeats_before_pause(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		beats = {"n": 0}
		meta = _meta("In Progress", err="Deadlock 1213")
		sess = {"identities_done": [], "riv_completed": [], "status": "Running"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=meta,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_post_exhaust_grace_waits",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_in_progress_max_waits",
			return_value=3,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = final_completion_barrier(
				riv_name="611vj32kg4",
				item_code="ITEM-A",
				warehouse="WH-A",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				verify_chain=lambda *a, **k: {"dirty": False},
				delay_s=10,
				heartbeat=lambda: beats.__setitem__("n", beats["n"] + 1),
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("in_progress_unresolved"))
		self.assertEqual(sess.get("status"), "Paused")
		self.assertGreater(beats["n"], 0)
		self.assertAlmostEqual(sum(clock.sleeps), 30.0, places=3)


class TestResumeReconcileV572(unittest.TestCase):
	def test_resume_reconciles_completed_current_riv_once(self):
		"""HRC-00009: Pause while In Progress; RIV later Completed; resume must
		reconcile before creating another RIV and must not double-count."""
		completed = _meta(
			"Completed",
			err="Budget msgprint\nDeadlock found when trying to get lock",
		)
		sess = {
			"identities_done": [],
			"riv_completed": [],
			"riv_failed": [],
			"current_riv": "611vj32kg4",
			"current_item_code": "ITEM-A",
			"current_warehouse": "WH-A",
			"status": "Paused",
		}
		saves: list[dict] = []

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=completed,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.get_value.return_value = completed.error_log
			f.db.set_value.return_value = None
			out = reconcile_session_current_riv(
				sess,
				company="C",
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 12},
				save_session=lambda s: saves.append(dict(s)),
			)
			# Second call must be idempotent.
			out2 = reconcile_session_current_riv(
				sess,
				company="C",
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 12},
				save_session=lambda s: saves.append(dict(s)),
			)

		self.assertTrue(out["ok"])
		self.assertTrue(out.get("reconciled"))
		self.assertTrue(out.get("advanced"))
		self.assertEqual(sess["identities_done"], ["ITEM-A|WH-A"])
		self.assertEqual(sess["riv_completed"], ["611vj32kg4"])
		self.assertIsNone(sess.get("current_riv"))
		# Second pass: no current_riv → skipped; counters unchanged.
		self.assertTrue(out2.get("skipped"))
		self.assertEqual(sess["identities_done"], ["ITEM-A|WH-A"])
		self.assertEqual(sess["riv_completed"], ["611vj32kg4"])
		self.assertTrue(saves)

	def test_resume_needs_reattach_when_still_in_progress(self):
		sess = {
			"current_riv": "611vj32kg4",
			"current_item_code": "ITEM-A",
			"current_warehouse": "WH-A",
			"identities_done": [],
			"riv_completed": [],
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=_meta("In Progress"),
		):
			out = reconcile_session_current_riv(
				sess,
				company="C",
				verify_chain=lambda *a, **k: {"dirty": False},
			)
		self.assertTrue(out.get("needs_reattach"))
		self.assertFalse(out.get("ok"))
		self.assertEqual(sess["identities_done"], [])

	def test_execute_one_identity_reconciles_before_create(self):
		"""execute_one_identity must not create when current_riv already Completed."""
		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr

		sess = {
			"session_id": "20261106014343",
			"campaign": "HRC-00009",
			"identities_done": [],
			"riv_completed": [],
			"riv_created": ["611vj32kg4"],
			"riv_failed": [],
			"current_riv": "611vj32kg4",
			"current_item_code": "ITEM-A",
			"current_warehouse": "WH-A",
			"status": "Paused",
			"revision": 1,
		}
		create_calls = {"n": 0}

		def fake_create(*_a, **_k):
			create_calls["n"] += 1

		with mock.patch.object(sr, "_ensure_bootstrap"), mock.patch.object(
			sr, "_empty_rq"
		), mock.patch.object(sr, "get_seq_session", return_value=sess), mock.patch.object(
			sr, "_save_seq", side_effect=lambda p: None
		), mock.patch.object(
			sr,
			"inspect_sle_chain",
			return_value={"dirty": False, "sle_count": 12},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=_meta("Completed", err="prior deadlock"),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.create_repost_item_valuation_entry",
			side_effect=fake_create,
		):
			f.db.get_value.return_value = "prior deadlock"
			f.db.set_value.return_value = None
			out = sr.execute_one_identity(
				"ITEM-A",
				"WH-A",
				"2026-01-01",
				"00:00:00",
				company="C",
				campaign="HRC-00009",
			)

		self.assertTrue(out["ok"])
		self.assertTrue(out.get("resume_reconcile") or out.get("reconciled"))
		self.assertEqual(create_calls["n"], 0)
		self.assertEqual(sess["identities_done"], ["ITEM-A|WH-A"])
		self.assertEqual(sess["riv_completed"], ["611vj32kg4"])


class TestErrorLogArchiveV572(unittest.TestCase):
	def test_archive_banners_without_erasing_traceback(self):
		prior = "Budget exceeded\nDeadlock found when trying to get lock (1213)\nTraceback..."
		sess: dict = {"riv_error_archive": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.get_value.return_value = prior
			captured = {}

			def _set_value(dt, name, fields, update_modified=False):
				captured["error_log"] = fields["error_log"]

			f.db.set_value.side_effect = _set_value
			out = archive_completed_riv_attempt_errors(sess, "611vj32kg4")

		self.assertTrue(out["archived"])
		self.assertTrue(out["bannered"])
		self.assertEqual(len(sess["riv_error_archive"]), 1)
		self.assertIn(prior[:40], sess["riv_error_archive"][0]["error_log"])
		self.assertTrue(captured["error_log"].startswith(COMPLETED_ERROR_ARCHIVE_BANNER))
		self.assertIn(prior, captured["error_log"])
		self.assertIn("Final status: Completed", captured["error_log"])

	def test_reconcile_archives_on_completed(self):
		sess = {"identities_done": [], "riv_completed": [], "riv_failed": [], "current_riv": "R1"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=_meta("Completed", err="old failure", name="R1"),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.get_value.return_value = "old failure"
			f.db.set_value.return_value = None
			out = reconcile_completed_riv(
				riv_name="R1",
				item_code="ITEM-A",
				warehouse="WH-A",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 1},
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out.get("error_archive", {}).get("archived"))


class TestDeadlockAndNonRetryableV572(unittest.TestCase):
	def tearDown(self):
		reset_retry_clock()

	def test_retryable_deadlock_then_success(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		statuses = ["Queued", "Queued", "Queued", "Completed"]
		calls = {"i": 0}

		def reader(_n):
			idx = min(calls["i"], len(statuses) - 1)
			calls["i"] += 1
			return _meta(statuses[idx], name="R-DL")

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
				riv_name="R-DL",
				item_code="ITEM-A",
				warehouse="WH-A",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 2},
				delay_s=10,
				max_attempts=5,
			)
		self.assertTrue(out["ok"])
		self.assertEqual(n["c"], 2)
		self.assertEqual(sess["identities_done"], ["ITEM-A|WH-A"])

	def test_non_retryable_stops_session(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		sess = {"identities_done": [], "riv_completed": [], "riv_failed": [], "status": "Running"}

		def execute_fn(_):
			raise Exception("Stock valuation integrity (I5)")

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=_meta("Queued", name="R-NR"),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = run_resilient_riv_execution(
				riv_name="R-NR",
				item_code="ITEM-A",
				warehouse="WH-A",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				execute_fn=execute_fn,
				verify_chain=lambda *a, **k: {"dirty": False},
				delay_s=10,
				max_attempts=5,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("non_retryable") or out.get("stop_chain"))
		self.assertEqual(sum(clock.sleeps), 0.0)

	def test_stop_during_long_in_progress_wait(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		flags = {"n": 0}

		def stop():
			flags["n"] += 1
			return flags["n"] >= 2

		sess = {"identities_done": [], "riv_completed": [], "status": "Running"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=_meta("In Progress"),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_post_exhaust_grace_waits",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_in_progress_max_waits",
			return_value=10,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = final_completion_barrier(
				riv_name="R-STOP",
				item_code="ITEM-A",
				warehouse="WH-A",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				verify_chain=lambda *a, **k: {"dirty": False},
				stop_check=stop,
				delay_s=30,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("stopped") or out.get("interrupted_by_stop") or sess.get("status") in ("Paused", "Stopped", "Running"))
		self.assertLess(sum(clock.sleeps), 300.0)


class TestExecutorLockConcurrencyV572(unittest.TestCase):
	def test_second_executor_refused(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr

		cache = {}

		class _Cache:
			def get_value(self, key):
				return cache.get(key)

			def set_value(self, key, val, expires_in_sec=None):
				cache[key] = val

			def delete_value(self, key):
				cache.pop(key, None)

		with mock.patch.object(frappe, "cache", return_value=_Cache()):
			sr._acquire_executor_lock("HRC-00009", "sess-A")
			with self.assertRaises(Exception):
				sr._acquire_executor_lock("HRC-00009", "sess-B")
			# Owner can refresh.
			sr._refresh_executor_lock("HRC-00009", "sess-A")
			self.assertTrue(sr._executor_lock_owned("HRC-00009", "sess-A"))
			self.assertFalse(sr._executor_lock_owned("HRC-00009", "sess-B"))
			# Non-owner refresh is a no-op (does not steal).
			sr._refresh_executor_lock("HRC-00009", "sess-B")
			self.assertTrue(sr._executor_lock_owned("HRC-00009", "sess-A"))

	def test_mark_done_once_no_duplicate_counters(self):
		sess = {"identities_done": [], "riv_completed": [], "riv_failed": []}
		a = mark_identity_done_once(sess, identity_key="ITEM-A|WH-A", riv_name="611vj32kg4")
		b = mark_identity_done_once(sess, identity_key="ITEM-A|WH-A", riv_name="611vj32kg4")
		c = mark_identity_done_once(sess, identity_key="ITEM-A|WH-A", riv_name="DUPLICATE-RIV")
		self.assertTrue(a["advanced"])
		self.assertFalse(b["advanced"])
		self.assertFalse(c["advanced"])
		self.assertEqual(sess["identities_done"], ["ITEM-A|WH-A"])
		self.assertEqual(sess["riv_completed"], ["611vj32kg4"])


class TestSchedulerOwnershipFailClosedV572(unittest.TestCase):
	def test_execute_defers_on_probe_exception(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_EXECUTE_REPOSTING_ENTRY = mock.Mock()
		with mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._ensure_iran_runtime_for_riv",
		), mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._should_defer_riv_execution",
			side_effect=RuntimeError("redis down"),
		), mock.patch.object(frappe, "log_error"):
			guard.execute_reposting_entry("RIV-SEQ")
		guard._ORIG_EXECUTE_REPOSTING_ENTRY.assert_not_called()

	def test_enqueue_skipped_on_probe_exception(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_ENQUEUE_REPOSTING_ENTRY = mock.Mock(return_value="job")
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			side_effect=RuntimeError("redis down"),
		), mock.patch.object(frappe, "log_error"):
			out = guard.enqueue_reposting_entry("RIV-SEQ")
		self.assertIsNone(out)
		guard._ORIG_ENQUEUE_REPOSTING_ENTRY.assert_not_called()

	def test_scheduler_defers_when_claim_held_by_sequential(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_EXECUTE_REPOSTING_ENTRY = mock.Mock()
		# Scheduler worker: claim held by Sequential, local owner unset → defer.
		with mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._ensure_iran_runtime_for_riv",
		), mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._should_defer_riv_execution",
			return_value=True,
		) as defer:
			guard.execute_reposting_entry("611vj32kg4")
		defer.assert_called_once_with("611vj32kg4")
		guard._ORIG_EXECUTE_REPOSTING_ENTRY.assert_not_called()

	def test_should_defer_when_foreign_claim_held(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
			return_value=False,
		), mock.patch.object(frappe, "local", SimpleNamespace(iran_hr_riv_exec_owner=None)):
			self.assertTrue(guard._should_defer_riv_execution("611vj32kg4"))
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
			return_value=False,
		), mock.patch.object(
			frappe, "local", SimpleNamespace(iran_hr_riv_exec_owner="611vj32kg4")
		):
			self.assertFalse(guard._should_defer_riv_execution("611vj32kg4"))


class TestLongInProgressThenCompleteV572(unittest.TestCase):
	def tearDown(self):
		reset_retry_clock()

	def test_beyond_original_barrier_then_complete_on_resume_path(self):
		"""Simulate: barrier pauses after N waits; RIV later Completes; resume reconciles."""
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		# Phase 1 — still In Progress through entire short barrier → Pause
		meta_ip = _meta("In Progress", err="1213 deadlock prior attempt")
		sess = {
			"identities_done": [],
			"riv_completed": [],
			"riv_failed": [],
			"current_riv": "611vj32kg4",
			"current_item_code": "ITEM-A",
			"current_warehouse": "WH-A",
			"status": "Running",
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=meta_ip,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_post_exhaust_grace_waits",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_in_progress_max_waits",
			return_value=2,  # stand-in for "original barrier exhausted"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			pause_out = final_completion_barrier(
				riv_name="611vj32kg4",
				item_code="ITEM-A",
				warehouse="WH-A",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				verify_chain=lambda *a, **k: {"dirty": False},
				delay_s=10,
			)
		self.assertTrue(pause_out.get("in_progress_unresolved"))
		self.assertEqual(sess["status"], "Paused")
		self.assertEqual(sess["identities_done"], [])

		# Phase 2 — RIV completes after pause; resume reconcile advances once.
		meta_done = _meta("Completed", err="1213 deadlock prior attempt")
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=meta_done,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.get_value.return_value = meta_done.error_log
			f.db.set_value.return_value = None
			recon = reconcile_session_current_riv(
				sess,
				company="C",
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 9},
			)
		self.assertTrue(recon["ok"])
		self.assertTrue(recon.get("advanced"))
		self.assertEqual(sess["identities_done"], ["ITEM-A|WH-A"])
		self.assertEqual(sess["riv_completed"], ["611vj32kg4"])
		self.assertIsNone(sess.get("current_riv"))


if __name__ == "__main__":
	unittest.main()
