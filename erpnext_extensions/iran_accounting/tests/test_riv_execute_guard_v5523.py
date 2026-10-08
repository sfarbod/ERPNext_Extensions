# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.23 — RIV execute guard must be RQ/pickle-safe (parallel reposting)."""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils.background_jobs import get_job

from erpnext.stock.doctype.repost_item_valuation import repost_item_valuation as riv_mod
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	REPOSTING_JOB_ID_PREFIX,
	enqueue_reposting_entry,
)

from erpnext_extensions.iran_accounting.integration import monkey_patches as mp
from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

# Capture true ERPNext upstream once (before tests swap in probes).
if getattr(riv_mod, "_iran_original_execute_reposting_entry", None) not in (
	None,
	guard.execute_reposting_entry,
):
	_REAL_UPSTREAM = riv_mod._iran_original_execute_reposting_entry
elif riv_mod.execute_reposting_entry is not guard.execute_reposting_entry:
	_REAL_UPSTREAM = riv_mod.execute_reposting_entry
else:
	_REAL_UPSTREAM = None


def _upstream_probe(name, continue_reposting=False):
	"""Module-level probe used as stand-in for ERPNext execute_reposting_entry."""
	frappe.flags._riv_guard_probe_calls = getattr(frappe.flags, "_riv_guard_probe_calls", []) + [
		{"name": name, "continue_reposting": continue_reposting}
	]
	return {"probed": name, "continue_reposting": continue_reposting}


def _reset_riv_guard_install_state(*, restore_real: bool = False):
	"""Test helper: clear install flags so install can be re-exercised."""
	guard._ORIG_EXECUTE_REPOSTING_ENTRY = None
	if hasattr(riv_mod, "_iran_patched_execute_reposting_entry_lock"):
		delattr(riv_mod, "_iran_patched_execute_reposting_entry_lock")
	if hasattr(riv_mod, "_iran_original_execute_reposting_entry"):
		delattr(riv_mod, "_iran_original_execute_reposting_entry")
	if restore_real and _REAL_UPSTREAM is not None:
		riv_mod.execute_reposting_entry = _REAL_UPSTREAM
		guard.install_execute_reposting_entry_guard(riv_mod)


class TestRivExecuteGuardPickleAndIdentityV5523(unittest.TestCase):
	def setUp(self):
		_reset_riv_guard_install_state()
		# Ensure a stable "upstream" for install.
		riv_mod.execute_reposting_entry = _upstream_probe
		guard.install_execute_reposting_entry_guard(riv_mod)

	def tearDown(self):
		_reset_riv_guard_install_state(restore_real=True)

	def test_no_locals_in_qualname(self):
		target = riv_mod.execute_reposting_entry
		self.assertIs(target, guard.execute_reposting_entry)
		self.assertEqual(
			target.__module__,
			"erpnext_extensions.iran_accounting.integration.riv_execute_guard",
		)
		self.assertEqual(target.__name__, "execute_reposting_entry")
		self.assertEqual(target.__qualname__, "execute_reposting_entry")
		self.assertNotIn("<locals>", target.__qualname__)

	def test_pickle_roundtrip(self):
		target = riv_mod.execute_reposting_entry
		blob = pickle.dumps(target)
		restored = pickle.loads(blob)
		self.assertIs(restored, guard.execute_reposting_entry)
		self.assertNotIn("<locals>", restored.__qualname__)

	def test_nested_local_still_fails_pickle_baseline(self):
		"""Document 5.5.22 failure mode for BEFORE/AFTER contrast."""

		def nested_local(name, continue_reposting=False):
			return name

		with self.assertRaises(Exception) as ctx:
			pickle.dumps(nested_local)
		self.assertIn("local", str(ctx.exception).lower())


