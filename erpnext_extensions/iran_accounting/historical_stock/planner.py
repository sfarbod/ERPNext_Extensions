# Copyright (c) 2026, ERPNext Extensions contributors
"""Single Historical Repair decision engine.

Scan, Dashboard, Graph, Impact, and Apply must call ``evaluate_row``.
No other module may decide eligibility on its own.
"""

from __future__ import annotations

from frappe.utils import cint, flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	G0_HEALTHY,
	G1_ECONOMICALLY_WRONG,
	G2_MISSING,
	G3_UNBALANCED,
	G4_POISONED_SLE,
	I4_LEFTOVER_REPAIR,
	I4_READY,
	I4_REPAIRED,
	I4_REPLAY_REQUIRED,
	I4_WAITING,
	RATE_EPS,
	RIV_SAFE_TO_RETRY,
	RIV_UNSAFE,
	RIV_WAITING_GL,
	RIV_WAITING_RATE,
	RIV_WAITING_SLE,
	SLE_PATIENT_ZERO_REQUIRED,
	SLE_POISONED_CHAIN,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_RATE_REBUILD_COMPLETE,
	STATUS_RECONSTRUCTABLE,
	STATUS_VALUATION_POISON_DEPENDENCY,
	TOPIC_I4,
	Z0_LEGITIMATE_ZERO,
)

PLAN_READY = "READY"
PLAN_BLOCKED = "BLOCKED"
PLAN_WAITING_PATIENT_ZERO = "WAITING_PATIENT_ZERO"
PLAN_WAITING_RATE_REPAIR = "WAITING_RATE_REPAIR"
PLAN_WAITING_SLE_REPAIR = "WAITING_SLE_REPAIR"
PLAN_WAITING_GL_REPAIR = "WAITING_GL_REPAIR"
PLAN_WAITING_RIV = "WAITING_RIV"
PLAN_AMBIGUOUS = "AMBIGUOUS"
PLAN_MANUAL = "MANUAL"
PLAN_NO_REPAIR_PATH = "NO_REPAIR_PATH"
PLAN_READY_LOCAL = "READY_LOCAL_REPAIR"
PLAN_READY_BATCH_SCOPED = "READY_BATCH_SCOPED_REPAIR"
PLAN_READY_WO = "READY_WORK_ORDER_REPAIR"
PLAN_READY_IDENTITY = "READY_IDENTITY_REPAIR"
PLAN_WAREHOUSE_ESCALATION = "WAREHOUSE_ESCALATION_REQUIRED"
PLAN_INVALID_GRAPH = "INVALID_DEPENDENCY_GRAPH"
PLAN_READY_I4 = I4_READY
PLAN_WAITING_I4 = I4_WAITING
PLAN_I4_REPLAY_REQUIRED = I4_REPLAY_REQUIRED
PLAN_I4_REPAIRED = I4_REPAIRED

# Phase 2 Wrong Rate planner states (no generic BLOCKED for rate rows)
PLAN_READY_WRONG_RATE = "READY_WRONG_RATE"
PLAN_WAITING_RATE_DEPENDENCY = "WAITING_RATE_DEPENDENCY"
PLAN_RATE_REPLAY_REQUIRED = "RATE_REPLAY_REQUIRED"
PLAN_RATE_REPAIR_COMPLETE = "RATE_REPAIR_COMPLETE"
PLAN_RATE_AMBIGUOUS = "RATE_AMBIGUOUS"
PLAN_RATE_MANUAL = "RATE_MANUAL"
PLAN_RATE_POISONED_OPENING = "RATE_POISONED_OPENING"
PLAN_RATE_WAREHOUSE_ESCALATION = "RATE_WAREHOUSE_ESCALATION"

PLAN_STATUSES = (
	PLAN_READY,
	PLAN_BLOCKED,
	PLAN_WAITING_PATIENT_ZERO,
	PLAN_WAITING_RATE_REPAIR,
	PLAN_WAITING_SLE_REPAIR,
	PLAN_WAITING_GL_REPAIR,
	PLAN_WAITING_RIV,
	PLAN_AMBIGUOUS,
	PLAN_MANUAL,
	PLAN_NO_REPAIR_PATH,
	PLAN_READY_LOCAL,
	PLAN_READY_BATCH_SCOPED,
	PLAN_READY_WO,
	PLAN_READY_IDENTITY,
	PLAN_WAREHOUSE_ESCALATION,
	PLAN_INVALID_GRAPH,
	PLAN_READY_I4,
	PLAN_WAITING_I4,
	PLAN_I4_REPLAY_REQUIRED,
	PLAN_I4_REPAIRED,
	PLAN_READY_WRONG_RATE,
	PLAN_WAITING_RATE_DEPENDENCY,
	PLAN_RATE_REPLAY_REQUIRED,
	PLAN_RATE_REPAIR_COMPLETE,
	PLAN_RATE_AMBIGUOUS,
	PLAN_RATE_MANUAL,
	PLAN_RATE_POISONED_OPENING,
	PLAN_RATE_WAREHOUSE_ESCALATION,
)

READY_STATUSES = (
	PLAN_READY,
	PLAN_READY_LOCAL,
	PLAN_READY_BATCH_SCOPED,
	PLAN_READY_WO,
	PLAN_READY_IDENTITY,
	PLAN_READY_I4,
	PLAN_READY_WRONG_RATE,
)

DATABASE_BACKUP_REQUIRED = "DATABASE BACKUP REQUIRED"


def empty_decision(**overrides) -> dict:
	base = {
		"eligible": False,
		"blocked": True,
		"planner_status": PLAN_BLOCKED,
		"reason": "",
		"confidence": None,
		"sql_updates": 0,
		"replay_count": 0,
		"rebuild_count": 0,
		"dependency": None,
		"patient_zero": None,
		"required_prerequisite": None,
	}
	base.update(overrides)
	return base


