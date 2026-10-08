# Copyright (c) 2026, ERPNext Extensions contributors
"""DB concurrency retry + sync worker preflight contracts (v5.3.47)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
	DB_1020_RETRY,
	DB_DEADLOCK_RETRY,
	DB_LOCK_TIMEOUT_RETRY,
	classify_db_concurrency_error,
	run_with_db_concurrency_retry,
)
from erpnext_extensions.iran_accounting.historical_stock.worker_preflight import (
	WORKER_PREFLIGHT_BLOCKED,
	WORKER_PREFLIGHT_READY,
	report_queue_status,
	run_worker_preflight,
)


class TestClassifyDbConcurrencyV5347(unittest.TestCase):
	def test_1213_deadlock(self):
		exc = Exception(1213, "Deadlock found when trying to get lock")
		self.assertEqual(classify_db_concurrency_error(exc), DB_DEADLOCK_RETRY)

	def test_1020_record_changed(self):
		exc = Exception(1020, "Record has changed since last read in table 'tabGL Entry'")
		self.assertEqual(classify_db_concurrency_error(exc), DB_1020_RETRY)

	def test_1205_lock_timeout(self):
		exc = Exception(1205, "Lock wait timeout exceeded")
		self.assertEqual(classify_db_concurrency_error(exc), DB_LOCK_TIMEOUT_RETRY)

	def test_query_deadlock_error_1020_message(self):
		exc = frappe.QueryDeadlockError(
			(1020, "Record has changed since last read in table 'tabGL Entry'")
		)
		self.assertEqual(classify_db_concurrency_error(exc), DB_1020_RETRY)

	def test_valuation_integrity_not_retryable(self):
		class ValuationIntegrityError(Exception):
			pass

		self.assertIsNone(classify_db_concurrency_error(ValuationIntegrityError("I3 boom")))

	def test_generic_exception_not_retryable(self):
		self.assertIsNone(classify_db_concurrency_error(RuntimeError("I3 negative SVD")))


class TestBoundedRetryV5347(unittest.TestCase):
	def test_retries_then_succeeds(self):
		state = {"n": 0}

		def fn():
			state["n"] += 1
			if state["n"] < 3:
				return {
					"ok": False,
					"db_concurrency_retry": True,
					"retry_reason": DB_1020_RETRY,
					"reason": "1020 GL",
				}
			return {"ok": True, "riv_status": "Completed"}

		out = run_with_db_concurrency_retry(fn, max_attempts=3, backoff_s=(0.0, 0.0, 0.0))
		self.assertTrue(out["ok"])
		self.assertEqual(out["attempt_n"], 3)
		self.assertEqual(state["n"], 3)

	def test_exhaustion(self):
		def fn():
			return {
				"ok": False,
				"db_concurrency_retry": True,
				"retry_reason": DB_DEADLOCK_RETRY,
				"reason": "deadlock",
			}

		out = run_with_db_concurrency_retry(fn, max_attempts=3, backoff_s=(0.0, 0.0, 0.0))
		self.assertFalse(out["ok"])
		self.assertEqual(len(out["attempts"]), 3)

	def test_no_retry_on_i3(self):
		calls = {"n": 0}

		def fn():
			calls["n"] += 1
			return {
				"ok": False,
				"db_concurrency_retry": False,
				"reason": "I3 negative incoming SVD",
				"status": "FAILED_RIV",
			}

		out = run_with_db_concurrency_retry(fn, max_attempts=3, backoff_s=(0.0, 0.0, 0.0))
		self.assertFalse(out["ok"])
		self.assertEqual(calls["n"], 1)

	def test_raised_deadlock_retries(self):
		state = {"n": 0}

		def fn():
			state["n"] += 1
			if state["n"] == 1:
				raise frappe.QueryDeadlockError(
					(1020, "Record has changed since last read in table 'tabGL Entry'")
				)
			return {"ok": True}

		out = run_with_db_concurrency_retry(fn, max_attempts=3, backoff_s=(0.0, 0.0, 0.0))
		self.assertTrue(out["ok"])
		self.assertEqual(state["n"], 2)


class TestWorkerPreflightSyncReportV5347(FrappeTestCase):
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight.worker_queue_status",
		return_value={
			"queue": "long",
			"available": False,
			"workers_for_queue": 0,
			"workers_total": 0,
			"message": "Background workers are paused or not listening to the long queue.",
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._redis_reachable",
		return_value={"ok": True, "error": None},
	)
	def test_report_allows_workers_zero_for_sync(self, *_mocks):
		out = report_queue_status(queues=("long",), phase="L6:sync")
		self.assertTrue(out["ready"])
		self.assertEqual(out["mode"], "FOREGROUND_SYNC_REPORT_ONLY")
		self.assertEqual(out["queues"]["long"]["workers_for_queue"], 0)

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight.worker_queue_status",
		return_value={
			"queue": "long",
			"available": False,
			"workers_for_queue": 0,
			"workers_total": 0,
			"message": "Background workers are paused or not listening to the long queue.",
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._redis_reachable",
		return_value={"ok": True, "error": None},
	)
	def test_async_preflight_blocks_workers_zero(self, *_mocks):
		out = run_worker_preflight(queues=("long",), require_probe=False)
		self.assertEqual(out["status"], WORKER_PREFLIGHT_BLOCKED)
		self.assertFalse(out["ready"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._run_probe",
		return_value={"ok": True},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight.worker_queue_status",
		return_value={
			"queue": "long",
			"available": True,
			"workers_for_queue": 2,
			"workers_total": 2,
			"message": "2 worker(s)",
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._redis_reachable",
		return_value={"ok": True, "error": None},
	)
	def test_async_ready(self, *_mocks):
		out = run_worker_preflight(queues=("long",), require_probe=True)
		self.assertEqual(out["status"], WORKER_PREFLIGHT_READY)


class TestNarrowRivAtomicContractV5347(unittest.TestCase):
	def test_source_has_locks_and_retry(self):
		import inspect

		from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
			create_and_run_narrow_riv,
		)

		src = inspect.getsource(create_and_run_narrow_riv)
		self.assertIn("riv_exec_lock_key", src)
		self.assertIn("company_gl_lock_key", src)
		self.assertIn("max_db_concurrency_attempts", src)
		self.assertIn("run_with_db_concurrency_retry", src)
		self.assertIn("iran_hr_riv_exec_owner", src)
		self.assertIn("_execute_repost_without_mid_commits", src)

	def test_execute_guard_patched(self):
		import inspect

		from erpnext_extensions.iran_accounting.integration import monkey_patches as mp
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		# v5.5.23: lock guard lives in module-level riv_execute_guard (RQ/pickle-safe).
		src = inspect.getsource(mp._patch_repost_compatibility)
		self.assertIn("install_execute_reposting_entry_guard", src)
		guard_src = inspect.getsource(guard.execute_reposting_entry)
		self.assertIn("riv_exec_lock_held", guard_src)
		self.assertIn("company_gl_lock_held", guard_src)
		self.assertIn("iran_hr_riv_exec_owner", guard_src)
		self.assertNotIn("<locals>", guard.execute_reposting_entry.__qualname__)

	def test_l6_sync_default(self):
		from erpnext_extensions.iran_accounting.historical_stock._validation import (
			sept30_l6_full_repost as l6,
		)

		self.assertTrue(l6.L6_SYNC_EXECUTION)
		self.assertEqual(l6.L6_MAX_DB_CONCURRENCY_ATTEMPTS, 3)
		src = open(l6.__file__, encoding="utf-8").read()
		self.assertIn("report_queue_status", src)
		self.assertIn("max_db_concurrency_attempts", src)
		self.assertIn("deadlock_metrics", src)
		self.assertNotIn("SET status='Skipped'", src)


class TestIdentity2781CanaryContractV5347(unittest.TestCase):
	"""Regression contract for Clean A stop at identity ~2781 / item 17000003."""

	IDENTITY_2781 = {
		"item": "17000003",
		"warehouse": "انبار approved مواد اولیه اسپاد",
		"riv_name": "tdaq2l7sbn",
		"gl_victim_voucher": "MAT-STE-2026-36823",
		"errno": 1020,
		"table": "tabGL Entry",
		"phase": "repost_gl_entries / _delete_gl_entries",
		"classification": "INFRASTRUCTURE_GL_DEADLOCK",
	}

	def test_canary_constants(self):
		self.assertEqual(self.IDENTITY_2781["item"], "17000003")
		self.assertEqual(self.IDENTITY_2781["errno"], 1020)
		self.assertEqual(self.IDENTITY_2781["classification"], "INFRASTRUCTURE_GL_DEADLOCK")

	def test_retry_classifies_2781_error(self):
		exc = frappe.QueryDeadlockError(
			(1020, "Record has changed since last read in table 'tabGL Entry'")
		)
		self.assertEqual(classify_db_concurrency_error(exc), DB_1020_RETRY)
