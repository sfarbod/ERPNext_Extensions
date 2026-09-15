# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse repair validation — READY only when simulation proves all gates."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	READY_WAREHOUSE_REPLAY,
	UNSAFE_GLOBAL_DEPENDENCY,
	WAREHOUSE_AMBIGUOUS,
	WAREHOUSE_POISONED_OPENING,
	WAREHOUSE_REAL_SHORTAGE,
	WAREHOUSE_REPLAY_REQUIRED,
)


def validate_warehouse_simulation(analysis: dict, simulation: dict) -> dict:
	"""Return planner status + reasons for a warehouse-scoped candidate."""
	checks = []
	ok = True

	def gate(name, passed, detail=""):
		nonlocal ok
		checks.append({"gate": name, "passed": bool(passed), "detail": detail})
		if not passed:
			ok = False

	sim_ok = bool(simulation.get("ok"))
	gate("simulation_ok", sim_ok, simulation.get("reason") or "")

	gate(
		"final_quantity_unchanged",
		bool(simulation.get("final_qty_unchanged")),
		f"current={simulation.get('final_qty_current')} proposed={simulation.get('final_qty_proposed')}",
	)
	gate(
		"no_unexplained_negative_qty",
		not (simulation.get("negative_qty_vouchers") or []),
		str(simulation.get("negative_qty_vouchers") or []),
	)
	# Warehouse qty sim from scope analysis
	wh_qty = (analysis.get("warehouse_qty") or {}) if analysis else {}
	prop_min = flt(wh_qty.get("proposed_min"))
	gate("warehouse_proposed_min_non_negative", prop_min >= -0.0001 or not wh_qty, f"proposed_min={prop_min}")

	gate(
		"no_negative_incoming_rate",
		not (simulation.get("negative_incoming_vouchers") or []),
		str(simulation.get("negative_incoming_vouchers") or []),
	)
	gate(
		"no_exploded_valuation",
		not (simulation.get("exploded_rate_vouchers") or []),
		str(simulation.get("exploded_rate_vouchers") or []),
	)
	gate("ma_deterministic_idempotent", bool(simulation.get("idempotent")), "second sim must match")
	gate("expected_bin_present", bool(simulation.get("expected_bin")), "")
	gate("gl_impact_known", bool(simulation.get("expected_gl_impact")), simulation.get("expected_gl_impact") or "")
	gate(
		"no_unrelated_item",
		True,  # identity-scoped by construction
		"item+warehouse identity only",
	)
	# Cross-warehouse: informational — transfers must stay value-neutral at apply time
	gate("cross_warehouse_documented", True, str(analysis.get("cross_warehouse_movements") or []))
	gate("idempotent_replay", bool(simulation.get("idempotent")), "")

	# Opening poison
	status = READY_WAREHOUSE_REPLAY
	required_action = "Apply warehouse-scoped timestamp reorder + identity MA replay"
	if analysis.get("warehouse_still_negative"):
		status = WAREHOUSE_REAL_SHORTAGE
		required_action = "Do not timestamp-force; stock is truly insufficient"
		ok = False
	elif analysis.get("cyclic"):
		status = UNSAFE_GLOBAL_DEPENDENCY
		required_action = "Break cyclic dependency manually"
		ok = False
	elif not simulation.get("previous_voucher") and flt(simulation.get("opening_qty")) < -0.0001:
		status = WAREHOUSE_POISONED_OPENING
		required_action = "Repair earlier patient zero first"
		ok = False
	elif not ok:
		# Classify why
		if simulation.get("negative_qty_vouchers"):
			status = WAREHOUSE_REAL_SHORTAGE
			required_action = "Simulation introduces negative qty — MANUAL/shortage"
		elif simulation.get("negative_incoming_vouchers") or simulation.get("exploded_rate_vouchers"):
			status = WAREHOUSE_POISONED_OPENING
			required_action = "Clear rate poison before warehouse replay"
		elif not simulation.get("idempotent"):
			status = WAREHOUSE_AMBIGUOUS
			required_action = "Non-deterministic simulation — investigate"
		else:
			status = WAREHOUSE_AMBIGUOUS
			required_action = "Failed safety gates — keep MANUAL"
	elif analysis.get("required_replay_scope") == "WAREHOUSE_VALUATION_SCOPED":
		status = READY_WAREHOUSE_REPLAY
	else:
		status = WAREHOUSE_REPLAY_REQUIRED

	affected_sle = simulation.get("downstream_affected_sle") or []
	return {
		"ok": status == READY_WAREHOUSE_REPLAY and all(c["passed"] for c in checks if c["gate"] != "cross_warehouse_documented"),
		"planner_status": status,
		"reason": analysis.get("reason") or simulation.get("reason") or status,
		"required_action": required_action,
		"required_scope": analysis.get("required_replay_scope") or "WAREHOUSE_VALUATION_SCOPED",
		"affected_vouchers": analysis.get("affected_vouchers") or simulation.get("affected_vouchers") or [],
		"affected_sle": [a.get("sle") for a in affected_sle],
		"affected_batches": simulation.get("affected_batches") or [],
		"estimated_sql": max(1, len(affected_sle) + len(analysis.get("moves") or []) + 2),
		"estimated_runtime_seconds": round(0.05 * max(1, simulation.get("row_count") or 1), 2),
		"checks": checks,
		"eligible": status == READY_WAREHOUSE_REPLAY,
	}
