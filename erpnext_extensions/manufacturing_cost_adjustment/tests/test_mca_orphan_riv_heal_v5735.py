# Copyright (c) 2026, ERPNext Extensions contributors
"""Orphan RIV In Progress heal guards — must not mark Completed while live."""

from __future__ import annotations

import inspect
import time

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
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


def run_orphan_riv_heal_suite() -> dict:
	results = []

	def t_heal_requires_no_live_job_and_guards():
		src = inspect.getsource(re._heal_orphaned_riv_in_progress)
		assert "no live" in src.lower() or "live = True" in src or "if live:" in src
		assert "1020" in src
		assert "finished_ok" in src or "is_finished" in src
		assert "age < 90" in src

	def t_bare_status_string_is_noop():
		# Must not raise / mutate when get_value returns a bare string
		from unittest.mock import patch

		with patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.get_value",
			return_value="In Progress",
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.set_value"
		) as sv:
			re._heal_orphaned_riv_in_progress("RIV-FAKE-STRING")
			sv.assert_not_called()

	def t_pipeline_returns_before_riv():
		src = inspect.getsource(re._run_pipeline)
		assert "mca_stage_done_riv_queued" in src
		idx_return = src.find("mca_stage_done_riv_queued")
		idx_monitor = src.find("_monitor_riv_and_finalize")
		assert idx_return < idx_monitor

	results.append(check("heal_guards", t_heal_requires_no_live_job_and_guards))
	results.append(check("bare_status_string_noop", t_bare_status_string_is_noop))
	results.append(check("pipeline_returns_before_riv", t_pipeline_returns_before_riv))
	ok = all(r["ok"] for r in results)
	return {"ok": ok, "checks": results}


if __name__ == "__main__":
	frappe.connect()
	print(run_orphan_riv_heal_suite())