class TestRivExecuteGuardIdempotencyV5523(unittest.TestCase):
	def setUp(self):
		_reset_riv_guard_install_state()
		riv_mod.execute_reposting_entry = _upstream_probe

	def tearDown(self):
		_reset_riv_guard_install_state(restore_real=True)

	def test_install_twice_keeps_original(self):
		guard.install_execute_reposting_entry_guard(riv_mod)
		first_orig = riv_mod._iran_original_execute_reposting_entry
		self.assertIs(first_orig, _upstream_probe)
		self.assertIs(guard._ORIG_EXECUTE_REPOSTING_ENTRY, _upstream_probe)

		guard.install_execute_reposting_entry_guard(riv_mod)
		self.assertIs(riv_mod._iran_original_execute_reposting_entry, first_orig)
		self.assertIs(guard._ORIG_EXECUTE_REPOSTING_ENTRY, first_orig)
		self.assertIs(riv_mod.execute_reposting_entry, guard.execute_reposting_entry)
		self.assertIsNot(guard._ORIG_EXECUTE_REPOSTING_ENTRY, guard.execute_reposting_entry)

	def test_apply_monkey_patches_twice(self):
		mp._PATCHED = False
		mp.apply_monkey_patches()
		orig1 = riv_mod._iran_original_execute_reposting_entry
		self.assertIsNotNone(orig1)
		self.assertIsNot(orig1, guard.execute_reposting_entry)

		# Second apply short-circuits via mp._PATCHED but install must stay safe if forced.
		mp.apply_monkey_patches()
		guard.install_execute_reposting_entry_guard(riv_mod)
		self.assertIs(riv_mod._iran_original_execute_reposting_entry, orig1)
		self.assertIs(guard._ORIG_EXECUTE_REPOSTING_ENTRY, orig1)


class TestRivExecuteGuardLockContractV5523(unittest.TestCase):
	def setUp(self):
		_reset_riv_guard_install_state()
		riv_mod.execute_reposting_entry = _upstream_probe
		guard.install_execute_reposting_entry_guard(riv_mod)
		frappe.flags._riv_guard_probe_calls = []
		frappe.local.iran_hr_riv_exec_owner = None

	def tearDown(self):
		frappe.local.iran_hr_riv_exec_owner = None
		frappe.flags._riv_guard_probe_calls = []
		_reset_riv_guard_install_state(restore_real=True)

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
		return_value=False,
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
		return_value=True,
	)
	def test_case_a_hr_lock_different_owner(self, _riv, _gl):
		frappe.local.iran_hr_riv_exec_owner = "other-riv"
		out = guard.execute_reposting_entry("riv-a", continue_reposting=True)
		self.assertIsNone(out)
		self.assertEqual(frappe.flags._riv_guard_probe_calls, [])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
		return_value=False,
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
		return_value=True,
	)
	def test_case_b_same_hr_owner(self, _riv, _gl):
		frappe.local.iran_hr_riv_exec_owner = "riv-b"
		out = guard.execute_reposting_entry("riv-b", continue_reposting=False)
		self.assertEqual(out["probed"], "riv-b")
		self.assertEqual(len(frappe.flags._riv_guard_probe_calls), 1)

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
		return_value=True,
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
		return_value=False,
	)
	def test_case_c_company_gl_lock_no_owner(self, _riv, _gl):
		frappe.local.iran_hr_riv_exec_owner = None
		with patch("frappe.db.get_value", return_value="Test Company"):
			out = guard.execute_reposting_entry("riv-c", continue_reposting=True)
		self.assertIsNone(out)
		self.assertEqual(frappe.flags._riv_guard_probe_calls, [])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
		side_effect=RuntimeError("redis down"),
	)
	def test_case_d_lock_check_fail_open(self, _riv):
		out = guard.execute_reposting_entry("riv-d", continue_reposting=True)
		self.assertEqual(out["probed"], "riv-d")
		self.assertEqual(len(frappe.flags._riv_guard_probe_calls), 1)

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
		return_value=False,
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
		return_value=False,
	)
	def test_case_e_no_locks_calls_original_once(self, _riv, _gl):
		out = guard.execute_reposting_entry("riv-e", continue_reposting=True)
		self.assertEqual(out["probed"], "riv-e")
		self.assertEqual(out["continue_reposting"], True)
		self.assertEqual(len(frappe.flags._riv_guard_probe_calls), 1)

	def test_resolve_original_raises_if_missing(self):
		guard._ORIG_EXECUTE_REPOSTING_ENTRY = guard.execute_reposting_entry
		riv_mod.execute_reposting_entry = guard.execute_reposting_entry
		riv_mod._iran_original_execute_reposting_entry = guard.execute_reposting_entry
		with self.assertRaises(Exception):
			guard._resolve_original_execute_reposting_entry()

	def test_lazy_resolve_when_global_cleared(self):
		"""Fresh worker may import wrapper before install sets the global."""
		self.assertIs(riv_mod._iran_original_execute_reposting_entry, _upstream_probe)
		guard._ORIG_EXECUTE_REPOSTING_ENTRY = None
		frappe.flags._riv_guard_probe_calls = []
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.riv_exec_lock_held",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.db_concurrency.company_gl_lock_held",
			return_value=False,
		):
			out = guard.execute_reposting_entry("lazy-riv", continue_reposting=False)
		self.assertEqual(out["probed"], "lazy-riv")
		self.assertIs(guard._ORIG_EXECUTE_REPOSTING_ENTRY, _upstream_probe)

	def test_recoverable_errors_remain_upstream(self):
		"""Wrapper must not reimplement RecoverableErrors / status transitions."""
		import inspect

		from rq.timeouts import JobTimeoutException

		self.assertIn(JobTimeoutException, riv_mod.RecoverableErrors)
		# Upstream module source (not the nested IRR `repost` wrapper).
		mod_src = open(riv_mod.__file__, encoding="utf-8").read()
		self.assertIn("RecoverableErrors", mod_src)
		self.assertIn('status = "In Progress"', mod_src)
		guard_src = inspect.getsource(guard.execute_reposting_entry)
		self.assertNotIn("RecoverableErrors", guard_src)
		self.assertNotIn("deduplicate_similar_repost", guard_src)