def evaluate_row(row: dict | None, *, cache: dict | None = None) -> dict:
	"""Canonical eligibility. Apply must refuse iff this is not READY with sql_updates>0."""
	cache = cache if cache is not None else {}
	row = row or {}
	patient = _patient_name(row)
	confidence = row.get("confidence")
	decision = empty_decision(confidence=confidence, patient_zero=patient)
	topic = str(row.get("topic") or "")

	# Route by surface first. Topic-specific WAITING_* must not be collapsed
	# into a generic LIKELY/AMBIGUOUS MANUAL status.
	if row.get("inbound_document") and row.get("outbound_document"):
		return _evaluate_posting(row, decision, cache)
	if topic in ("FAILED_RIV",) or row.get("riv_name"):
		return _evaluate_riv(row, decision)
	if topic in ("GL",) or (row.get("gl_class") and not row.get("inbound_document")):
		return _evaluate_gl(row, decision)
	if topic in (TOPIC_I4, "I4_LEFTOVER") or row.get("repair_class") == I4_LEFTOVER_REPAIR:
		return _evaluate_i4(row, decision, patient)
	if topic == "SLE_BIN":
		return _evaluate_sle_bin(row, decision, patient)
	if topic in ("MANUFACTURE",):
		return _evaluate_manufacture(row, decision, patient, cache)
	return _evaluate_rate(row, decision, cache, patient)


def attach_plan(row: dict, *, cache: dict | None = None) -> dict:
	decision = evaluate_row(row, cache=cache)
	out = dict(row or {})
	out["planner"] = decision
	out["planner_status"] = decision["planner_status"]
	out["eligible"] = bool(decision["eligible"])
	out["blocked"] = bool(decision["blocked"])
	out["reason"] = decision["reason"]
	out["skip_reason"] = decision["reason"]
	out["blocker"] = decision["reason"]
	out["repair_required"] = bool(decision["eligible"])
	out["sql_updates"] = int(decision["sql_updates"] or 0)
	out["replay_count"] = int(decision["replay_count"] or 0)
	out["rebuild_count"] = int(decision["rebuild_count"] or 0)
	out["required_prerequisite"] = decision["required_prerequisite"]
	if decision["patient_zero"] and not out.get("patient_zero"):
		out["patient_zero"] = {"voucher_no": decision["patient_zero"]}
	from erpnext_extensions.iran_accounting.historical_stock.dependency import stamp_dependency

	return stamp_dependency(out, cache=cache, decision=decision)


def attach_plan_many(rows: list | None, *, cache: dict | None = None) -> list:
	cache = cache if cache is not None else {}
	return [attach_plan(r, cache=cache) for r in rows or []]


def stamp_scan_result(result: dict | None, *, cache: dict | None = None) -> dict:
	"""Stamp Scan/Dashboard payloads so eligible never diverges from Apply."""
	out = dict(result or {})
	cache = cache if cache is not None else {}
	raw_rows = list(out.get("rows") or [])
	# Index before stamping so WAITING_PZ can resolve cleared / circular roots.
	by_voucher: dict = {}
	for r in raw_rows:
		v = r.get("voucher") or r.get("voucher_no")
		if v and v not in by_voucher:
			by_voucher[v] = r
	cache["rows_by_voucher"] = by_voucher
	rows = attach_plan_many(raw_rows, cache=cache)
	# Re-index stamped rows (planner_status now known) and re-stamp WAITING
	# rows that may unlock after circular / cleared-PZ resolution.
	by_voucher = {}
	for r in rows:
		v = r.get("voucher") or r.get("voucher_no")
		if v and v not in by_voucher:
			by_voucher[v] = r
	cache["rows_by_voucher"] = by_voucher
	rows = [attach_plan(r, cache=cache) for r in rows]
	out["rows"] = rows
	out["eligible"] = [r for r in rows if r.get("eligible")]
	out["repairable"] = sum(
		1 for r in rows if r.get("planner_status") in READY_STATUSES and cint(r.get("sql_updates")) > 0
	)
	return out


