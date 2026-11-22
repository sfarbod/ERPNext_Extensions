# Copyright (c) 2026, ERPNext Extensions contributors
"""MCA parallel execution + no synchronous RIV wait — v5.8.5 gates."""

from __future__ import annotations

import inspect
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.locks import (
	acquire_se_lock,
	release_se_lock,
)
from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re


def check(name: str, fn) -> dict:
	t0 = time.perf_counter()
	try:
		fn()
		return {"name": name, "ok": True, "ms": round((time.perf_counter() - t0) * 1000, 2)}
	except Exception as exc:
		return {
			"name": name,
			"ok": False,
			"error": str(exc),
			"ms": round((time.perf_counter() - t0) * 1000, 2),
		}


def run_parallel_no_riv_wait_suite() -> dict:
	results = []

	def t_pipeline_returns_before_riv_wait():
		src = inspect.getsource(re._run_pipeline)
		assert "mca_stage_done_riv_queued" in src
		assert "mca_stage_complete" in src
		# Must not call monitor before returning from first-time riv queue
		# (monitor only on resume path after the early return).
		idx_return = src.find("mca_stage_done_riv_queued")
		idx_monitor = src.find("_monitor_riv_and_finalize")
		assert idx_return != -1 and idx_monitor != -1
		assert idx_return < idx_monitor

	def t_monitor_no_schedule_retry_on_pending():
		src = inspect.getsource(re._monitor_riv_and_finalize)
		# Pending RIV must not occupy MCA worker with delayed process_mca retries
		assert "_schedule_retry(mca_name, \"riv_in_progress\"" not in src
		assert "riv_pending_no_wait" in src or "riv_pending" in src

	def t_same_se_lock_held_while_pending_riv():
		"""Per-SE lock must not release on Pending RIV — only terminal statuses.

		Independent SEs use different keys (parallelism). Same-SE exclusivity
		must survive the Pending RIV window so a sibling MCA cannot mutate SE.
		"""
		src = inspect.getsource(re.process_mca)
		finally_src = src[src.find("finally:") :]
		assert "release_se_lock" in finally_src
		assert "PS_COMPLETED" in finally_src
		assert "PS_FAILED" in finally_src
		assert "PS_PENDING_RIV" not in finally_src

	def t_refresh_sle_default_not_full_walk():
		src = inspect.getsource(re._refresh_sle)
		assert "mca_full_sle_refresh" in src
		assert "source remirror" in src.lower() or "source_only" in src or "RIV owns" in src

	def t_same_se_lock_excludes_parallel():
		se = f"TEST-SE-{frappe.generate_hash(length=8)}"
		assert acquire_se_lock(se, "mca:a")
		assert not acquire_se_lock(se, "mca:b")
		release_se_lock(se, "mca:a")
		assert acquire_se_lock(se, "mca:b")
		release_se_lock(se, "mca:b")

	def t_independent_se_locks_allow_overlap():
		se1 = f"TEST-SE-A-{frappe.generate_hash(length=6)}"
		se2 = f"TEST-SE-B-{frappe.generate_hash(length=6)}"
		held = []

		def hold(se, owner, hold_s=0.3):
			ok = acquire_se_lock(se, owner)
			held.append((se, owner, ok, time.time()))
			time.sleep(hold_s)
			release_se_lock(se, owner)
			return ok

		with ThreadPoolExecutor(max_workers=2) as pool:
			futs = [
				pool.submit(hold, se1, "mca:1"),
				pool.submit(hold, se2, "mca:2"),
			]
			oks = [f.result() for f in as_completed(futs)]
		assert all(oks), oks
		# Both acquired — proves independent SE locks do not serialize
		assert len(held) == 2 and all(h[2] for h in held)

	def t_concurrency_constant():
		assert C.MCA_DEFAULT_CONCURRENCY >= 2
		assert C.MCA_RQ_QUEUE in C.VALID_FRAPPE_QUEUES
		assert C.MCA_RQ_QUEUE == "long"
		assert C.MCA_RIV_RQ_QUEUE == "long"
		assert C.MCA_RQ_QUEUE != "mca"

	results.append(check("pipeline_returns_before_riv_wait", t_pipeline_returns_before_riv_wait))
	results.append(check("monitor_no_schedule_retry_on_pending", t_monitor_no_schedule_retry_on_pending))
	results.append(check("same_se_lock_held_while_pending_riv", t_same_se_lock_held_while_pending_riv))
	results.append(check("refresh_sle_default_not_full_walk", t_refresh_sle_default_not_full_walk))
	results.append(check("same_se_lock_excludes_parallel", t_same_se_lock_excludes_parallel))
	results.append(check("independent_se_locks_allow_overlap", t_independent_se_locks_allow_overlap))
	results.append(check("concurrency_constant", t_concurrency_constant))

	ok = all(r["ok"] for r in results)
	return {"ok": ok, "checks": results}


if __name__ == "__main__":
	frappe.connect()
	print(run_parallel_no_riv_wait_suite())