class TestRivExecuteGuardFreshProcessV5523(unittest.TestCase):
	def test_fresh_process_import_and_install(self):
		site = getattr(frappe.local, "site", None) or "development.localhost"
		bench_dir = os.path.abspath(os.path.join(os.path.dirname(frappe.__file__), "..", "..", ".."))
		sites_path = os.path.join(bench_dir, "sites")
		script = f"""
import os, pickle
os.chdir({sites_path!r})
import frappe
frappe.init({site!r})
frappe.connect()
import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod
from erpnext_extensions.iran_accounting.integration import monkey_patches as mp
from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

mp._PATCHED = False
mp.apply_monkey_patches()
target = riv_mod.execute_reposting_entry
assert target is guard.execute_reposting_entry, (target, guard.execute_reposting_entry)
assert "<locals>" not in target.__qualname__, target.__qualname__
assert riv_mod._iran_original_execute_reposting_entry is not guard.execute_reposting_entry
assert guard._ORIG_EXECUTE_REPOSTING_ENTRY is riv_mod._iran_original_execute_reposting_entry
blob = pickle.dumps(target)
restored = pickle.loads(blob)
assert restored is guard.execute_reposting_entry
resolved = guard._resolve_original_execute_reposting_entry()
assert resolved is not guard.execute_reposting_entry
print("FRESH_PROCESS_OK", target.__module__, target.__qualname__, resolved.__module__, getattr(resolved, "__qualname__", type(resolved).__name__))
frappe.destroy()
"""
		py = subprocess.run(
			[sys.executable, "-c", script],
			cwd=sites_path,
			capture_output=True,
			text=True,
			timeout=120,
			env={**os.environ},
		)
		if py.returncode != 0:
			self.fail(f"fresh process failed:\nSTDOUT:{py.stdout}\nSTDERR:{py.stderr}")
		self.assertIn("FRESH_PROCESS_OK", py.stdout)


