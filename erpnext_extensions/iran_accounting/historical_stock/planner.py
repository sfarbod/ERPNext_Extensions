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
	RIV_SAFE_TO_RETRY,
	RIV_UNSAFE,
	RIV_WAITING_GL,
	RIV_WAITING_RATE,
	RIV_WAITING_SLE,
	SLE_PATIENT_ZERO_REQUIRED,
	SLE_POISONED_CHAIN,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_RECONSTRUCTABLE,
	STATUS_VALUATION_POISON_DEPENDENCY,
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
	return out


def attach_plan_many(rows: list | None, *, cache: dict | None = None) -> list:
	cache = cache if cache is not None else {}
	return [attach_plan(r, cache=cache) for r in rows or []]


def stamp_scan_result(result: dict | None, *, cache: dict | None = None) -> dict:
	"""Stamp Scan/Dashboard payloads so eligible never diverges from Apply."""
	out = dict(result or {})
	rows = attach_plan_many(out.get("rows") or [], cache=cache)
	out["rows"] = rows
	out["eligible"] = [r for r in rows if r.get("eligible")]
	out["repairable"] = sum(
		1 for r in rows if r.get("planner_status") == PLAN_READY and cint(r.get("sql_updates")) > 0
	)
	return out


def plan_selection(rows: list | None) -> dict:
	"""Repair Planner for the current selection. Used by Impact and the Desk."""
	cache = {}
	rows = list(rows or [])
	decisions = [evaluate_row(r, cache=cache) for r in rows]
	planned_rows = [attach_plan(r, cache=cache) for r in rows]
	blockers = [d for d in decisions if d["planner_status"] != PLAN_READY or cint(d["sql_updates"]) <= 0]
	ready = [d for d in decisions if d["planner_status"] == PLAN_READY and cint(d["sql_updates"]) > 0]
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
		"planner_status": PLAN_READY if all_ready and sql > 0 else (blockers[0]["planner_status"] if blockers else PLAN_BLOCKED),
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
	from erpnext_extensions.iran_accounting.historical_stock.impact import format_impact

	plan["preview_text"] = format_impact(plan)
	return plan


def assert_ready(row: dict, *, cache: dict | None = None) -> dict:
	"""Apply-time gate. Raises with the same reason Scan/Impact already showed."""
	import frappe

	decision = evaluate_row(row, cache=cache)
	if decision["planner_status"] != PLAN_READY or cint(decision["sql_updates"]) <= 0:
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


