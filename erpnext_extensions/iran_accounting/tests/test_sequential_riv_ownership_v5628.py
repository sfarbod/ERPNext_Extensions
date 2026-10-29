# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.28 — Sequential RIV sync ownership vs Core parallel/RQ dual-exec."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry import (
	configure_retry_clock,
	final_completion_barrier,
	reset_retry_clock,
	run_resilient_riv_execution,
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


class TestEnqueueRespectsRivClaimV5628(unittest.TestCase):
	def test_enqueue_skipped_when_claim_held(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_ENQUEUE_REPOSTING_ENTRY = mock.Mock(return_value="job")
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			return_value=True,
		):
			out = guard.enqueue_reposting_entry("RIV-CLAIMED")
		self.assertIsNone(out)
		guard._ORIG_ENQUEUE_REPOSTING_ENTRY.assert_not_called()

	def test_enqueue_forwards_when_unclaimed(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_ENQUEUE_REPOSTING_ENTRY = mock.Mock(return_value="job-1")
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			return_value=False,
		):
			out = guard.enqueue_reposting_entry("RIV-FREE")
		self.assertEqual(out, "job-1")
		guard._ORIG_ENQUEUE_REPOSTING_ENTRY.assert_called_once_with("RIV-FREE")

	def test_active_jobs_union_claimed(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS = mock.Mock(return_value={"RQ1"})
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.listed_claimed_riv_executions",
			return_value={"SYNC1"},
		):
			active = guard.get_entries_with_active_jobs()
		self.assertEqual(active, {"RQ1", "SYNC1"})

	def test_execute_defers_when_foreign_claim(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_EXECUTE_REPOSTING_ENTRY = mock.Mock()
		frappe.local.iran_hr_riv_exec_owner = None
		with mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._should_defer_riv_execution",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._ensure_iran_runtime_for_riv",
		):
			guard.execute_reposting_entry("RIV-X")
		guard._ORIG_EXECUTE_REPOSTING_ENTRY.assert_not_called()

	def test_execute_runs_when_owner_matches(self):
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		guard._ORIG_EXECUTE_REPOSTING_ENTRY = mock.Mock(return_value="ok")
		frappe.local.iran_hr_riv_exec_owner = "RIV-Y"
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard._ensure_iran_runtime_for_riv",
		):
			out = guard.execute_reposting_entry("RIV-Y")
		self.assertEqual(out, "ok")
		guard._ORIG_EXECUTE_REPOSTING_ENTRY.assert_called_once()


class TestInProgressBarrierPauseV5628(unittest.TestCase):
	def tearDown(self):
		reset_retry_clock()

	def test_still_in_progress_returns_unresolved_not_failed(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		meta = SimpleNamespace(
			name="R1",
			status="In Progress",
			item_code="I",
			warehouse="W",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			error_log="1020",
			modified="x",
		)
		sess = {"identities_done": [], "riv_completed": [], "status": "Running"}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=meta,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_post_exhaust_grace_waits",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_in_progress_max_waits",
			return_value=2,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = final_completion_barrier(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				verify_chain=lambda *a, **k: {"dirty": False},
				delay_s=10,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("in_progress_unresolved"))
		self.assertFalse(out.get("attempts_exhausted"))
		self.assertFalse(out.get("stop_chain"))
		self.assertEqual(sess.get("status"), "Paused")
		self.assertAlmostEqual(sum(clock.sleeps), 20.0, places=3)

	def test_completed_during_in_progress_barrier_reconciles(self):
		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		states = [
			SimpleNamespace(name="R1", status="In Progress", item_code="I", warehouse="W",
				posting_date="2026-01-01", posting_time="00:00:00", error_log="", modified="a"),
			SimpleNamespace(name="R1", status="Completed", item_code="I", warehouse="W",
				posting_date="2026-01-01", posting_time="00:00:00", error_log="", modified="b"),
			SimpleNamespace(name="R1", status="Completed", item_code="I", warehouse="W",
				posting_date="2026-01-01", posting_time="00:00:00", error_log="", modified="b"),
		]
		seq = iter(states)

		def reader(_n):
			try:
				return next(seq)
			except StopIteration:
				return states[-1]

		sess = {"identities_done": [], "riv_completed": [], "riv_failed": []}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			side_effect=reader,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_post_exhaust_grace_waits",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.seq_in_progress_max_waits",
			return_value=5,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.frappe"
		) as f:
			f.db.rollback.return_value = None
			out = final_completion_barrier(
				riv_name="R1",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
				sess=sess,
				verify_chain=lambda *a, **k: {"dirty": False, "sle_count": 2},
				delay_s=10,
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out.get("reconciled_after_exhaustion") or out.get("reconciled"))
		self.assertEqual(sess["identities_done"], ["I|W"])