def plan_selection(rows: list | None) -> dict:
	"""Repair Planner for the current selection. Used by Impact and the Desk."""
	cache = {}
	rows = list(rows or [])
	decisions = [evaluate_row(r, cache=cache) for r in rows]
	planned_rows = [attach_plan(r, cache=cache) for r in rows]
	blockers = [d for d in decisions if d["planner_status"] not in READY_STATUSES or cint(d["sql_updates"]) <= 0]
	ready = [d for d in decisions if d["planner_status"] in READY_STATUSES and cint(d["sql_updates"]) > 0]
	all_ready = bool(decisions) and not blockers
	sql = sum(cint(d["sql_updates"]) for d in ready) if all_ready else 0
	replay = sum(cint(d["replay_count"]) for d in ready) if all_ready else 0
	rebuild = sum(cint(d["rebuild_count"]) for d in ready) if all_ready else 0
	skip_reason = None
	if not decisions:
		skip_reason = "No rows selected. Repair Selected will not write."
	elif blockers:
		skip_reason = blockers[0]["reason"] or f"{blockers[0]['planner_status']} — Repair Selected disabled"
	elif sql <= 0:
		skip_reason = "SQL updates = 0. Repair Selected is disabled."
	vouchers = []
	seen = set()
	for row in planned_rows:
		for key in ("voucher", "voucher_no", "inbound_document", "outbound_document"):
			val = row.get(key)
			if isinstance(val, dict):
				val = val.get("voucher_no")
			if val and val not in seen:
				seen.add(val)
				vouchers.append(val)
	abort_reasons = [
		{
			"voucher": d.get("patient_zero") or d.get("required_prerequisite"),
			"reason": d["reason"],
			"planner_status": d["planner_status"],
		}
		for d in blockers
	]
	plan = {
		"dry_run": True,
		"planner": True,
		"repairing": vouchers if all_ready else [],
		"rows": planned_rows,
		"decisions": decisions,
		"eligible": [d for d in decisions if d["eligible"]],
		"blocked": blockers,
		"planner_status": (ready[0]["planner_status"] if all_ready and ready else (blockers[0]["planner_status"] if blockers else PLAN_BLOCKED)),
		"reason": skip_reason,
		"skip_reason": skip_reason,
		"confidence": ready[0]["confidence"] if len(ready) == 1 else None,
		"sql_updates": sql,
		"estimated_sql_updates": sql,
		"replay_count": replay,
		"rebuild_count": rebuild,
		"estimated_replay_depth": replay,
		"replay_chain": [d.get("required_prerequisite") or d.get("patient_zero") for d in decisions if d.get("required_prerequisite") or d.get("patient_zero")],
		"stock_entries": len(vouchers) if all_ready else 0,
		"sle": 0,
		"sabb": 0,
		"sbe": 0,
		"bin": 0,
		"gl": 0,
		"failed_riv": 0,
		"aborted": not all_ready,
		"abort_reasons": abort_reasons,
		"executable": all_ready and sql > 0,
		"database_backup_recommended": True,
		"database_backup_required": True,
		"full_rollback_possible": all_ready,
		"warning": DATABASE_BACKUP_REQUIRED,
		"global_riv": False,
		"patient_zero": decisions[0]["patient_zero"] if len(decisions) == 1 else None,
		"required_prerequisite": decisions[0]["required_prerequisite"] if len(decisions) == 1 else None,
		"dependency": decisions[0]["dependency"] if len(decisions) == 1 else None,
		"immediate_blocker": (planned_rows[0].get("immediate_blocker") if planned_rows else None),
		"root_blocker": (planned_rows[0].get("root_blocker") if planned_rows else None),
		"root_status": (planned_rows[0].get("root_status") if planned_rows else None),
		"dependency_depth": (planned_rows[0].get("dependency_depth") if planned_rows else 0),
		"repair_order": (planned_rows[0].get("repair_order_list") if planned_rows else []),
		"repair_sequence": (planned_rows[0].get("repair_order") if planned_rows else ""),
		"required_action": (planned_rows[0].get("required_action") if planned_rows else None),
		"dependency_tree": (planned_rows[0].get("dependency_tree") if planned_rows else None),
		"tree_text": (planned_rows[0].get("tree_text") if planned_rows else ""),
		"chain_preview": (planned_rows[0].get("chain_preview") if planned_rows else []),
		"no_repair_path": (planned_rows[0].get("no_repair_path") if planned_rows else False),
		"stop_reason": (planned_rows[0].get("stop_reason") if planned_rows else None),
		"estimated_repair_count": (planned_rows[0].get("estimated_repair_count") if planned_rows else 0),
		"blocked_because": (planned_rows[0].get("blocked_because") if planned_rows else None),
		"batch_isolation": (planned_rows[0].get("batch_isolation") if planned_rows else None),
		"smallest_safe_scope": (planned_rows[0].get("smallest_safe_scope") if planned_rows else None),
		"dependency_type": (planned_rows[0].get("dependency_type") if planned_rows else None),
		"escalation_reason": (planned_rows[0].get("escalation_reason") if planned_rows else None),
		"unrelated_poison": (planned_rows[0].get("unrelated_poison") if planned_rows else None),
	}
	if all_ready and ready:
		plan["sle"] = sum(cint(d.get("sle_count")) for d in ready)
		plan["sabb"] = sum(cint(d.get("sabb_count")) for d in ready)
		plan["sbe"] = sum(cint(d.get("sbe_count")) for d in ready)
		plan["bin"] = sum(cint(d.get("bin_count")) for d in ready)
		plan["replay_chain"] = vouchers
		plan["estimated_replay_seconds"] = max(1.0, (plan["sle"] * 50) / 1000.0)
		plan["stock_entries"] = sum(cint(d.get("se_count")) for d in ready) or len(vouchers)
	else:
		plan["estimated_replay_seconds"] = 0
		plan["stock_entries"] = 0
		if planned_rows and planned_rows[0].get("repair_order_list"):
			plan["replay_chain"] = list(planned_rows[0]["repair_order_list"]) + ["Replay downstream", "Integrity"]
			plan["estimated_replay_seconds"] = planned_rows[0].get("estimated_runtime_seconds") or 0
	from erpnext_extensions.iran_accounting.historical_stock.impact import format_impact

	plan["preview_text"] = format_impact(plan)
	return plan


def assert_ready(row: dict, *, cache: dict | None = None) -> dict:
	"""Apply-time gate. Raises with the same reason Scan/Impact already showed."""
	import frappe

	decision = evaluate_row(row, cache=cache)
	if decision["planner_status"] not in READY_STATUSES or cint(decision["sql_updates"]) <= 0:
		frappe.throw(
			decision["reason"] or f"{decision['planner_status']} — Repair Selected is disabled",
			title=decision["planner_status"],
		)
	return decision


def _not_ready(decision, status, reason, *, prerequisite=None, dependency=None, patient=None) -> dict:
	decision["planner_status"] = status
	decision["eligible"] = False
	decision["blocked"] = True
	decision["reason"] = reason
	decision["sql_updates"] = 0
	decision["replay_count"] = 0
	decision["rebuild_count"] = 0
	if prerequisite is not None:
		decision["required_prerequisite"] = prerequisite
	if dependency is not None:
		decision["dependency"] = dependency
	if patient is not None:
		decision["patient_zero"] = patient
	return decision


def _ready(decision, *, sql, replay=0, rebuild=0, reason="READY", planner_status=None, **counts) -> dict:
	decision["planner_status"] = planner_status or PLAN_READY
	decision["eligible"] = True
	decision["blocked"] = False
	decision["reason"] = reason
	decision["sql_updates"] = int(sql)
	decision["replay_count"] = int(replay)
	decision["rebuild_count"] = int(rebuild)
	decision.update(counts)
	return decision


def _patient_name(row) -> str | None:
	pz = row.get("patient_zero")
	if isinstance(pz, dict):
		return pz.get("voucher_no") or pz.get("voucher")
	if pz:
		return str(pz)
	return None


