# Copyright (c) 2026, ERPNext Extensions contributors
"""Orphan RIV In Progress nudge — must NEVER mark Completed; only re-kick."""

from __future__ import annotations

import inspect
import time
from unittest.mock import patch

import frappe

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

	def t_heal_never_marks_completed_only_nudges():
		src = inspect.getsource(re._heal_orphaned_riv_in_progress)
		assert '"status": "Completed"' not in src
		assert "'status': 'Completed'" not in src
		assert "healed orphan In Progress" not in src
		assert "_kick_riv_execution" in src
		assert "1020" in src
		assert "deadlock" in src.lower()
		assert "age < 90" in src
		assert "Never" in src or "never" in src

	def t_bare_status_string_is_noop():
		# Must not raise / mutate when get_value returns a bare string
		with patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.get_value",
			return_value="In Progress",
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.set_value"
		) as sv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._kick_riv_execution"
		) as kick:
			re._heal_orphaned_riv_in_progress("RIV-FAKE-STRING")
			sv.assert_not_called()
			kick.assert_not_called()

	def t_pipeline_returns_before_riv():
		src = inspect.getsource(re._run_pipeline)
		assert "mca_stage_done_riv_queued" in src
		idx_return = src.find("mca_stage_done_riv_queued")
		idx_monitor = src.find("_monitor_riv_and_finalize")
		assert idx_return < idx_monitor

	def t_monitor_calls_heal_but_finalize_gate_requires_core_completed():
		src = inspect.getsource(re._monitor_riv_and_finalize)
		assert "_heal_orphaned_riv_in_progress" in src
		assert "_riv_statuses_allow_mca_finalize" in src
		assert "PS_COMPLETED" in src or "C.PS_COMPLETED" in src

	results.append(check("heal_never_marks_completed_only_nudges", t_heal_never_marks_completed_only_nudges))
	results.append(check("bare_status_string_noop", t_bare_status_string_is_noop))
	results.append(check("pipeline_returns_before_riv", t_pipeline_returns_before_riv))
	results.append(check("monitor_finalize_gate", t_monitor_calls_heal_but_finalize_gate_requires_core_completed))
	ok = all(r["ok"] for r in results)
	return {"ok": ok, "checks": results}


if __name__ == "__main__":
	frappe.connect()
	print(run_orphan_riv_heal_suite())
