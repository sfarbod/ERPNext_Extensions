# Copyright (c) 2026, ERPNext Extensions contributors
"""Queued Manufacture Repair orchestration (v5.5.4).

Shared Dry Run + Apply infrastructure:

- ONE RQ job on ``long`` queue
- ONE Frappe DB connection / business transaction
- Redis progress/status outside the business transaction
- Shared single-flight lock per Job Card (Dry Run ↔ Apply)
- Apply commits exactly once after verification; Dry Run always rolls back
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
# Staging Dry Run valuation has been observed >6 minutes for PO-JOB08760.
# 600s left insufficient margin for a full Apply; use 900s worker timeout.
JOB_TIMEOUT = 900
STATUS_TTL = 24 * 60 * 60
# Lock TTL must exceed worker timeout + margin (900s + 300s).
LOCK_TTL = 25 * 60
# Stale running detection — must not expire during a valid long Apply.
POLL_STALE_RUNNING_SEC = 20 * 60

MODE_DRY_RUN = "DRY_RUN"
MODE_APPLY = "APPLY"

TERMINAL = frozenset({"PASS", "FAILED", "STALE_PLAN", "BLOCKED", "CANCELLED", "COMMITTED"})

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
	"COMMIT": 95,
	"ROLLBACK": 95,
	"COMPLETE": 100,
}

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
	"COMMIT": "COMMIT",
}


def _run_key(run_id: str) -> str:
	return f"jcsr:repair:run:{run_id}"


def _lock_key(job_card: str) -> str:
	return f"jcsr:repair:lock:{job_card}"


def _active_key(job_card: str) -> str:
	return f"jcsr:repair:active:{job_card}"


def _committed_key(job_card: str) -> str:
	"""Authoritative post-commit evidence (survives Redis status-update failure)."""
	return f"jcsr:repair:committed:{job_card}"


def _cache_set(key: str, value: Any, ttl: int = STATUS_TTL) -> None:
	frappe.cache().set_value(key, value, expires_in_sec=ttl)


def _cache_get(key: str) -> Any:
	return frappe.cache().get_value(key, expires=True, use_local_cache=False)


def _cache_delete(key: str) -> None:
	try:
		frappe.cache().delete_value(key)
	except Exception:
		pass


def _acquire_lock(job_card: str, run_id: str) -> bool:
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
		"commit_count": result.get("commit_count"),
		"commits_before_verify": result.get("commits_before_verify"),
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


def _persist_committed_evidence(job_card: str, run_id: str, compact: dict) -> None:
	"""DB-authoritative recovery marker — written AFTER successful commit."""
	_cache_set(
		_committed_key(job_card),
		{
			"run_id": run_id,
			"job_card": job_card,
			"mode": MODE_APPLY,
			"status": "COMMITTED",
			"committed": True,
			"mutated": True,
			"canonical_name": compact.get("canonical_name"),
			"fingerprint": compact.get("fingerprint"),
			"finished_at": _now_iso(),
			"result": compact,
		},
		STATUS_TTL,
	)


def get_committed_evidence(job_card: str) -> dict | None:
	ev = _cache_get(_committed_key(job_card))
	return ev if isinstance(ev, dict) else None


def _db_apply_committed_state(job_card: str) -> dict | None:
	"""Fallback recovery when Redis status is missing/failed after commit."""
	try:
		active = frappe.db.sql(
			"""
			select name from `tabStock Entry`
			where job_card=%s and purpose='Manufacture' and docstatus=1
			order by creation desc limit 1
			""",
			job_card,
		)
		if not active:
			return None
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
			scan_manufacture_reconciliation,
		)

		scan = scan_manufacture_reconciliation(job_card)
		blockers = (scan.get("plan") or {}).get("blockers") or []
		if "Nothing to repair" not in blockers and (scan.get("plan") or {}).get("apply_allowed"):
			return None
		return {
			"ok": True,
			"status": "COMMITTED",
			"committed": True,
			"mutated": True,
			"canonical_name": active[0][0],
			"fingerprint": (scan.get("plan") or {}).get("fingerprint"),
			"recovered_from_db": True,
			"message": "Recovered committed Apply from authoritative DB state",
		}
	except Exception:
		return None


def get_active_repair(job_card: str) -> dict | None:
	"""Return non-stale active Dry Run or Apply for job card, or None."""
	if not job_card:
		return None
	run_id = _cache_get(_active_key(job_card)) or _lock_holder(job_card)
	if not run_id:
		return None
	if isinstance(run_id, bytes):
		run_id = run_id.decode()
	state = _cache_get(_run_key(run_id))
	if not state:
		# Post-commit Redis failure: lock may linger without run state
		ev = get_committed_evidence(job_card)
		if ev and ev.get("run_id") == run_id:
			_release_lock(job_card, run_id)
			return None
		_release_lock(job_card, run_id)
		return None
	if state.get("status") in TERMINAL:
		_release_lock(job_card, run_id)
		return None
	if _is_stale(state):
		# Before marking FAILED, check if Apply actually committed
		if state.get("mode") == MODE_APPLY:
			ev = get_committed_evidence(job_card)
			if ev and ev.get("run_id") == run_id:
				_update_run(
					run_id,
					status="COMMITTED",
					phase="COMPLETE",
					progress=100,
					message="APPLY PASS — COMMITTED (recovered)",
					finished_at=_now_iso(),
					result=ev.get("result") or ev,
					committed=True,
					mutated=True,
				)
				_release_lock(job_card, run_id)
				return None
			db_rec = _db_apply_committed_state(job_card)
			if db_rec:
				_update_run(
					run_id,
					status="COMMITTED",
					phase="COMPLETE",
					progress=100,
					message=db_rec.get("message"),
					finished_at=_now_iso(),
					result=db_rec,
					committed=True,
					mutated=True,
				)
				_persist_committed_evidence(job_card, run_id, db_rec)
				_release_lock(job_card, run_id)
				return None
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


def repair_in_progress(job_card: str) -> bool:
	return bool(get_active_repair(job_card))


def _normalize_plan(plan) -> dict:
	if isinstance(plan, str):
		plan = json.loads(plan) if plan else {}
	plan = dict(plan or {})
	out = {
		"dispositions": plan.get("dispositions") or [],
		"merge_documents": plan.get("merge_documents"),
		"merge_material_issues": plan.get("merge_material_issues"),
		"stamp_mode": plan.get("stamp_mode") or "HISTORICAL",
		"fingerprint": plan.get("fingerprint"),
	}
	# Preserve operator Batch Offset approvals exactly — do not invent or default them.
	if "batch_offset_approvals" in plan:
		approvals = plan.get("batch_offset_approvals")
		if isinstance(approvals, list):
			out["batch_offset_approvals"] = [dict(a) if isinstance(a, dict) else a for a in approvals]
		else:
			out["batch_offset_approvals"] = approvals
	# Preserve Partial Batch Offset pair approvals (v5.5.21).
	if "partial_batch_offset_approvals" in plan:
		partials = plan.get("partial_batch_offset_approvals")
		if isinstance(partials, list):
			out["partial_batch_offset_approvals"] = [
				dict(a) if isinstance(a, dict) else a for a in partials
			]
		else:
			out["partial_batch_offset_approvals"] = partials
	# Preserve Manufacture Batch Replacement approvals (v5.5.22).
	if "manufacture_batch_replace_approvals" in plan:
		replaces = plan.get("manufacture_batch_replace_approvals")
		if isinstance(replaces, list):
			out["manufacture_batch_replace_approvals"] = [
				dict(a) if isinstance(a, dict) else a for a in replaces
			]
		else:
			out["manufacture_batch_replace_approvals"] = replaces
	return out


def _already_running_payload(job_card: str, active: dict) -> dict:
	mode = active.get("mode") or MODE_DRY_RUN
	return {
		"ok": True,
		"already_running": True,
		"status": f"{mode}_ALREADY_RUNNING",
		"mode": mode,
		"run_id": active.get("run_id"),
		"job_id": active.get("job_id"),
		"job_card": job_card,
		"phase": active.get("phase"),
		"progress": active.get("progress"),
		"message": f"A {mode.replace('_', ' ').title()} is already active for this Job Card",
	}


def _start_repair(job_card: str, plan, mode: str, confirm: int | bool = 0) -> dict:
	if not job_card:
		frappe.throw(frappe._("Job Card is required"))
	if not frappe.db.exists("Job Card", job_card):
		frappe.throw(frappe._("Job Card {0} not found").format(job_card))
	if mode == MODE_APPLY and not cint(confirm):
		frappe.throw(frappe._("Confirmation required before Apply."))

	active = get_active_repair(job_card)
	if active:
		return _already_running_payload(job_card, active)

	# Post-commit recovery: refuse blind re-Apply if evidence says already committed
	# for the same fingerprint and DB is already repaired.
	if mode == MODE_APPLY:
		ev = get_committed_evidence(job_card)
		snap = _normalize_plan(plan)
		if ev and ev.get("fingerprint") and snap.get("fingerprint") == ev.get("fingerprint"):
			db_rec = _db_apply_committed_state(job_card)
			if db_rec:
				return {
					"ok": True,
					"already_committed": True,
					"status": "COMMITTED",
					"mode": MODE_APPLY,
					"run_id": ev.get("run_id"),
					"job_card": job_card,
					"message": "Apply already committed — rescan; do not re-run",
					"result": ev.get("result") or db_rec,
				}

	snapshot = _normalize_plan(plan)
	snapshot_hash = frappe.generate_hash(
		json.dumps(snapshot, sort_keys=True, default=str), length=12
	)
	run_id = str(uuid.uuid4())
	if not _acquire_lock(job_card, run_id):
		active = get_active_repair(job_card)
		if active:
			return _already_running_payload(job_card, active)
		_release_lock(job_card)
		if not _acquire_lock(job_card, run_id):
			frappe.throw(frappe._("Could not acquire repair lock for {0}").format(job_card))

	user = frappe.session.user
	created = _now_iso()
	state = {
		"run_id": run_id,
		"job_card": job_card,
		"mode": mode,
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
		"trace": [{"ts": created, "phase": "QUEUED", "progress": 0, "mode": mode}],
	}
	_cache_set(_run_key(run_id), state, STATUS_TTL)
	_cache_set(_active_key(job_card), run_id, LOCK_TTL)

	inline_flag = (
		"jcsr_dry_run_now" if mode == MODE_DRY_RUN else "jcsr_apply_now"
	)
	conf_flag = (
		"jcsr_dry_run_inline" if mode == MODE_DRY_RUN else "jcsr_apply_inline"
	)
	run_now = bool(frappe.flags.get(inline_flag)) or bool(frappe.conf.get(conf_flag))
	# Also honor legacy dry-run inline for apply tests when jcsr_repair_now set
	if bool(frappe.flags.get("jcsr_repair_now")):
		run_now = True

	method = (
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair.execute_queued_dry_run"
		if mode == MODE_DRY_RUN
		else "erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair.execute_queued_apply"
	)
	job = frappe.enqueue(
		method,
		queue=QUEUE_NAME,
		timeout=JOB_TIMEOUT,
		job_name=f"jcsr-{mode.lower()}-{job_card}-{run_id[:8]}",
		run_id=run_id,
		job_card=job_card,
		plan_snapshot=snapshot,
		user=user,
		now=run_now,
		enqueue_after_commit=False,
		confirm=1 if mode == MODE_APPLY else 0,
	)
	job_id = getattr(job, "id", None) or ("inline" if run_now else str(job))
	_update_run(run_id, job_id=job_id)
	if run_now:
		state = _cache_get(_run_key(run_id)) or {}
		return {
			"ok": True,
			"already_running": False,
			"run_id": run_id,
			"job_id": job_id,
			"status": state.get("status") or "QUEUED",
			"mode": mode,
			"job_card": job_card,
			"created_at": created,
			"queue": QUEUE_NAME,
			"timeout": JOB_TIMEOUT,
			"inline": True,
			"result": state.get("result"),
		}

	frappe.logger("jcsr_queued_repair").info(
		f"enqueued {mode} run_id={run_id} job_card={job_card} job_id={job_id} queue={QUEUE_NAME}"
	)
	return {
		"ok": True,
		"already_running": False,
		"run_id": run_id,
		"job_id": job_id,
		"status": "QUEUED",
		"mode": mode,
		"job_card": job_card,
		"created_at": created,
		"queue": QUEUE_NAME,
		"timeout": JOB_TIMEOUT,
	}


def start_manufacture_repair_dry_run(job_card: str, plan=None) -> dict:
	return _start_repair(job_card, plan, MODE_DRY_RUN)


def start_manufacture_repair_apply(job_card: str, plan=None, confirm: int | bool = 0) -> dict:
	return _start_repair(job_card, plan, MODE_APPLY, confirm=confirm)


def get_manufacture_repair_status(run_id: str) -> dict:
	if not run_id:
		frappe.throw(frappe._("run_id is required"))
	state = _cache_get(_run_key(run_id))
	if not state:
		# Try committed evidence by scanning — run_id may match
		return {"ok": False, "status": "FAILED", "error": "RUN_NOT_FOUND", "run_id": run_id}
	user = frappe.session.user
	if user != "Administrator" and "System Manager" not in (frappe.get_roles() or []):
		if state.get("requested_by") not in (user, "Administrator"):
			frappe.throw(frappe._("Not permitted to view this repair run"), frappe.PermissionError)
	out = {
		"ok": True,
		"run_id": state.get("run_id"),
		"job_card": state.get("job_card"),
		"job_id": state.get("job_id"),
		"mode": state.get("mode"),
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


def get_manufacture_repair_dry_run_status(run_id: str) -> dict:
	return get_manufacture_repair_status(run_id)


def get_manufacture_repair_apply_status(run_id: str) -> dict:
	return get_manufacture_repair_status(run_id)


def get_active_dry_run(job_card: str) -> dict | None:
	active = get_active_repair(job_card)
	if active and active.get("mode") == MODE_DRY_RUN:
		return active
	return None


def get_active_apply(job_card: str) -> dict | None:
	active = get_active_repair(job_card)
	if active and active.get("mode") == MODE_APPLY:
		return active
	return None


def dry_run_in_progress(job_card: str) -> bool:
	return repair_in_progress(job_card)


def apply_in_progress(job_card: str) -> bool:
	active = get_active_repair(job_card)
	return bool(active and active.get("mode") == MODE_APPLY)


def _make_progress_cb(run_id: str):
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
				**{
					k: extra[k]
					for k in ("valuation_roots_done", "valuation_roots_total")
					if k in extra
				},
			}
		)
		_update_run(run_id, trace=trace[-80:])

	return progress_cb


def _map_final_status(result: dict, mode: str) -> str:
	status_raw = str(result.get("status") or "")
	if status_raw in ("STALE PLAN", "STALE_PLAN") or "STALE" in status_raw.upper():
		return "STALE_PLAN"
	if status_raw == "BLOCKED" or (result.get("blockers") and not result.get("ok")):
		return "BLOCKED"
	if mode == MODE_DRY_RUN and result.get("ok") and status_raw == "DRY_RUN_PASS":
		return "PASS"
	if mode == MODE_APPLY and result.get("ok") and result.get("committed") and status_raw == "APPLY_PASS":
		return "COMMITTED"
	if mode == MODE_APPLY and result.get("ok") and status_raw == "APPLY_PASS":
		# Should not happen — Apply PASS without commit
		return "FAILED"
	return "FAILED"


def _instrument_commits(result: dict):
	"""Count frappe.db.commit calls during engine execution."""
	commits_before_verify = {"n": 0}
	commit_total = {"n": 0}
	orig = frappe.db.commit
	verify_started = {"v": False}

	def _wrap():
		commit_total["n"] += 1
		if not verify_started["v"]:
			commits_before_verify["n"] += 1
		return orig()

	return commits_before_verify, commit_total, _wrap, verify_started, orig


def execute_queued_dry_run(
	run_id: str,
	job_card: str,
	plan_snapshot: dict,
	user: str | None = None,
	confirm: int | bool = 0,
) -> dict:
	"""Worker: ONE job, ONE connection, full Dry Run + rollback."""
	_ = confirm
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
		mode=MODE_DRY_RUN,
	)
	progress_cb = _make_progress_cb(run_id)
	try:
		progress_cb("T01")
		result = run_repair(
			job_card,
			plan_input=plan_snapshot,
			dry_run=True,
			progress_cb=progress_cb,
		)
		final_status = _map_final_status(result, MODE_DRY_RUN)
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
		frappe.logger("jcsr_queued_repair").info(
			f"finished dry_run run_id={run_id} job_card={job_card} status={final_status}"
		)
		return compact
	except Exception as exc:
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


def execute_queued_apply(
	run_id: str,
	job_card: str,
	plan_snapshot: dict,
	user: str | None = None,
	confirm: int | bool = 1,
) -> dict:
	"""Worker: ONE job, ONE connection, full Apply + ONE final commit."""
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
		mode=MODE_APPLY,
	)
	progress_cb = _make_progress_cb(run_id)

	commits_before_verify, commit_total, commit_wrap, verify_started, orig_commit = (
		_instrument_commits({})
	)
	# Only instrument during the engine call; restore immediately after so
	# failure-audit commits are not miscounted as pre-verify business commits.
	frappe.db.commit = commit_wrap  # type: ignore[method-assign]

	committed_business = False
	compact = None
	try:
		progress_cb("T01")

		inner_cb = progress_cb

		def progress_cb_tracked(phase: str, **extra):
			if phase in ("T18_T22",) or _T_PHASE.get(phase) == "VERIFY":
				verify_started["v"] = True
			inner_cb(phase, **extra)

		try:
			result = run_repair(
				job_card,
				plan_input=plan_snapshot,
				dry_run=False,
				confirm=confirm,
				progress_cb=progress_cb_tracked,
			)
		finally:
			frappe.db.commit = orig_commit  # type: ignore[method-assign]
		result["commits_before_verify"] = commits_before_verify["n"]
		result["commit_count"] = commit_total["n"]
		committed_business = bool(result.get("committed"))
		compact = _compact_result(result)

		if committed_business:
			# Persist evidence BEFORE Redis status (DB is authoritative)
			_persist_committed_evidence(job_card, run_id, compact)
			progress_cb("COMMIT")
			# QA10: simulate Redis status update failure after commit
			if getattr(frappe.flags, "jcsr_fail_redis_after_commit", None):
				raise RuntimeError("INJECTED_FAILURE:redis_status_after_commit")

		final_status = _map_final_status(result, MODE_APPLY)
		msg = (
			"APPLY PASS — COMMITTED"
			if final_status == "COMMITTED"
			else final_status
		)
		_update_run(
			run_id,
			status=final_status,
			phase="COMPLETE",
			progress=100,
			message=msg,
			finished_at=_now_iso(),
			result=compact,
			error=(result.get("error") or None),
			valuation_roots_done=cint((result.get("valuation") or {}).get("count")),
			valuation_roots_total=cint((result.get("valuation") or {}).get("count"))
			+ cint((result.get("valuation") or {}).get("skipped_count")),
		)
		frappe.logger("jcsr_queued_repair").info(
			f"finished apply run_id={run_id} job_card={job_card} status={final_status} "
			f"committed={committed_business} commits={commit_total['n']}"
		)
		return compact
	except Exception as exc:
		msg = frappe.as_unicode(exc)[:500]
		# If business already committed, NEVER claim rollback
		if committed_business or (
			compact and compact.get("committed")
		) or get_committed_evidence(job_card):
			ev = get_committed_evidence(job_card) or {}
			recovered = ev.get("result") or compact or {
				"ok": True,
				"status": "COMMITTED",
				"committed": True,
				"mutated": True,
				"error": f"Post-commit status failure: {msg}",
			}
			try:
				_update_run(
					run_id,
					status="COMMITTED",
					phase="COMPLETE",
					progress=100,
					message="APPLY PASS — COMMITTED (post-commit status recovered)",
					finished_at=_now_iso(),
					result=recovered,
					error=f"POST_COMMIT_STATUS_FAILURE:{msg}",
				)
			except Exception:
				pass
			return recovered

		try:
			frappe.db.rollback()
		except Exception:
			pass
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
		frappe.log_error(title=f"jcsr_queued_apply:{run_id}", message=frappe.get_traceback())
		return {
			"ok": False,
			"status": "FAILED",
			"error": msg,
			"mutated": False,
			"committed": False,
		}
	finally:
		try:
			frappe.db.commit = orig_commit  # type: ignore[method-assign]
		except Exception:
			pass
		_release_lock(job_card, run_id)
