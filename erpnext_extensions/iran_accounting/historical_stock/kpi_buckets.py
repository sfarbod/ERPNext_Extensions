# Copyright (c) 2026, ERPNext Extensions contributors
"""Canonical Historical Repair KPI bucket membership (single source of truth).

Dashboard chips, topic Scan filters, Master Plan, and repairability MUST use
these definitions. Do not duplicate status lists in JavaScript.
"""

from __future__ import annotations

from typing import Iterable

from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_AMBIGUOUS,
	PLAN_MANUAL,
	PLAN_RATE_AMBIGUOUS,
	PLAN_RATE_MANUAL,
	PLAN_RATE_POISONED_OPENING,
	PLAN_RATE_REPAIR_COMPLETE,
	PLAN_RATE_REPLAY_REQUIRED,
	PLAN_RATE_WAREHOUSE_ESCALATION,
	PLAN_READY_WRONG_RATE,
	PLAN_WAITING_PATIENT_ZERO,
	PLAN_WAITING_RATE_DEPENDENCY,
	PLAN_WAITING_RATE_REPAIR,
	PLAN_READY_I4,
	PLAN_WAITING_I4,
	PLAN_I4_REPLAY_REQUIRED,
	PLAN_READY_I1,
	PLAN_WAITING_I1,
	PLAN_I1_MANUAL,
	PLAN_I1_REPAIRED,
)

# ---------------------------------------------------------------------------
# Wrong Rate
# ---------------------------------------------------------------------------

WRONG_RATE_READY_STATUSES = frozenset({PLAN_READY_WRONG_RATE})
WRONG_RATE_WAITING_STATUSES = frozenset(
	{
		PLAN_WAITING_PATIENT_ZERO,
		PLAN_WAITING_RATE_DEPENDENCY,
		PLAN_WAITING_RATE_REPAIR,
	}
)
WRONG_RATE_MANUAL_STATUSES = frozenset(
	{
		PLAN_RATE_MANUAL,
		PLAN_RATE_AMBIGUOUS,
		PLAN_MANUAL,
		PLAN_AMBIGUOUS,
		PLAN_RATE_POISONED_OPENING,
		PLAN_RATE_WAREHOUSE_ESCALATION,
	}
)
WRONG_RATE_COMPLETE_STATUSES = frozenset({PLAN_RATE_REPAIR_COMPLETE})
# Replay-required is unresolved but neither READY nor MANUAL problem card.
WRONG_RATE_REPLAY_STATUSES = frozenset({PLAN_RATE_REPLAY_REQUIRED})

WRONG_RATE_ACTIVE_STATUSES = (
	WRONG_RATE_READY_STATUSES
	| WRONG_RATE_WAITING_STATUSES
	| WRONG_RATE_MANUAL_STATUSES
	| WRONG_RATE_REPLAY_STATUSES
)

# ---------------------------------------------------------------------------
# I4 (SLE surface)
# ---------------------------------------------------------------------------

I4_READY_STATUSES = frozenset({PLAN_READY_I4, "READY_I4"})
I4_WAITING_STATUSES = frozenset({PLAN_WAITING_I4, "WAITING_I4", "I4_WAITING"})
I4_MANUAL_STATUSES = frozenset({PLAN_MANUAL, "MANUAL"})
I4_REPLAY_STATUSES = frozenset({PLAN_I4_REPLAY_REQUIRED, "I4_REPLAY_REQUIRED"})

# ---------------------------------------------------------------------------
# I1 (Manufacture negative incoming rate)
# ---------------------------------------------------------------------------

I1_READY_STATUSES = frozenset({PLAN_READY_I1, "READY_I1"})
I1_WAITING_STATUSES = frozenset({PLAN_WAITING_I1, "WAITING_I1"})
I1_MANUAL_STATUSES = frozenset({PLAN_I1_MANUAL, "MANUAL_I1"})
I1_COMPLETE_STATUSES = frozenset({PLAN_I1_REPAIRED, "I1_REPAIRED"})

# ---------------------------------------------------------------------------
# GL
# ---------------------------------------------------------------------------

GL_READY_PREFIX = "READY"
GL_WAITING_TOKEN = "WAITING"
GL_MANUAL_STATUSES = frozenset({"MANUAL", "NO_REPAIR_PATH", "BLOCKED"})

# ---------------------------------------------------------------------------
# RIV
# ---------------------------------------------------------------------------

RIV_SAFE_STATUSES = frozenset({"SAFE_TO_RETRY"})
RIV_WAITING_STATUSES = frozenset(
	{
		"WAITING_RATE",
		"WAITING_SLE",
		"WAITING_GL",
		"WAITING_PATIENT_ZERO",
		"WAITING_REPLAY",
		"WAITING_FOR_RATE_REPAIR",
		"WAITING_FOR_SLE_REPAIR",
		"WAITING_FOR_GL_REPAIR",
	}
)
RIV_UNSAFE_STATUSES = frozenset(
	{
		"NEGATIVE_STOCK",
		"RAW_MATERIAL_COST",
		"VALUATION_INTEGRITY",
		"PERMANENTLY_UNSAFE",
		"UNSAFE",
		"UNKNOWN",
	}
)