def _ready(decision, *, sql, replay=0, rebuild=0, reason="READY", **counts) -> dict:
	decision["planner_status"] = PLAN_READY
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
			scope = "on this pair" if on_pair else "on unrelated voucher — not an inversion artifact of this pair"
			detail = (
				f"{hit['reason']} on {hit.get('voucher') or ''} {hit.get('sle') or ''} "
				f"incoming_rate={hit.get('incoming_rate')} ({scope})"
			).strip()
			return _not_ready(
				decision,
				PLAN_BLOCKED,
				f"{STATUS_VALUATION_POISON}: {item_code} {warehouse} ({detail})",
				dependency=hit["reason"],
				prerequisite=hit.get("voucher") if not on_pair else None,
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
		reason="READY — posting-order EXACT pair, poison-free",
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
	if confidence == CONFIDENCE_AMBIGUOUS or status in ("AMBIGUOUS_DEPENDENCY", "AMBIGUOUS_RELATIONSHIP"):
		return _not_ready(decision, PLAN_AMBIGUOUS, "AMBIGUOUS — reconstruction sources disagree or relationship is unproven")
	if confidence == CONFIDENCE_MANUAL or status in (STATUS_MANUAL_REVIEW, Z0_LEGITIMATE_ZERO):
		return _not_ready(decision, PLAN_MANUAL, f"MANUAL — {status or 'operator review required'}")
	if confidence == CONFIDENCE_LIKELY:
		return _not_ready(decision, PLAN_MANUAL, "LIKELY — preview only; Repair Selected requires EXACT")
	poison = _rate_poison_reason(row, cache)
	if poison or status == STATUS_VALUATION_POISON_DEPENDENCY:
		return _not_ready(
			decision,
			PLAN_BLOCKED,
			f"{STATUS_VALUATION_POISON_DEPENDENCY}: {row.get('item') or row.get('item_code')} {row.get('warehouse') or ''} ({poison or 'poisoned SLE'})".strip(),
			patient=patient,
			dependency=poison or status,
		)
	if status == Z0_LEGITIMATE_ZERO:
		return _not_ready(decision, PLAN_MANUAL, "Z0 legitimate zero — no repair")
	if status == STATUS_DEPENDENCY_REPAIR_REQUIRED or (patient and voucher and patient != voucher):
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"{STATUS_DEPENDENCY_REPAIR_REQUIRED}: patient-zero is {patient or 'unknown'}",
			patient=patient,
			prerequisite=patient,
			dependency=patient,
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
			return _not_ready(
				decision,
				PLAN_WAITING_PATIENT_ZERO,
				f"{STATUS_DEPENDENCY_REPAIR_REQUIRED}: patient-zero is {patient}",
				patient=patient,
				prerequisite=patient,
			)
	if row.get("confidence") != CONFIDENCE_EXACT:
		return _not_ready(decision, PLAN_MANUAL, f"{row.get('confidence') or 'unknown'} — Repair Selected requires EXACT")
	if status not in (STATUS_RECONSTRUCTABLE, "") and not row.get("eligible"):
		return _not_ready(decision, PLAN_BLOCKED, f"Row not eligible ({status or 'unknown'})")
	if status == STATUS_RECONSTRUCTABLE or row.get("eligible"):
		counts = _write_counts(row, cache)
		sql = counts["sql"]
		if sql <= 0:
			return _not_ready(decision, PLAN_BLOCKED, "SQL updates = 0. Repair Selected is disabled.")
		return _ready(
			decision,
			sql=sql,
			replay=1,
			rebuild=counts["sle"],
			reason="READY — EXACT reconstructable rate",
			se_count=counts["se"],
			sle_count=counts["sle"],
			sabb_count=counts["sabb"],
			sbe_count=counts["sbe"],
			bin_count=counts["bin"],
			patient_zero=patient or voucher,
		)
	return _not_ready(decision, PLAN_BLOCKED, f"Row not eligible ({status or 'unknown'})")


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
	if klass in (G1_ECONOMICALLY_WRONG, G2_MISSING, G3_UNBALANCED):
		return _ready(decision, sql=max(_gl_row_count(voucher), 1), rebuild=1, reason=f"READY — rebuild {klass}")
	return _not_ready(decision, PLAN_BLOCKED, f"GL class {klass} is not repairable")


def _evaluate_riv(row, decision) -> dict:
	st = row.get("riv_status") or row.get("status")
	if st == RIV_WAITING_RATE:
		return _not_ready(decision, PLAN_WAITING_RATE_REPAIR, "WAITING_RATE_REPAIR — Failed RIV waits on zero/wrong rate repair")
	if st == RIV_WAITING_SLE:
		return _not_ready(decision, PLAN_WAITING_SLE_REPAIR, "WAITING_SLE_REPAIR — Failed RIV waits on SLE replay")
	if st == RIV_WAITING_GL:
		return _not_ready(decision, PLAN_WAITING_GL_REPAIR, "WAITING_GL_REPAIR — Failed RIV waits on GL rebuild")
	if st == RIV_UNSAFE:
		return _not_ready(decision, PLAN_BLOCKED, "UNSAFE — Failed RIV must not be retried")
	if st == RIV_SAFE_TO_RETRY:
		return _ready(decision, sql=1, reason="READY — SAFE_TO_RETRY")
	return _not_ready(decision, PLAN_WAITING_RIV, f"{st or 'WAITING_RIV'} — Failed RIV is not READY")


def _evaluate_sle_bin(row, decision, patient) -> dict:
	st = str(row.get("status") or "")
	if st == SLE_POISONED_CHAIN:
		return _not_ready(decision, PLAN_BLOCKED, "POISONED_CHAIN — SLE identity is not replay-safe")
	if st == SLE_PATIENT_ZERO_REQUIRED or patient:
		return _not_ready(
			decision,
			PLAN_WAITING_PATIENT_ZERO,
			f"WAITING_PATIENT_ZERO — repair {patient or 'patient-zero'} first",
			patient=patient,
			prerequisite=patient,
		)
	return _not_ready(decision, PLAN_WAITING_SLE_REPAIR, "WAITING_SLE_REPAIR — use Replay Downstream / Rebuild after rate repair")


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


def _rate_poison_reason(row, cache) -> str | None:
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
		from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero

		found = find_patient_zero(item, warehouse, batch)
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