class TestRivExecuteGuardRqEnqueueAndWorkerV5523(unittest.TestCase):
	TEST_RIV = "__riv_guard_v5523_probe__"

	def setUp(self):
		frappe.flags._riv_guard_probe_calls = []
		# Ensure guard installed over real module, then point original at probe for safe exec.
		mp._PATCHED = False
		mp.apply_monkey_patches()
		self._saved_orig = riv_mod._iran_original_execute_reposting_entry
		riv_mod._iran_original_execute_reposting_entry = _upstream_probe
		guard._ORIG_EXECUTE_REPOSTING_ENTRY = _upstream_probe
		# Cancel any leftover probe job
		self._delete_probe_job()

	def tearDown(self):
		self._delete_probe_job()
		if self._saved_orig is not None:
			riv_mod._iran_original_execute_reposting_entry = self._saved_orig
			guard._ORIG_EXECUTE_REPOSTING_ENTRY = self._saved_orig
		frappe.flags._riv_guard_probe_calls = []
		_reset_riv_guard_install_state(restore_real=True)

	def _delete_probe_job(self):
		job = get_job(f"{REPOSTING_JOB_ID_PREFIX}{self.TEST_RIV}")
		if job:
			try:
				job.delete()
			except Exception:
				pass

	def test_real_enqueue_no_pickling_error(self):
		self.assertIs(riv_mod.execute_reposting_entry, guard.execute_reposting_entry)
		self.assertNotIn("<locals>", riv_mod.execute_reposting_entry.__qualname__)

		# Real upstream enqueue path (not mocked).
		enqueue_reposting_entry(self.TEST_RIV)

		job = get_job(f"{REPOSTING_JOB_ID_PREFIX}{self.TEST_RIV}")
		self.assertIsNotNone(job, "RQ job was not created — pickle/enqueue failed")
		self.assertEqual(job.id, f"{frappe.local.site}||{REPOSTING_JOB_ID_PREFIX}{self.TEST_RIV}")
		# Queue name ends with :long
		self.assertTrue(str(job.origin).endswith(":long") or "long" in str(job.origin), job.origin)
		status = job.get_status(refresh=True)
		self.assertIn(status, ("queued", "started", "deferred", "scheduled"))

	def test_worker_execute_job_path(self):
		"""Consume the enqueued job via frappe.execute_job (same bootstrap as RQ worker)."""
		from frappe.utils.background_jobs import execute_job

		enqueue_reposting_entry(self.TEST_RIV)
		job = get_job(f"{REPOSTING_JOB_ID_PREFIX}{self.TEST_RIV}")
		self.assertIsNotNone(job)

		# Clear probe, then run as worker would after dequeue.
		frappe.flags._riv_guard_probe_calls = []
		kwargs = job.kwargs
		# RQ stores execute_job kwargs at top level
		execute_job(
			site=kwargs["site"],
			method=kwargs["method"],
			event=kwargs.get("event"),
			job_name=kwargs.get("job_name"),
			kwargs=kwargs.get("kwargs") or {},
			user=kwargs.get("user"),
			is_async=False,
		)
		calls = frappe.flags._riv_guard_probe_calls
		self.assertEqual(len(calls), 1, calls)
		self.assertEqual(calls[0]["name"], self.TEST_RIV)
		self.assertTrue(calls[0]["continue_reposting"])


class TestRivExecuteGuardSequentialParallelSmokeV5523(unittest.TestCase):
	def test_sequential_calls_wrapper_inline(self):
		mp._PATCHED = False
		mp.apply_monkey_patches()
		self.assertIs(riv_mod.execute_reposting_entry, guard.execute_reposting_entry)

		saved = riv_mod._iran_original_execute_reposting_entry
		riv_mod._iran_original_execute_reposting_entry = _upstream_probe
		guard._ORIG_EXECUTE_REPOSTING_ENTRY = _upstream_probe
		frappe.flags._riv_guard_probe_calls = []
		try:
			# Sequential mode invokes the module global directly (same as repost_entries loop).
			riv_mod.execute_reposting_entry("seq-riv", continue_reposting=False)
			self.assertEqual(len(frappe.flags._riv_guard_probe_calls), 1)
		finally:
			riv_mod._iran_original_execute_reposting_entry = saved
			guard._ORIG_EXECUTE_REPOSTING_ENTRY = saved
			frappe.flags._riv_guard_probe_calls = []
			_reset_riv_guard_install_state(restore_real=True)

	def test_source_documents_n1_deferred(self):
		"""N=1 off-by-one remains upstream; not patched in 5.5.23."""
		import inspect

		src = inspect.getsource(riv_mod.run_parallel_reposting)
		self.assertIn("len(items) > no_of_parallel_reposting", src)
		# Guard module must not patch run_parallel / enqueue_reposting_entry.
		guard_src = open(guard.__file__, encoding="utf-8").read()
		self.assertNotIn("run_parallel_reposting", guard_src)
		self.assertNotIn("enqueue_reposting_entry", guard_src)