# KPI label → bucket id (backend + UI contract)
KPI_BUCKETS = {
	"Wrong Rate": "wrong_rate_active",
	"Wrong Rate READY": "wrong_rate_ready",
	"Wrong Rate WAITING": "wrong_rate_waiting",
	"Wrong Rate MANUAL": "wrong_rate_manual",
	"Wrong Rate Complete": "wrong_rate_complete",
	"READY_I4": "i4_ready",
	"WAITING_I4": "i4_waiting",
	"MANUAL_I4": "i4_manual",
	"REPLAY_REQUIRED_I4": "i4_replay",
	"I4 Leftover": "i4_all",
	"READY_I1": "i1_ready",
	"WAITING_I1": "i1_waiting",
	"MANUAL_I1": "i1_manual",
	"I1 Negative Rate": "i1_all",
	"GL READY": "gl_ready",
	"GL WAITING": "gl_waiting",
	"GL MANUAL": "gl_manual",
	"Broken GL": "gl_all",
	"RIV SAFE": "riv_safe",
	"RIV WAITING": "riv_waiting",
	"RIV UNSAFE": "riv_unsafe",
	"Failed RIV": "riv_all",
}


def _ps(row: dict) -> str:
	return str((row or {}).get("planner_status") or (row or {}).get("i4_status") or (row or {}).get("riv_status") or "")


def wrong_rate_bucket(row: dict) -> str | None:
	"""Return ready|waiting|manual|complete|replay|other for a Wrong Rate row."""
	ps = _ps(row)
	# Phase 5: foreign patient-zero is WAITING even if planner left RATE_AMBIGUOUS.
	pz = row.get("patient_zero") if isinstance(row, dict) else None
	pz_v = None
	if isinstance(pz, dict):
		pz_v = pz.get("voucher_no") or pz.get("voucher")
	elif pz:
		pz_v = str(pz)
	voucher = (row or {}).get("voucher") or (row or {}).get("voucher_no")
	if pz_v and voucher and pz_v != voucher and ps in WRONG_RATE_MANUAL_STATUSES:
		return "waiting"
	if ps in WRONG_RATE_COMPLETE_STATUSES:
		return "complete"
	if ps in WRONG_RATE_READY_STATUSES and int((row or {}).get("sql_updates") or 0) > 0:
		return "ready"
	if ps in WRONG_RATE_READY_STATUSES:
		# READY label without sql → treat as unresolved other (not READY card)
		return "other"
	if ps in WRONG_RATE_WAITING_STATUSES:
		return "waiting"
	if ps in WRONG_RATE_MANUAL_STATUSES:
		return "manual"
	if ps in WRONG_RATE_REPLAY_STATUSES:
		return "replay"
	return "other" if ps else None


def row_matches_kpi_bucket(row: dict, bucket: str) -> bool:
	"""True if row belongs to the named KPI bucket."""
	bucket = str(bucket or "").strip()
	if not bucket:
		return True
	ps = _ps(row)
	topic = str((row or {}).get("topic") or "")

	if bucket == "wrong_rate_active":
		return wrong_rate_bucket(row) in ("ready", "waiting", "manual", "replay", "other")
	if bucket == "wrong_rate_ready":
		return wrong_rate_bucket(row) == "ready"
	if bucket == "wrong_rate_waiting":
		return wrong_rate_bucket(row) == "waiting"
	if bucket == "wrong_rate_manual":
		return wrong_rate_bucket(row) == "manual"
	if bucket == "wrong_rate_complete":
		return wrong_rate_bucket(row) == "complete"

	if bucket == "i4_ready":
		return ps in I4_READY_STATUSES or (
			bool((row or {}).get("eligible")) and ps.startswith("READY")
		)
	if bucket == "i4_waiting":
		return ps in I4_WAITING_STATUSES
	if bucket == "i4_manual":
		return ps in I4_MANUAL_STATUSES and not (row or {}).get("eligible")
	if bucket == "i4_replay":
		return ps in I4_REPLAY_STATUSES
	if bucket == "i4_all":
		return (
			ps in I4_READY_STATUSES
			or ps in I4_WAITING_STATUSES
			or ps in I4_MANUAL_STATUSES
			or ps in I4_REPLAY_STATUSES
			or (row or {}).get("repair_class") == "I4_LEFTOVER_REPAIR"
			or topic == "I4_LEFTOVER"
		)

	if bucket == "i1_ready":
		return ps in I1_READY_STATUSES and int((row or {}).get("sql_updates") or 0) > 0
	if bucket == "i1_waiting":
		return ps in I1_WAITING_STATUSES
	if bucket == "i1_manual":
		return ps in I1_MANUAL_STATUSES and not (row or {}).get("eligible")
	if bucket == "i1_all":
		return (
			ps in I1_READY_STATUSES
			or ps in I1_WAITING_STATUSES
			or ps in I1_MANUAL_STATUSES
			or ps in I1_COMPLETE_STATUSES
			or (row or {}).get("repair_class") == "I1_NEGATIVE_RATE_REPAIR"
			or topic == "I1_NEGATIVE_RATE"
		)

	if bucket == "gl_ready":
		return (
			bool((row or {}).get("eligible"))
			and ps.startswith(GL_READY_PREFIX)
			and int((row or {}).get("sql_updates") or 0) > 0
			and not (row or {}).get("sle_poisoned")
		)
	if bucket == "gl_waiting":
		return GL_WAITING_TOKEN in ps or (row or {}).get("gl_role") in (
			"WAITING_SLE",
			"WAITING_RATE",
			"WAITING_REPLAY",
		) or bool((row or {}).get("sle_poisoned"))
	if bucket == "gl_manual":
		return ps in GL_MANUAL_STATUSES and not (row or {}).get("eligible")
	if bucket == "gl_all":
		return True  # GL scan rows are already GL anomalies

	riv_st = str((row or {}).get("riv_status") or (row or {}).get("planner_status") or "")
	if bucket == "riv_safe":
		return riv_st in RIV_SAFE_STATUSES
	if bucket == "riv_waiting":
		return riv_st in RIV_WAITING_STATUSES
	if bucket == "riv_unsafe":
		return riv_st in RIV_UNSAFE_STATUSES
	if bucket == "riv_all":
		return True

	# Fallback: treat bucket as exact planner_status or CSV of statuses
	if "," in bucket:
		wanted = {s.strip() for s in bucket.split(",") if s.strip()}
		return ps in wanted
	return ps == bucket