def _evaluate_posting(row, decision, cache) -> dict:
	from erpnext_extensions.iran_accounting.stock_posting_order import (
		STATUS_INSUFFICIENT_STOCK,
		STATUS_MIDNIGHT,
		STATUS_MIDNIGHT_REVIEW,
		STATUS_REAL_STOCK_SHORTAGE,
		STATUS_VALUATION_POISON,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import _row_moves, _voucher_item_warehouses
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import window_poison_hit

	opt = row.get("optimizer_status") or row.get("status")
	if opt in (STATUS_MIDNIGHT, STATUS_MIDNIGHT_REVIEW):
		return _not_ready(decision, PLAN_MANUAL, "MIDNIGHT_REVIEW — posting-date boundary requires manual review")
	if opt in (STATUS_INSUFFICIENT_STOCK, STATUS_REAL_STOCK_SHORTAGE):
		return _not_ready(decision, PLAN_BLOCKED, "REAL_STOCK_SHORTAGE — timestamp change refused")
	if row.get("confidence") == CONFIDENCE_AMBIGUOUS or opt in ("AMBIGUOUS_DEPENDENCY", "AMBIGUOUS_RELATIONSHIP"):
		return _not_ready(decision, PLAN_AMBIGUOUS, "AMBIGUOUS — reconstruction sources disagree or relationship is unproven")
	if row.get("confidence") == CONFIDENCE_MANUAL or opt in (STATUS_MANUAL_REVIEW,):
		return _not_ready(decision, PLAN_MANUAL, f"MANUAL — {opt or 'operator review required'}")
	if row.get("confidence") and row.get("confidence") != CONFIDENCE_EXACT:
		return _not_ready(decision, PLAN_MANUAL, f"{row.get('confidence')} — Repair Selected requires EXACT")
	moves = _row_moves(row)
	if not moves:
		return _not_ready(decision, PLAN_BLOCKED, "No timestamp moves in preview — repair no longer needed or already applied")
	try:
		min_before = row.get("min_qty_before")
		if min_before is not None and flt(min_before) >= 0:
			return _not_ready(decision, PLAN_BLOCKED, "Revalidation failed: repair no longer needed")
		min_after = row.get("min_qty_after")
		if min_after is not None and flt(min_after) < 0:
			return _not_ready(decision, PLAN_BLOCKED, "Revalidation failed: proposed order still negative")
	except (TypeError, ValueError):
		pass
	in_t = row.get("current_inbound_time")
	out_t = row.get("current_outbound_time")
	if not in_t or not out_t:
		return _not_ready(decision, PLAN_BLOCKED, "Current posting date/time is required")
	from_dt = min(get_datetime(in_t), get_datetime(out_t))
	from erpnext_extensions.iran_accounting.historical_stock.scope import READY_SCOPES, evaluate_minimal_scope

	scope = evaluate_minimal_scope(row, cache=cache)
	decision["scope"] = scope
	if scope.get("window_loaded"):
		if scope.get("status") not in READY_SCOPES:
			return _not_ready(
				decision,
				scope.get("status") or PLAN_WAREHOUSE_ESCALATION,
				scope.get("escalation_reason") or scope.get("reason") or PLAN_WAREHOUSE_ESCALATION,
				dependency=scope.get("dependency_type"),
				prerequisite=(scope.get("local_poisons") or [{}])[0].get("voucher") if scope.get("local_poisons") else None,
			)
	else:
		identities = set()
		vw_cache = cache.setdefault("vw", {})
		for doc_move in moves:
			doc = doc_move.get("document")
			if not doc:
				continue
			if doc not in vw_cache:
				try:
					vw_cache[doc] = set(_voucher_item_warehouses(doc))
				except Exception:
					vw_cache[doc] = set()
			identities |= vw_cache[doc]
		if row.get("item") and row.get("warehouse"):
			identities.add((row["item"], row["warehouse"]))
		poison_cache = cache.setdefault("poison", {})
		for item_code, warehouse in identities:
			key = (item_code, warehouse, str(from_dt))
			if key not in poison_cache:
				poison_cache[key] = window_poison_hit(
					item_code, warehouse, from_dt, ignore_inversion_artifacts=True
				)
			hit = poison_cache[key]
			if hit:
				pair_vouchers = {row.get("inbound_document"), row.get("outbound_document")} | {
					m.get("document") for m in moves if m.get("document")
				}
				on_pair = hit.get("voucher") in pair_vouchers
				scope_txt = "on this pair" if on_pair else "on unrelated voucher — not an inversion artifact of this pair"
				detail = (
					f"{hit['reason']} on {hit.get('voucher') or ''} {hit.get('sle') or ''} "
					f"incoming_rate={hit.get('incoming_rate')} ({scope_txt})"
				).strip()
				prereq = hit.get("voucher") if not on_pair else None
				return _not_ready(
					decision,
					PLAN_WAITING_RATE_REPAIR if prereq else PLAN_WAITING_RATE_REPAIR,
					f"{STATUS_VALUATION_POISON}: {item_code} {warehouse} ({detail})",
					dependency=hit["reason"],
					prerequisite=prereq or hit.get("voucher"),
				)
	rev = _posting_revalidate(row, moves, from_dt, cache)
	if rev:
		return _not_ready(decision, PLAN_BLOCKED, rev)
	counts = _write_counts(row, cache)
	sql = counts["sql"]
	if sql <= 0:
		return _not_ready(decision, PLAN_BLOCKED, "SQL updates = 0. Repair Selected is disabled.")
	return _ready(
		decision,
		sql=sql,
		replay=1,
		rebuild=counts["sle"] + counts["sabb"],
		reason=(scope.get("reason") if scope.get("window_loaded") else "READY — posting-order EXACT pair, poison-free"),
		planner_status=(scope.get("status") if scope.get("window_loaded") and scope.get("status") in READY_SCOPES else PLAN_READY),
		se_count=counts["se"],
		sle_count=counts["sle"],
		sabb_count=counts["sabb"],
		sbe_count=counts["sbe"],
		bin_count=counts["bin"],
	)


def _evaluate_rate(row, decision, cache, patient) -> dict:
	status = str(row.get("status") or "")
	voucher = row.get("voucher") or row.get("voucher_no")
	confidence = row.get("confidence")
	topic = str(row.get("topic") or "")
	is_wrong_rate = topic in ("WRONG_RATE", "WRONG") or bool(row.get("flags") or row.get("mismatch_class"))
	# Phase 2: Wrong Rate uses explicit RATE_* / READY_WRONG_RATE (never generic BLOCKED).
	amb_status = PLAN_RATE_AMBIGUOUS if is_wrong_rate else PLAN_AMBIGUOUS
	man_status = PLAN_RATE_MANUAL if is_wrong_rate else PLAN_MANUAL
	wait_dep = PLAN_WAITING_RATE_DEPENDENCY if is_wrong_rate else PLAN_WAITING_RATE_REPAIR
	# Already-valued / rebuild-complete: rate surface is healthy (amount micro-gaps stay complete).
	if status == STATUS_RATE_REBUILD_COMPLETE or str(row.get("source") or row.get("source_of_truth") or "") == "already_valued":
		cur = flt(row.get("current") if row.get("current") is not None else row.get("current_rate"))
		exp = flt(row.get("expected") if row.get("expected") is not None else row.get("proposed_rate"))
		if abs(exp) > RATE_EPS and abs(cur - exp) <= 1:
			return _not_ready(
				decision,
				PLAN_RATE_REPAIR_COMPLETE,
				"RATE_REPAIR_COMPLETE — already valued / rates match",
				patient=patient,
			)
		if status == STATUS_RATE_REBUILD_COMPLETE and abs(cur) > RATE_EPS and abs(exp) <= RATE_EPS:
			# SE basic present; treat as complete even when attach_rate_analysis shows SLE current=0.
			return _not_ready(
				decision,
				PLAN_RATE_REPAIR_COMPLETE,
				"RATE_REPAIR_COMPLETE — already valued on SE",
				patient=patient,
			)
	if confidence == CONFIDENCE_AMBIGUOUS or status in ("AMBIGUOUS_DEPENDENCY", "AMBIGUOUS_RELATIONSHIP"):
		return _not_ready(decision, amb_status, "AMBIGUOUS — reconstruction sources disagree or relationship is unproven")
	if status in (STATUS_MANUAL_REVIEW, Z0_LEGITIMATE_ZERO) and confidence == CONFIDENCE_MANUAL:
		return _not_ready(decision, man_status, f"MANUAL — {status or 'operator review required'}")
	if confidence == CONFIDENCE_MANUAL or status == Z0_LEGITIMATE_ZERO:
		return _not_ready(decision, man_status, f"MANUAL — {status or 'operator review required'}")
	hit = _rate_poison_hit(row, cache)
	poison = (hit or {}).get("reason") or _rate_poison_reason(row, cache)
	poison_voucher = (hit or {}).get("voucher")
	if (poison or status == STATUS_VALUATION_POISON_DEPENDENCY) and poison_voucher and voucher and poison_voucher != voucher:
		return _not_ready(
			decision,
			wait_dep,
			f"{STATUS_VALUATION_POISON_DEPENDENCY}: {row.get('item') or row.get('item_code')} {row.get('warehouse') or ''} ({poison} on {poison_voucher})".strip(),
			patient=patient,
			prerequisite=poison_voucher,
			dependency=poison or status,
		)
	if status == Z0_LEGITIMATE_ZERO:
		return _not_ready(decision, man_status, "Z0 legitimate zero — no repair")
	# Resolve patient-zero waits that are only engine limitations.
	if str(row.get("dependency") or row.get("blocked_because") or "") == "circular_earliest_root":
		patient = None
		decision["patient_zero"] = voucher
		decision["dependency"] = "circular_earliest_root"
	elif patient and voucher and patient != voucher:
		if _rate_patient_cleared(patient, cache):
			patient = None
			decision["patient_zero"] = voucher
		elif _circular_earliest_is_self(voucher, patient, cache, row):
			patient = None
			decision["patient_zero"] = voucher
			decision["dependency"] = "circular_earliest_root"
	still_waiting_pz = bool(patient and voucher and patient != voucher)
	if still_waiting_pz:
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"{STATUS_DEPENDENCY_REPAIR_REQUIRED}: patient-zero is {patient or 'unknown'}",
			patient=patient,
			prerequisite=patient,
			dependency=poison or patient,
		)
	if row.get("surface") == "SLE" and patient and voucher and patient != voucher:
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"{STATUS_DEPENDENCY_REPAIR_REQUIRED}: patient-zero is {patient}",
			patient=patient,
			prerequisite=patient,
		)
	# SLE implied-SVD EXACT rows still go through zero-rate classify on apply.
	if row.get("surface") == "SLE" and not patient:
		patient = _lookup_patient(row, cache)
		if patient and patient != voucher:
			if _rate_patient_cleared(patient, cache):
				patient = None
			elif _circular_earliest_is_self(voucher, patient, cache, row):
				patient = None
			else:
				return _not_ready(
					decision,
					PLAN_WAITING_PATIENT_ZERO,
					f"{STATUS_DEPENDENCY_REPAIR_REQUIRED}: patient-zero is {patient}",
					patient=patient,
					prerequisite=patient,
				)
	if confidence == CONFIDENCE_LIKELY:
		return _not_ready(decision, man_status, "LIKELY — preview only; Repair Selected requires EXACT")
	if row.get("confidence") != CONFIDENCE_EXACT:
		return _not_ready(decision, man_status, f"{row.get('confidence') or 'unknown'} — Repair Selected requires EXACT")
	if status not in (STATUS_RECONSTRUCTABLE, "") and not row.get("eligible"):
		# Cleared dependency rows may still carry DEPENDENCY_REPAIR_REQUIRED status.
		if status == STATUS_DEPENDENCY_REPAIR_REQUIRED and patient in (None, voucher):
			status = STATUS_RECONSTRUCTABLE
		else:
			fallback = PLAN_RATE_MANUAL if is_wrong_rate else PLAN_NO_REPAIR_PATH
			return _not_ready(decision, fallback, f"Row not eligible ({status or 'unknown'})")
	if status == STATUS_RECONSTRUCTABLE or row.get("eligible") or (
		status == STATUS_DEPENDENCY_REPAIR_REQUIRED and patient in (None, voucher)
	):
		counts = _write_counts(row, cache)
		sql = counts["sql"]
		if sql <= 0:
			fallback = PLAN_RATE_MANUAL if is_wrong_rate else PLAN_NO_REPAIR_PATH
			return _not_ready(decision, fallback, "SQL updates = 0. Repair Selected is disabled.")
		ready_status = PLAN_READY_WRONG_RATE if is_wrong_rate else PLAN_READY
		return _ready(
			decision,
			sql=sql,
			replay=1,
			rebuild=counts["sle"],
			reason="READY_WRONG_RATE — EXACT reconstructable rate" if is_wrong_rate else "READY — EXACT reconstructable rate",
			planner_status=ready_status,
			se_count=counts["se"],
			sle_count=counts["sle"],
			sabb_count=counts["sabb"],
			sbe_count=counts["sbe"],
			bin_count=counts["bin"],
			patient_zero=patient or voucher,
		)
	fallback = PLAN_RATE_MANUAL if is_wrong_rate else PLAN_NO_REPAIR_PATH
	return _not_ready(decision, fallback, f"Row not eligible ({status or 'unknown'})")


