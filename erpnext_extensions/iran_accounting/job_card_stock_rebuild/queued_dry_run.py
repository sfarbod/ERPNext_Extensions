# Copyright (c) 2026, ERPNext Extensions contributors
"""Queued Manufacture Repair Dry Run orchestration (v5.5.3).

ONE background job executes the entire existing ``run_repair`` Dry Run engine on
ONE DB connection / business transaction, then rolls back. Progress / result
live in Redis (outside the business SQL transaction).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
	run_repair,
)

QUEUE_NAME = "long"
JOB_TIMEOUT = 600  # seconds — Staging Dry Run may exceed 90s HTTP boundary
STATUS_TTL = 24 * 60 * 60
LOCK_TTL = 20 * 60  # stale active-run recovery
POLL_STALE_RUNNING_SEC = 15 * 60

TERMINAL = frozenset({"PASS", "FAILED", "STALE_PLAN", "BLOCKED", "CANCELLED"})

PHASE_PROGRESS = {
	"QUEUED": 0,
	"PREPARING": 5,
	"LOCKING": 10,
	"FINGERPRINT": 15,
	"TEMP_BRIDGE": 25,
	"DEPENDENCIES": 35,
	"CANONICAL_MANUFACTURE": 50,
	"LOGISTICS": 60,
	"VALUATION": 70,
	"VERIFY": 90,
	"ROLLBACK": 95,
	"COMPLETE": 100,
}

# Map internal T-codes → coarse UI phases
_T_PHASE = {
	"T00": "PREPARING",
	"T01": "PREPARING",
	"T02": "LOCKING",
	"T03": "FINGERPRINT",
	"T04": "FINGERPRINT",
	"T05": "TEMP_BRIDGE",
	"T06_T07": "TEMP_BRIDGE",
	"T08": "DEPENDENCIES",
	"T09": "DEPENDENCIES",
	"T10": "DEPENDENCIES",
	"T10_MI": "DEPENDENCIES",
	"T11_T12": "CANONICAL_MANUFACTURE",
	"T13": "LOGISTICS",
	"T14": "LOGISTICS",
	"T15": "LOGISTICS",
	"T16": "LOGISTICS",
	"T17": "VALUATION",
	"T18_T22": "VERIFY",
	"T23": "ROLLBACK",
	"T24": "COMPLETE",
}


def _run_key(run_id: str) -> str:
	return f"jcsr:dry_run:run:{run_id}"


def _lock_key(job_card: str) -> str:
	return f"jcsr:dry_run:lock:{job_card}"


def _active_key(job_card: str) -> str:
	return f"jcsr:dry_run:active:{job_card}"


def _cache_set(key: str, value: Any, ttl: int = STATUS_TTL) -> None:
	# expires keys are not retained in frappe.local.cache after set in some
	# paths; still prefer Redis as source of truth for cross-process polls.
	frappe.cache().set_value(key, value, expires_in_sec=ttl)


def _cache_get(key: str) -> Any:
	# Worker updates must be visible to Desk polls / long-lived processes.
	# Bypass request-local cache so we always read Redis.
	return frappe.cache().get_value(key, expires=True, use_local_cache=False)


def _cache_delete(key: str) -> None:
	try:
		frappe.cache().delete_value(key)
	except Exception:
		pass


def _acquire_lock(job_card: str, run_id: str) -> bool:
	"""Atomic Redis SET NX EX for single-flight."""
	cache = frappe.cache()
	raw_key = cache.make_key(_lock_key(job_card))
	ok = cache.set(name=raw_key, value=run_id.encode(), nx=True, ex=LOCK_TTL)
	return bool(ok)


def _release_lock(job_card: str, run_id: str | None = None) -> None:
	cache = frappe.cache()
	raw_key = cache.make_key(_lock_key(job_card))
	try:
		cur = cache.get(raw_key)
		if run_id is None or (cur and cur.decode() == run_id):
			cache.delete(raw_key)
	except Exception:
		pass
	_cache_delete(_active_key(job_card))


def _lock_holder(job_card: str) -> str | None:
	cache = frappe.cache()
	raw_key = cache.make_key(_lock_key(job_card))
	try:
		cur = cache.get(raw_key)
		return cur.decode() if cur else None
	except Exception:
		return None


def _now_iso() -> str:
	return str(now_datetime())


def _compact_result(result: dict) -> dict:
	"""Keep Redis payload small — no huge ledger dumps."""
	pt = result.get("phase_timings") or {}
	t17 = next((p for p in (pt.get("phases") or []) if p.get("phase") == "T17"), None)
	val = result.get("valuation") or {}
	return {
		"ok": bool(result.get("ok")),
		"status": result.get("status"),
		"error": (result.get("error") or "")[:500],
		"mutated": bool(result.get("mutated")),
		"committed": bool(result.get("committed")),
		"fingerprint": result.get("fingerprint"),
		"canonical_name": result.get("canonical_name"),
		"repair_run_id": result.get("repair_run_id"),
		"blockers": (result.get("blockers") or [])[:20],
		"temporary_bridge": {
			"required": bool((result.get("temporary_bridge") or {}).get("required")),
			"shortages_n": len((result.get("temporary_bridge") or {}).get("shortages") or []),
		},
		"recreated_logistics": [
			{"from": x.get("from"), "to": x.get("to")}
			for x in (result.get("recreated_logistics") or [])[:20]
		],
		"logistics_equivalence": [
			{"original": eq.get("original"), "recreated": eq.get("recreated"), "ok": eq.get("ok")}
			for eq in (result.get("logistics_equivalence") or [])[:10]
		],
		"valuation": {
			"ok": val.get("ok"),
			"count": val.get("count"),
			"skipped_count": val.get("skipped_count"),
			"future_sle_total": val.get("future_sle_total"),
			"elapsed": val.get("elapsed"),
		},
		"verification": {
			"ok": bool((result.get("verification") or {}).get("ok")),
			"errors": ((result.get("verification") or {}).get("errors") or [])[:10],
		},
		"phase_timings": {
			"total_elapsed": pt.get("total_elapsed"),
			"t17": t17,
			"slowest": (pt.get("slowest") or [])[:5],
		},
	}


def _update_run(run_id: str, **fields) -> dict:
	state = _cache_get(_run_key(run_id)) or {}
	state.update(fields)
	state["updated_at"] = _now_iso()
	phase = state.get("phase") or "QUEUED"
	if "progress" not in fields and phase in PHASE_PROGRESS:
		state["progress"] = PHASE_PROGRESS[phase]
	_cache_set(_run_key(run_id), state, STATUS_TTL)
	return state


def _is_stale(state: dict | None) -> bool:
	if not state:
		return True
	if state.get("status") in TERMINAL:
		return True
	updated = state.get("updated_at") or state.get("started_at") or state.get("created_at")
	if not updated:
		return True
	try:
		from frappe.utils import get_datetime

		age = (now_datetime() - get_datetime(updated)).total_seconds()
		return age > POLL_STALE_RUNNING_SEC
	except Exception:
		return True


def get_active_dry_run(job_card: str) -> dict | None:
	"""Return non-stale active run for job card, or None."""
	if not job_card:
		return None
	run_id = _cache_get(_active_key(job_card)) or _lock_holder(job_card)
	if not run_id:
		return None
	if isinstance(run_id, bytes):
		run_id = run_id.decode()
	state = _cache_get(_run_key(run_id))
	if not state:
		_release_lock(job_card, run_id)
		return None
	if state.get("status") in TERMINAL:
		_release_lock(job_card, run_id)
		return None
	if _is_stale(state):
		_update_run(
			run_id,
			status="FAILED",
			phase="COMPLETE",
			progress=100,
			message="Stale run recovered (worker abandoned)",
			finished_at=_now_iso(),
			error="STALE_RUN",
		)
		_release_lock(job_card, run_id)
		return None
	return state


def start_manufacture_repair_dry_run(job_card: str, plan=None) -> dict:
	"""Enqueue ONE Dry Run job; return immediately."""
	if not job_card:
		frappe.throw(frappe._("Job Card is required"))
	if not frappe.db.exists("Job Card", job_card):
		frappe.throw(frappe._("Job Card {0} not found").format(job_card))

	# Reconnect to active run (single-flight)
	active = get_active_dry_run(job_card)
	if active:
		return {
			"ok": True,
			"already_running": True,
			"status": "DRY_RUN_ALREADY_RUNNING",
			"run_id": active.get("run_id"),
			"job_id": active.get("job_id"),
			"job_card": job_card,
			"phase": active.get("phase"),
			"progress": active.get("progress"),
			"message": "A Dry Run is already active for this Job Card",
		}

	if isinstance(plan, str):
		plan = json.loads(plan) if plan else {}
	plan = dict(plan or {})
	# Immutable request snapshot (dispositions + client fingerprint hint)
	snapshot = {
		"dispositions": plan.get("dispositions") or [],
		"merge_documents": plan.get("merge_documents"),
		"merge_material_issues": plan.get("merge_material_issues"),
		"stamp_mode": plan.get("stamp_mode") or "HISTORICAL",
		"fingerprint": plan.get("fingerprint"),
	}
	snapshot_hash = frappe.generate_hash(json.dumps(snapshot, sort_keys=True, default=str), length=12)

	run_id = str(uuid.uuid4())
	if not _acquire_lock(job_card, run_id):
		# Race: another tab won — reconnect
		active = get_active_dry_run(job_card)
		if active:
			return {
				"ok": True,
				"already_running": True,
				"status": "DRY_RUN_ALREADY_RUNNING",
				"run_id": active.get("run_id"),
				"job_id": active.get("job_id"),
				"job_card": job_card,
				"phase": active.get("phase"),
				"progress": active.get("progress"),
				"message": "A Dry Run is already active for this Job Card",
			}
		# Stale lock without state — steal
		_release_lock(job_card)
		if not _acquire_lock(job_card, run_id):
			frappe.throw(frappe._("Could not acquire Dry Run lock for {0}").format(job_card))

	user = frappe.session.user
	created = _now_iso()
	state = {
		"run_id": run_id,
		"job_card": job_card,
		"status": "QUEUED",
		"phase": "QUEUED",
		"progress": 0,
		"message": "Queued",
		"created_at": created,
		"updated_at": created,
		"started_at": None,
		"finished_at": None,
		"requested_by": user,
		"snapshot_hash": snapshot_hash,
		"job_id": None,
		"queue": QUEUE_NAME,
		"result": None,
		"error": None,
		"valuation_roots_done": 0,
		"valuation_roots_total": 0,
		"trace": [{"ts": created, "phase": "QUEUED", "progress": 0}],
	}
	_cache_set(_run_key(run_id), state, STATUS_TTL)
	_cache_set(_active_key(job_card), run_id, LOCK_TTL)

	# Tests / optional inline: run in-process (still ONE connection / ONE txn).
	run_now = bool(frappe.flags.get("jcsr_dry_run_now")) or bool(frappe.conf.get("jcsr_dry_run_inline"))
	job = frappe.enqueue(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_dry_run.execute_queued_dry_run",
		queue=QUEUE_NAME,
		timeout=JOB_TIMEOUT,
		job_name=f"jcsr-dry-run-{job_card}-{run_id[:8]}",
		run_id=run_id,
		job_card=job_card,
		plan_snapshot=snapshot,
		user=user,
		now=run_now,
		enqueue_after_commit=False,
	)
	job_id = getattr(job, "id", None) or ("inline" if run_now else str(job))
	_update_run(run_id, job_id=job_id)
	if run_now:
		# Inline path already finished execute_queued_dry_run
		state = _cache_get(_run_key(run_id)) or {}
		return {
			"ok": True,
			"already_running": False,
			"run_id": run_id,
			"job_id": job_id,
			"status": state.get("status") or "QUEUED",
			"job_card": job_card,
			"created_at": created,
			"queue": QUEUE_NAME,
			"timeout": JOB_TIMEOUT,
			"inline": True,
			"result": state.get("result"),
		}

	frappe.logger("jcsr_queued_dry_run").info(
		f"enqueued dry_run run_id={run_id} job_card={job_card} job_id={job_id} queue={QUEUE_NAME}"
	)
	return {
		"ok": True,
		"already_running": False,
		"run_id": run_id,
		"job_id": job_id,
		"status": "QUEUED",
		"job_card": job_card,
		"created_at": created,
		"queue": QUEUE_NAME,
		"timeout": JOB_TIMEOUT,
	}


def get_manufacture_repair_dry_run_status(run_id: str) -> dict:
	"""Fast poll endpoint."""
	if not run_id:
		frappe.throw(frappe._("run_id is required"))
	state = _cache_get(_run_key(run_id))
	if not state:
		return {"ok": False, "status": "FAILED", "error": "RUN_NOT_FOUND", "run_id": run_id}
	# Permission: System Manager or same requester
	user = frappe.session.user
	if user != "Administrator" and "System Manager" not in (frappe.get_roles() or []):
		if state.get("requested_by") not in (user, "Administrator"):
			frappe.throw(frappe._("Not permitted to view this Dry Run"), frappe.PermissionError)
	out = {
		"ok": True,
		"run_id": state.get("run_id"),
		"job_card": state.get("job_card"),
		"job_id": state.get("job_id"),
		"status": state.get("status"),
		"phase": state.get("phase"),
		"progress": state.get("progress"),
		"message": state.get("message"),
		"created_at": state.get("created_at"),
		"started_at": state.get("started_at"),
		"updated_at": state.get("updated_at"),
		"finished_at": state.get("finished_at"),
		"error": state.get("error"),
		"valuation_roots_done": state.get("valuation_roots_done") or 0,
		"valuation_roots_total": state.get("valuation_roots_total") or 0,
	}
	if state.get("status") in TERMINAL:
		out["result"] = state.get("result")
	return out


def execute_queued_dry_run(
	run_id: str,
	job_card: str,
	plan_snapshot: dict,
	user: str | None = None,
) -> dict:
	"""Worker entry: ONE job, ONE connection, full Dry Run + rollback."""
	if user and frappe.session.user != user:
		frappe.set_user(user)

	started = _now_iso()
	_update_run(
		run_id,
		status="RUNNING",
		phase="PREPARING",
		progress=5,
		message="Preparing",
		started_at=started,
	)

	def progress_cb(phase: str, **extra):
		coarse = _T_PHASE.get(phase, phase)
		prog = PHASE_PROGRESS.get(coarse, None)
		fields = {
			"status": "RUNNING",
			"phase": coarse,
			"message": coarse.replace("_", " ").title(),
		}
		if prog is not None:
			fields["progress"] = prog
		if "valuation_roots_done" in extra:
			fields["valuation_roots_done"] = extra["valuation_roots_done"]
		if "valuation_roots_total" in extra:
			fields["valuation_roots_total"] = extra["valuation_roots_total"]
		state = _update_run(run_id, **fields)
		trace = list(state.get("trace") or [])
		trace.append(
			{
				"ts": _now_iso(),
				"phase": coarse,
				"progress": state.get("progress"),
				"t_code": phase,
				**{k: extra[k] for k in ("valuation_roots_done", "valuation_roots_total") if k in extra},
			}
		)
		# Cap trace length
		_update_run(run_id, trace=trace[-80:])

	try:
		progress_cb("T01")
		result = run_repair(
			job_card,
			plan_input=plan_snapshot,
			dry_run=True,
			progress_cb=progress_cb,
		)
		status_raw = str(result.get("status") or "")
		if status_raw in ("STALE PLAN", "STALE_PLAN") or "STALE" in status_raw.upper():
			final_status = "STALE_PLAN"
		elif status_raw == "BLOCKED" or (result.get("blockers") and not result.get("ok")):
			final_status = "BLOCKED"
		elif result.get("ok") and status_raw == "DRY_RUN_PASS":
			final_status = "PASS"
		else:
			final_status = "FAILED"

		compact = _compact_result(result)
		_update_run(
			run_id,
			status=final_status,
			phase="COMPLETE",
			progress=100,
			message=final_status,
			finished_at=_now_iso(),
			result=compact,
			error=(result.get("error") or None),
			valuation_roots_done=cint((result.get("valuation") or {}).get("count")),
			valuation_roots_total=cint((result.get("valuation") or {}).get("count"))
			+ cint((result.get("valuation") or {}).get("skipped_count")),
		)
		frappe.logger("jcsr_queued_dry_run").info(
			f"finished dry_run run_id={run_id} job_card={job_card} status={final_status} "
			f"total={((result.get('phase_timings') or {}).get('total_elapsed'))}"
		)
		return compact
	except Exception as exc:
		# Always roll back business SQL; do not re-raise — status lives in Redis
		# and re-raise would break inline test orchestration / leave RQ noisy.
		try:
			frappe.db.rollback()
		except Exception:
			pass
		msg = frappe.as_unicode(exc)[:500]
		_update_run(
			run_id,
			status="FAILED",
			phase="COMPLETE",
			progress=100,
			message="FAILED",
			finished_at=_now_iso(),
			error=msg,
			result={
				"ok": False,
				"status": "FAILED",
				"error": msg,
				"mutated": False,
				"committed": False,
			},
		)
		frappe.log_error(title=f"jcsr_queued_dry_run:{run_id}", message=frappe.get_traceback())
		return {
			"ok": False,
			"status": "FAILED",
			"error": msg,
			"mutated": False,
			"committed": False,
		}
	finally:
		_release_lock(job_card, run_id)


def dry_run_in_progress(job_card: str) -> bool:
	return bool(get_active_dry_run(job_card))