def filter_rows_by_kpi_bucket(rows: Iterable[dict], bucket: str | None) -> list[dict]:
	if not bucket:
		return list(rows or [])
	return [r for r in (rows or []) if row_matches_kpi_bucket(r, bucket)]


def count_wrong_rate_buckets(rows: Iterable[dict]) -> dict:
	"""Dashboard WR sub-KPI counts from stamped rows."""
	out = {
		"ready": 0,
		"waiting": 0,
		"manual": 0,
		"complete": 0,
		"replay": 0,
		"other": 0,
		"active": 0,
		"total": 0,
	}
	for row in rows or []:
		out["total"] += 1
		b = wrong_rate_bucket(row) or "other"
		out[b] = out.get(b, 0) + 1
		if b != "complete":
			out["active"] += 1
	return out


def assert_wrong_rate_invariants(row: dict) -> list[str]:
	"""Return list of invariant violations (empty = OK)."""
	errs = []
	b = wrong_rate_bucket(row)
	ps = _ps(row)
	req = bool((row or {}).get("repair_required"))
	sql = int((row or {}).get("sql_updates") or 0)
	if b == "complete":
		if req:
			errs.append("RATE_REPAIR_COMPLETE must have repair_required=false")
		if sql > 0:
			errs.append("RATE_REPAIR_COMPLETE must have sql_updates=0")
		if b in ("ready", "waiting", "manual"):
			errs.append("complete cannot also be active bucket")
	if b == "ready":
		if not req and sql <= 0:
			errs.append("READY_WRONG_RATE should be repairable (sql_updates>0)")
		if sql <= 0:
			errs.append("READY_WRONG_RATE requires sql_updates>0")
	if b == "manual" and ps in WRONG_RATE_COMPLETE_STATUSES:
		errs.append("COMPLETE must not classify as MANUAL")
	if b == "waiting" and ps in WRONG_RATE_COMPLETE_STATUSES:
		errs.append("COMPLETE must not classify as WAITING")
	return errs


def kpi_bucket_for_label(label: str) -> str | None:
	return KPI_BUCKETS.get(label)


def statuses_for_bucket(bucket: str) -> list[str]:
	"""Status list for UI display / Select filters (informational)."""
	return {
		"wrong_rate_ready": sorted(WRONG_RATE_READY_STATUSES),
		"wrong_rate_waiting": sorted(WRONG_RATE_WAITING_STATUSES),
		"wrong_rate_manual": sorted(WRONG_RATE_MANUAL_STATUSES),
		"wrong_rate_complete": sorted(WRONG_RATE_COMPLETE_STATUSES),
		"wrong_rate_active": sorted(WRONG_RATE_ACTIVE_STATUSES),
		"i4_ready": sorted(I4_READY_STATUSES),
		"i4_waiting": sorted(I4_WAITING_STATUSES),
		"i4_manual": sorted(I4_MANUAL_STATUSES),
		"i4_replay": sorted(I4_REPLAY_STATUSES),
		"i1_ready": sorted(I1_READY_STATUSES),
		"i1_waiting": sorted(I1_WAITING_STATUSES),
		"i1_manual": sorted(I1_MANUAL_STATUSES),
		"riv_safe": sorted(RIV_SAFE_STATUSES),
		"riv_waiting": sorted(RIV_WAITING_STATUSES),
		"riv_unsafe": sorted(RIV_UNSAFE_STATUSES),
	}.get(bucket, [])
