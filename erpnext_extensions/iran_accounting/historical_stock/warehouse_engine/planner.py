# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse planner — attach explicit READY_WAREHOUSE_REPLAY states."""

from __future__ import annotations

from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.dependency import build_dependency_report
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.scope_analyzer import analyze_candidate
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.simulator import simulate_warehouse_replay
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.validator import validate_warehouse_simulation


def plan_warehouse_repair(row: dict, *, cache: dict | None = None) -> dict:
	"""Full analyze → simulate → validate pipeline (read-only)."""
	analysis = analyze_candidate(row, cache=cache)
	deps = build_dependency_report(analysis)
	proposed_times = _proposed_times(row)
	from_dt = analysis.get("from_datetime")
	sim = {"ok": False, "reason": "missing from_datetime"}
	if from_dt and analysis.get("item") and analysis.get("warehouse"):
		sim = simulate_warehouse_replay(
			analysis["item"],
			analysis["warehouse"],
			from_dt,
			proposed_times=proposed_times,
		)
	decision = validate_warehouse_simulation(analysis, sim)
	return {
		**row,
		"topic": "WAREHOUSE_ENGINE",
		"repair_class": "WAREHOUSE_VALUATION_REPLAY",
		"warehouse_analysis": analysis,
		"warehouse_dependencies": deps,
		"warehouse_simulation": {
			k: sim.get(k)
			for k in (
				"ok",
				"row_count",
				"first_divergence",
				"final_qty_unchanged",
				"idempotent",
				"expected_bin",
				"affected_batches",
				"negative_qty_vouchers",
				"negative_incoming_vouchers",
				"exploded_rate_vouchers",
			)
		},
		"planner_status": decision.get("planner_status"),
		"reason": decision.get("reason"),
		"required_action": decision.get("required_action"),
		"required_scope": decision.get("required_scope"),
		"affected_vouchers": decision.get("affected_vouchers"),
		"affected_sle": decision.get("affected_sle"),
		"sql_updates": decision.get("estimated_sql"),
		"estimated_runtime_seconds": decision.get("estimated_runtime_seconds"),
		"eligible": decision.get("eligible"),
		"confidence": "EXACT" if decision.get("eligible") else "LIKELY",
		"warehouse_validation": decision,
	}


def _proposed_times(row: dict) -> dict:
	times = {}
	for m in row.get("moves") or []:
		if m.get("document") and m.get("new"):
			times[m["document"]] = get_datetime(m["new"])
	if row.get("outbound_document") and row.get("proposed_outbound_time"):
		times.setdefault(row["outbound_document"], get_datetime(row["proposed_outbound_time"]))
	if row.get("inbound_document") and row.get("proposed_inbound_time"):
		times.setdefault(row["inbound_document"], get_datetime(row["proposed_inbound_time"]))
	return times