def _rate_patient_cleared(patient: str, cache: dict) -> bool:
	"""True when the named patient-zero no longer needs a rate repair."""
	by_v = cache.get("rows_by_voucher") or {}
	prow = by_v.get(patient)
	if not prow:
		return False
	ps = str(prow.get("planner_status") or "")
	status = str(prow.get("status") or "")
	source = str(prow.get("source") or prow.get("source_of_truth") or "")
	if ps == PLAN_RATE_REPAIR_COMPLETE or status == STATUS_RATE_REBUILD_COMPLETE:
		return True
	if source == "already_valued":
		return True
	cur = flt(prow.get("current") if prow.get("current") is not None else prow.get("current_rate"))
	exp = flt(prow.get("expected") if prow.get("expected") is not None else prow.get("proposed_rate"))
	if abs(exp) > RATE_EPS and abs(cur - exp) <= 1 and prow.get("confidence") == CONFIDENCE_EXACT:
		# Rates already match — amount-only / complete surface.
		if status == STATUS_RATE_REBUILD_COMPLETE or source == "already_valued":
			return True
		flags = prow.get("flags") or []
		if flags and set(flags) <= {"WRONG_AMOUNT"}:
			return True
	return False


def _row_posting_key(row: dict) -> tuple:
	dt = row.get("posting_datetime") or row.get("posting_date") or ""
	tm = row.get("posting_time") or ""
	voucher = row.get("voucher") or row.get("voucher_no") or ""
	try:
		return (get_datetime(f"{dt} {tm}".strip() if tm else dt), voucher)
	except Exception:
		return (str(dt), str(tm), voucher)