class _FakeRedis:
	"""Minimal Redis stand-in with SET NX + Lua eval for claim helpers."""

	def __init__(self):
		self.store: dict[str, str] = {}
		self.active: set[str] = set()
		self.ttls: dict[str, int] = {}

	def set(self, key, token, nx=True, ex=None):
		if nx and key in self.store:
			return False
		self.store[key] = token
		if ex is not None:
			self.ttls[key] = int(ex)
		return True

	def get(self, key):
		return self.store.get(key)

	def expire(self, key, ttl):
		if key in self.store:
			self.ttls[key] = int(ttl)
			return True
		return False

	def delete(self, key):
		self.store.pop(key, None)
		self.ttls.pop(key, None)

	def sadd(self, key, member):
		self.active.add(member)
		return 1

	def srem(self, key, member):
		self.active.discard(member)
		return 1

	def smembers(self, key):
		return set(self.active)

	def eval(self, script, numkeys, *keys_and_args):
		keys = keys_and_args[:numkeys]
		args = keys_and_args[numkeys:]
		# Refresh script: GET match → EXPIRE + SADD
		if "EXPIRE" in script and "SADD" in script and "DEL" not in script:
			cur = self.get(keys[0])
			if cur == args[0]:
				self.expire(keys[0], int(args[1]))
				self.sadd(keys[1], args[2])
				self.expire(keys[1], int(args[1]))
				return 1
			return 0
		# Release script: GET match → DEL + SREM
		if "DEL" in script:
			cur = self.get(keys[0])
			if cur == args[0]:
				self.delete(keys[0])
				self.srem(keys[1], args[1])
				return 1
			return 0
		raise RuntimeError(f"unexpected lua: {script[:40]}")


class TestClaimHelpersV5628(unittest.TestCase):
	def test_claim_refresh_release_roundtrip(self):
		from erpnext_extensions.iran_accounting.historical_stock import db_concurrency as dc

		fake = _FakeRedis()
		with mock.patch.object(dc, "_redis", return_value=fake):
			tok = dc.claim_riv_execution("RIV-A", ttl_s=60)
			self.assertIsNotNone(tok)
			self.assertTrue(dc.riv_exec_lock_held("RIV-A"))
			self.assertIn("RIV-A", dc.listed_claimed_riv_executions())
			self.assertTrue(dc.refresh_riv_execution_claim("RIV-A", tok, ttl_s=60))
			self.assertTrue(dc.release_riv_execution_claim("RIV-A", tok))
			self.assertFalse(dc.riv_exec_lock_held("RIV-A"))
			self.assertEqual(dc.listed_claimed_riv_executions(), set())

	def test_concurrent_claim_exactly_one_winner(self):
		from erpnext_extensions.iran_accounting.historical_stock import db_concurrency as dc

		fake = _FakeRedis()
		with mock.patch.object(dc, "_redis", return_value=fake):
			a = dc.claim_riv_execution("RIV-SAME", ttl_s=30)
			b = dc.claim_riv_execution("RIV-SAME", ttl_s=30)
			self.assertIsNotNone(a)
			self.assertIsNone(b)
			# Independent RIV still claimable.
			c = dc.claim_riv_execution("RIV-OTHER", ttl_s=30)
			self.assertIsNotNone(c)

	def test_release_without_token_is_noop(self):
		from erpnext_extensions.iran_accounting.historical_stock import db_concurrency as dc

		fake = _FakeRedis()
		with mock.patch.object(dc, "_redis", return_value=fake):
			tok = dc.claim_riv_execution("RIV-B", ttl_s=30)
			self.assertFalse(dc.release_riv_execution_claim("RIV-B", None))
			self.assertTrue(dc.riv_exec_lock_held("RIV-B"))
			self.assertFalse(dc.release_riv_execution_claim("RIV-B", "not-the-token"))
			self.assertTrue(dc.riv_exec_lock_held("RIV-B"))
			self.assertTrue(dc.release_riv_execution_claim("RIV-B", tok))

	def test_expired_claim_refresh_and_assert_fail(self):
		from erpnext_extensions.iran_accounting.historical_stock import db_concurrency as dc

		fake = _FakeRedis()
		with mock.patch.object(dc, "_redis", return_value=fake):
			tok = dc.claim_riv_execution("RIV-C", ttl_s=30)
			# Simulate TTL expiry / theft: clear key then allow a new owner.
			fake.delete(dc.riv_exec_lock_key("RIV-C"))
			self.assertFalse(dc.refresh_riv_execution_claim("RIV-C", tok, ttl_s=30))
			with self.assertRaises(dc.RivExecutionOwnershipError):
				dc.assert_riv_execution_owner("RIV-C", tok)
			new = dc.claim_riv_execution("RIV-C", ttl_s=30)
			self.assertIsNotNone(new)
			self.assertFalse(dc.refresh_riv_execution_claim("RIV-C", tok, ttl_s=30))

	def test_reject_malformed_claim_name(self):
		from erpnext_extensions.iran_accounting.historical_stock import db_concurrency as dc

		fake = _FakeRedis()
		with mock.patch.object(dc, "_redis", return_value=fake):
			self.assertIsNone(dc.claim_riv_execution(""))
			self.assertIsNone(dc.claim_riv_execution(None))  # type: ignore[arg-type]

	def test_ownership_lost_pauses_resilient_loop(self):
		from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
			RivExecutionOwnershipError,
		)

		clock = _FakeClock()
		configure_retry_clock(sleep_fn=clock.sleep, now_fn=clock.time)
		meta = SimpleNamespace(
			name="R1",
			status="Queued",
			item_code="I",
			warehouse="W",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			error_log="",
			modified="x",
		)
		sess = {"identities_done": [], "riv_completed": [], "status": "Running"}

		def execute_fn(_):
			raise RivExecutionOwnershipError("lost")

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry.read_riv_status",
			return_value=meta,
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
				delay_s=10,
				max_attempts=3,
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out.get("in_progress_unresolved"))
		self.assertTrue(out.get("ownership_lost"))
		self.assertFalse(out.get("stop_chain"))
		self.assertEqual(sess.get("status"), "Paused")


if __name__ == "__main__":
	unittest.main()