def _circular_earliest_is_self(voucher: str, patient: str, cache: dict, row: dict) -> bool:
	"""A↔B patient-zero loops: elect the earliest posting voucher as the root."""
	by_v = cache.get("rows_by_voucher") or {}
	prow = by_v.get(patient)
	if not prow:
		return False
	ppz = _patient_name(prow)
	if not ppz or ppz != voucher:
		return False
	self_row = by_v.get(voucher) or row
	return _row_posting_key(self_row) <= _row_posting_key(prow)


def _evaluate_manufacture(row, decision, patient, cache) -> dict:
	status = str(row.get("status") or "")
	if status == STATUS_DEPENDENCY_REPAIR_REQUIRED:
		return _not_ready(
			decision,
			PLAN_WAITING_RATE_REPAIR,
			"WAITING_RATE_REPAIR — zero-rate components must be repaired first",
			prerequisite=patient,
		)
	if status != STATUS_RECONSTRUCTABLE or row.get("confidence") != CONFIDENCE_EXACT:
		return _not_ready(decision, PLAN_MANUAL if row.get("needs_repair") else PLAN_BLOCKED, f"{status} — manufacture not EXACT reconstructable")
	counts = _write_counts(row, cache)
	sql = max(counts["sql"], 1)
	return _ready(decision, sql=sql, replay=1, rebuild=counts["sle"], reason="READY — manufacture contract EXACT")


def _evaluate_gl(row, decision) -> dict:
	klass = row.get("gl_class") or row.get("status")
	voucher = row.get("voucher")
	if klass == G0_HEALTHY:
		return _not_ready(decision, PLAN_BLOCKED, "G0 preserved — GL rebuild refused")
	if klass == G4_POISONED_SLE or _gl_has_poison(voucher):
		return _not_ready(
			decision,
			PLAN_WAITING_SLE_REPAIR,
			f"WAITING_SLE_REPAIR — GL rebuild blocked until SLE is healthy ({voucher})",
			prerequisite=voucher,
		)
	if klass == G2_MISSING and voucher:
		# Material Transfer / empty GL map cannot be auto-rebuilt — MANUAL
		if not _gl_expected_map_nonempty(voucher):
			return _not_ready(
				decision,
				PLAN_MANUAL,
				"MANUAL — G2_MISSING but Stock Entry produced empty expected GL map",
			)
	if klass in (G1_ECONOMICALLY_WRONG, G2_MISSING, G3_UNBALANCED):
		return _ready(decision, sql=max(_gl_row_count(voucher), 1), rebuild=1, reason=f"READY — rebuild {klass}")
	return _not_ready(decision, PLAN_BLOCKED, f"GL class {klass} is not repairable")


def _gl_expected_map_nonempty(voucher) -> bool:
	if not voucher:
		return False
	try:
		import frappe
		from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

		if not frappe.db.exists("Stock Entry", voucher):
			return False
		se = frappe.get_doc("Stock Entry", voucher)
		expected = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
		return bool(expected)
	except Exception:
		return False


def _evaluate_riv(row, decision) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock import (
		RIV_DEADLOCK,
		RIV_NEGATIVE_STOCK,
		RIV_PERMANENTLY_UNSAFE,
		RIV_RAW_MATERIAL_COST,
		RIV_TIMEOUT,
		RIV_UNKNOWN,
		RIV_VALUATION_INTEGRITY,
		RIV_WAITING_PATIENT_ZERO,
		RIV_WAITING_REPLAY,
	)

	st = row.get("riv_status") or row.get("status")
	# Accept legacy WAITING_FOR_* strings
	if st in (RIV_WAITING_RATE, "WAITING_FOR_RATE_REPAIR"):
		return _not_ready(decision, PLAN_WAITING_RATE_REPAIR, "WAITING_RATE — Failed RIV waits on zero/wrong rate repair")
	if st in (RIV_WAITING_SLE, "WAITING_FOR_SLE_REPAIR"):
		return _not_ready(decision, PLAN_WAITING_SLE_REPAIR, "WAITING_SLE — Failed RIV waits on SLE replay")
	if st in (RIV_WAITING_GL, "WAITING_FOR_GL_REPAIR"):
		return _not_ready(decision, PLAN_WAITING_GL_REPAIR, "WAITING_GL — Failed RIV waits on GL rebuild")
	if st == RIV_WAITING_PATIENT_ZERO:
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"WAITING_PATIENT_ZERO — repair {row.get('patient_zero') or 'upstream'} first",
			patient=row.get("patient_zero"),
			prerequisite=row.get("patient_zero"),
		)
	if st == RIV_WAITING_REPLAY:
		return _not_ready(decision, PLAN_WAITING_SLE_REPAIR, "WAITING_REPLAY — identity replay incomplete")
	if st in (RIV_NEGATIVE_STOCK, RIV_RAW_MATERIAL_COST, RIV_VALUATION_INTEGRITY, RIV_PERMANENTLY_UNSAFE, "UNSAFE"):
		return _not_ready(decision, PLAN_MANUAL, f"{st} — Failed RIV must not be auto-retried")
	if st in (RIV_UNKNOWN,):
		return _not_ready(decision, PLAN_MANUAL, "UNKNOWN — Failed RIV needs operator review")
	if st in (RIV_SAFE_TO_RETRY, RIV_DEADLOCK, RIV_TIMEOUT):
		# Deadlock/Timeout only READY when classifier already promoted to SAFE_TO_RETRY
		if st != RIV_SAFE_TO_RETRY:
			return _not_ready(decision, PLAN_WAITING_RIV, f"{st} — not yet SAFE_TO_RETRY")
		return _ready(decision, sql=1, reason="READY — SAFE_TO_RETRY")
	return _not_ready(decision, PLAN_WAITING_RIV, f"{st or 'WAITING_RIV'} — Failed RIV is not READY")


def _evaluate_sle_bin(row, decision, patient) -> dict:
	# I4 leftover on SLE/Bin tab routes to the I4 planner (not Wrong Rate).
	reason = str(row.get("reason") or "")
	poison = reason
	if "qty_after_zero_nonzero_value" in reason or row.get("repair_class") == I4_LEFTOVER_REPAIR:
		return _evaluate_i4(row, decision, patient)
	st = str(row.get("status") or "")
	if st == SLE_POISONED_CHAIN:
		return _not_ready(decision, PLAN_BLOCKED, "POISONED_CHAIN — SLE identity is not replay-safe")
	voucher = row.get("voucher") or row.get("voucher_no")
	if patient and voucher and str(patient) == str(voucher) and poison in (
		"qty_after_zero_nonzero_value",
		"qty_zero_nonzero_value",
	):
		return _evaluate_i4(row, decision, patient)
	if st == SLE_PATIENT_ZERO_REQUIRED or (patient and str(patient) != str(voucher or "")):
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"WAITING_PATIENT_ZERO — repair {patient or 'patient-zero'} first",
			patient=patient,
			prerequisite=patient,
		)
	if patient and str(patient) == str(voucher or ""):
		# Self patient-zero of a non-I4 class still waits for that class's repair path.
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"WAITING_PATIENT_ZERO — this voucher is the patient zero ({poison or st})",
			patient=patient,
			prerequisite=patient,
		)
	return _not_ready(decision, PLAN_WAITING_SLE_REPAIR, "WAITING_SLE_REPAIR — use Replay Downstream / Rebuild after rate repair")


def _evaluate_i4(row, decision, patient) -> dict:
	"""Patient Zero leftover is READY_I4; downstream leftover waits on the root."""
	voucher = row.get("voucher") or row.get("voucher_no")
	i4_status = str(row.get("i4_status") or row.get("status") or "")
	pz = patient or _patient_name(row)
	replay = cint(row.get("replay_count") or row.get("sql_updates_estimate") or 0)
	sql = cint(row.get("sql_updates") or 0)
	if sql <= 0:
		sql = max(1, replay)  # at least the PZ SLE + identity replay estimate
	residual = flt(
		row.get("residual_value")
		if row.get("residual_value") is not None
		else row.get("current_value")
		if row.get("current_value") is not None
		else row.get("stock_value")
		if row.get("stock_value") is not None
		else row.get("residual_stock_value")
		if row.get("residual_stock_value") is not None
		else 0
	)
	reason_ok = str(row.get("reason") or "")
	leftover_reason = "qty_after_zero" in reason_ok or row.get("repair_class") == I4_LEFTOVER_REPAIR
	# Explicit cleared state only when residual is known-zero / repaired flag.
	if i4_status == I4_REPAIRED or (
		row.get("qty_becomes_zero")
		and abs(residual) <= 1
		and i4_status in (I4_REPAIRED, "")
	):
		return _not_ready(decision, PLAN_I4_REPAIRED, "I4_REPAIRED — leftover already cleared")

	is_self_pz = bool(row.get("is_patient_zero")) or (pz and voucher and str(pz) == str(voucher))
	has_leftover = abs(residual) > 1 or (leftover_reason and residual == 0 and sql > 0) or i4_status == I4_READY
	prev_healthy = row.get("previous_healthy")
	pz_clears = row.get("pz_residual_clears_in_sim")
	prev_qty = row.get("previous_qty")
	# Opening must be healthy and simulation must clear PZ residual — otherwise MANUAL.
	if i4_status == "MANUAL" or prev_healthy is False or pz_clears is False:
		blocker = row.get("previous_blocker") or row.get("message") or (
			"Previous SLE poisoned / simulation does not clear Patient Zero residual"
		)
		return _not_ready(decision, PLAN_MANUAL, f"MANUAL — {blocker}", patient=pz)
	if prev_qty is not None and flt(prev_qty) < -0.0001:
		return _not_ready(
			decision,
			PLAN_MANUAL,
			f"MANUAL — previous SLE negative qty_after={prev_qty}; not auto-READY_I4",
			patient=pz,
		)

	if is_self_pz and leftover_reason and has_leftover and i4_status in (I4_READY, "", "READY"):
		return _ready(
			decision,
			sql=max(sql, 1),
			replay=max(replay, 1),
			rebuild=1,
			reason="READY_I4 — Identity leftover detected.",
			planner_status=PLAN_READY_I4,
			sle_count=replay or sql,
			bin_count=1,
			se_count=1,
		)
	if pz and voucher and str(pz) != str(voucher):
		return _not_ready(
			decision,
			PLAN_WAITING_I4,
			f"WAITING_I4 — repair I4 Patient Zero {pz} first",
			patient=pz,
			prerequisite=pz,
		)
	if i4_status == I4_WAITING or (leftover_reason and not is_self_pz):
		return _not_ready(
			decision,
			PLAN_WAITING_I4,
			f"WAITING_I4 — repair I4 Patient Zero {pz or 'root'} first",
			patient=pz,
			prerequisite=pz,
		)
	if i4_status == I4_REPLAY_REQUIRED:
		return _not_ready(
			decision,
			PLAN_I4_REPLAY_REQUIRED,
			"I4_REPLAY_REQUIRED — leftover root repaired; finish identity replay",
			patient=pz,
		)
	if leftover_reason and is_self_pz:
		# Fallback only when classify omitted health fields but status is READY_I4.
		if i4_status == I4_READY:
			return _ready(
				decision,
				sql=max(sql, 1),
				replay=max(replay, 1),
				rebuild=1,
				reason="READY_I4 — Identity leftover detected.",
				planner_status=PLAN_READY_I4,
				sle_count=replay or sql,
				bin_count=1,
				se_count=1,
			)
		return _not_ready(
			decision,
			PLAN_MANUAL,
			"MANUAL — I4 Patient Zero needs healthy previous SLE before auto-repair",
			patient=pz,
		)
	return _not_ready(decision, PLAN_NO_REPAIR_PATH, "NO_REPAIR_PATH — not an I4 leftover")


def _posting_revalidate(row, moves, from_dt, cache) -> str | None:
	item = row.get("item")
	warehouse = row.get("warehouse")
	if not item or not warehouse:
		return None
	key = ("sim", item, warehouse, row.get("batch") or "", str(from_dt))
	if key in cache:
		return cache[key]
	reason = None
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import simulate_running
		from erpnext_extensions.iran_accounting.stock_posting_order.repair import _identity_window
		from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

		sles = _identity_window(item, warehouse, row.get("batch"), from_dt)
		if sles:
			opening = D(row.get("opening_qty") or 0)
			times = {m["document"]: get_datetime(m["new"]) for m in moves}
			times.setdefault(
				row["inbound_document"],
				get_datetime(row.get("proposed_inbound_time") or row["current_inbound_time"]),
			)
			times.setdefault(row["outbound_document"], get_datetime(row["proposed_outbound_time"]))
			current = simulate_running(sles, opening)
			proposed = simulate_running(sles, opening, times)
			if current["min_qty"] >= 0:
				reason = "Revalidation failed: repair no longer needed"
			elif proposed["min_qty"] < 0:
				reason = "Revalidation failed: proposed order still negative"
			elif proposed["final_qty"] != current["final_qty"]:
				reason = "Revalidation failed: final qty would change"
	except Exception:
		reason = None
	cache[key] = reason
	return reason


def _rate_poison_hit(row, cache) -> dict | None:
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	from_dt = row.get("posting_datetime") or row.get("posting_date")
	if not item or not warehouse or not from_dt:
		return None
	key = ("poison_hit", item, warehouse, str(from_dt))
	if key in cache:
		return cache[key]
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import window_poison_hit

		cache[key] = window_poison_hit(item, warehouse, from_dt, ignore_inversion_artifacts=True)
	except Exception:
		cache[key] = None
	return cache[key]


def _rate_poison_reason(row, cache) -> str | None:
	hit = _rate_poison_hit(row, cache)
	if hit:
		return hit.get("reason")
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	from_dt = row.get("posting_datetime") or row.get("posting_date")
	if not item or not warehouse or not from_dt:
		return None
	key = ("poison_rate", item, warehouse, str(from_dt))
	if key in cache:
		return cache[key]
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import window_poison_reason

		cache[key] = window_poison_reason(item, warehouse, from_dt, ignore_inversion_artifacts=True)
	except Exception:
		cache[key] = None
	return cache[key]


def _lookup_patient(row, cache) -> str | None:
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse")
	batch = row.get("batch")
	if not item or not warehouse:
		return None
	key = ("pz", item, warehouse, batch or "")
	if key in cache:
		return cache[key]
	try:
		from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero_identity

		found = find_patient_zero_identity(item, warehouse, batch)
		name = None
		if isinstance(found, dict):
			name = found.get("voucher_no") or found.get("voucher")
		cache[key] = name
		return name
	except Exception:
		cache[key] = None
		return None


def _write_counts(row, cache) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.impact import (
		_count_bins,
		_count_sabb,
		_count_sbe,
		_count_sle,
		collect_vouchers,
	)

	vouchers = collect_vouchers([row])
	key = ("wc", tuple(vouchers), row.get("item") or row.get("item_code"), row.get("warehouse"))
	hit = cache.get(key)
	if hit:
		return hit
	items = [row.get("item") or row.get("item_code")] if (row.get("item") or row.get("item_code")) else []
	warehouses = [row.get("warehouse")] if row.get("warehouse") else []
	sle = cint(_count_sle(vouchers, items, warehouses)) if vouchers else 0
	sabb = cint(_count_sabb(vouchers)) if vouchers else 0
	sbe = cint(_count_sbe(vouchers)) if vouchers else 0
	bins = cint(_count_bins(items, warehouses)) if items and warehouses else 0
	se = len(vouchers)
	out = {"se": se, "sle": sle, "sabb": sabb, "sbe": sbe, "bin": bins, "sql": se + sle + sabb + sbe + bins}
	cache[key] = out
	return out


def _gl_has_poison(voucher) -> bool:
	if not voucher:
		return False
	try:
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import _voucher_has_poison_sle

		return bool(_voucher_has_poison_sle(voucher))
	except Exception:
		return False


def _gl_row_count(voucher) -> int:
	if not voucher:
		return 0
	import frappe

	try:
		return cint(
			frappe.db.sql(
				"""SELECT COUNT(*) FROM `tabGL Entry`
				   WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0""",
				voucher,
			)[0][0]
		)
	except Exception:
		return 0
