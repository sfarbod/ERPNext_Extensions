# Copyright (c) 2026, ERPNext Extensions contributors
"""Deterministic WORKER_PREFLIGHT for Historical Repair mutating phases.

Fail-closed: mutating campaign phases that create/depend on RIV or long-queue
work MUST NOT proceed when required workers are unavailable.

Does not mutate RIV lifecycle. Does not start workers itself.
"""

from __future__ import annotations

from time import perf_counter, sleep

import frappe

from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
	mark_queue_consumed,
	worker_queue_status,
)

WORKER_PREFLIGHT_READY = "WORKER_PREFLIGHT_READY"
WORKER_PREFLIGHT_BLOCKED = "WORKER_PREFLIGHT_BLOCKED"

# Harmless probe job identity (must not touch stock / GL).
_PROBE_METHOD = "erpnext_extensions.iran_accounting.historical_stock.worker_preflight._worker_probe_job"


def _worker_probe_job(queue_name: str = "long") -> str:
	"""RQ target: prove a listener dequeued and executed work on ``queue_name``."""
	mark_queue_consumed(queue_name)
	return f"PROBE_OK:{queue_name}"


def _redis_reachable() -> dict:
	out = {"ok": False, "error": None}
	try:
		from frappe.utils.background_jobs import get_redis_conn

		conn = get_redis_conn()
		pong = conn.ping()
		out["ok"] = bool(pong)
		if not pong:
			out["error"] = "redis_ping_false"
	except Exception as exc:  # noqa: BLE001
		out["error"] = str(exc)[:300]
	return out


def _scheduler_state() -> dict:
	"""Scheduler may be paused during controlled rehearsal; record, do not require on."""
	paused = None
	enabled = None
	try:
		paused = cint_safe(frappe.db.get_single_value("System Settings", "pause_scheduler"))
	except Exception:
		try:
			paused = cint_safe(frappe.conf.get("pause_scheduler"))
		except Exception:
			paused = None
	try:
		enabled = cint_safe(frappe.conf.get("enable_scheduler"))
	except Exception:
		enabled = None
	return {"pause_scheduler": paused, "enable_scheduler": enabled}


def cint_safe(v) -> int | None:
	try:
		from frappe.utils import cint

		return int(cint(v))
	except Exception:
		return None


def _run_probe(queue: str, *, timeout_s: float = 45.0) -> dict:
	"""Enqueue a no-op on ``queue`` and wait for RQ completion."""
	from frappe.utils.background_jobs import get_redis_conn
	from rq.job import Job

	t0 = perf_counter()
	try:
		job = frappe.enqueue(
			_PROBE_METHOD,
			queue=queue,
			timeout=60,
			enqueue_after_commit=False,
			queue_name=queue,
		)
	except Exception as exc:  # noqa: BLE001
		return {
			"ok": False,
			"reason": "probe_enqueue_failed",
			"error": str(exc)[:400],
			"elapsed": round(perf_counter() - t0, 3),
		}

	job_id = getattr(job, "id", None) or getattr(job, "name", None)
	if not job_id:
		# frappe.enqueue may return None when queue overloaded
		return {
			"ok": False,
			"reason": "probe_enqueue_returned_none",
			"hint": "QueueOverloaded or workers unavailable at enqueue",
			"elapsed": round(perf_counter() - t0, 3),
		}

	conn = get_redis_conn()
	deadline = t0 + timeout_s
	last_status = None
	while perf_counter() < deadline:
		try:
			rj = Job.fetch(job_id, connection=conn)
			last_status = rj.get_status(refresh=True)
			if last_status == "finished":
				mark_queue_consumed(queue)
				return {
					"ok": True,
					"job_id": job_id,
					"status": last_status,
					"result": rj.result,
					"elapsed": round(perf_counter() - t0, 3),
				}
			if last_status in ("failed", "stopped", "canceled", "cancelled"):
				return {
					"ok": False,
					"reason": "probe_job_failed",
					"job_id": job_id,
					"status": last_status,
					"exc_info": (rj.exc_info or "")[:400],
					"elapsed": round(perf_counter() - t0, 3),
				}
		except Exception as exc:  # noqa: BLE001
			last_status = f"fetch_error:{exc}"
		sleep(0.5)

	return {
		"ok": False,
		"reason": "probe_timeout",
		"job_id": job_id,
		"last_status": last_status,
		"elapsed": round(perf_counter() - t0, 3),
	}


def run_worker_preflight(
	*,
	queues: tuple[str, ...] | list[str] = ("long",),
	require_probe: bool = True,
	probe_timeout_s: float = 45.0,
) -> dict:
	"""Verify Redis + live listeners for required queues (+ optional probe).

	Returns ``status`` = WORKER_PREFLIGHT_READY | WORKER_PREFLIGHT_BLOCKED.
	"""
	t0 = perf_counter()
	reasons: list[dict] = []
	redis = _redis_reachable()
	if not redis.get("ok"):
		reasons.append({"code": "REDIS_UNREACHABLE", "detail": redis})

	queue_reports = {}
	for q in queues:
		rep = worker_queue_status(q)
		queue_reports[q] = rep
		if not rep.get("available") or int(rep.get("workers_for_queue") or 0) <= 0:
			# available via recent_dequeue alone is insufficient for mutating phases
			reasons.append(
				{
					"code": "WORKER_UNAVAILABLE",
					"queue": q,
					"detail": rep,
					"message": rep.get("message")
					or "Background workers are paused or not listening to the required queue.",
				}
			)

	probes = {}
	if require_probe and not reasons:
		for q in queues:
			probes[q] = _run_probe(q, timeout_s=probe_timeout_s)
			if not probes[q].get("ok"):
				reasons.append({"code": "PROBE_FAILED", "queue": q, "detail": probes[q]})

	status = WORKER_PREFLIGHT_READY if not reasons else WORKER_PREFLIGHT_BLOCKED
	return {
		"status": status,
		"ready": status == WORKER_PREFLIGHT_READY,
		"redis": redis,
		"scheduler": _scheduler_state(),
		"queues": queue_reports,
		"probes": probes,
		"blockers": reasons,
		"elapsed": round(perf_counter() - t0, 3),
	}


def require_worker_preflight(
	*,
	queues: tuple[str, ...] | list[str] = ("long",),
	require_probe: bool = True,
	phase: str = "campaign",
) -> dict:
	"""Fail-closed gate. Returns READY result or raises via structured BLOCKED dict.

	Callers should stop when ``ready`` is False — do not enqueue more RIV work.
	"""
	result = run_worker_preflight(queues=queues, require_probe=require_probe)
	result["phase"] = phase
	if not result.get("ready"):
		result["gate"] = "WORKER_PREFLIGHT_BLOCKED"
	return result


def report_queue_status(
	*,
	queues: tuple[str, ...] | list[str] = ("short", "default", "long"),
	phase: str = "campaign",
) -> dict:
	"""Non-blocking queue/Redis/scheduler report for sync/foreground phases.

	Does NOT fail closed on Workers=0. Use ``require_worker_preflight`` when the
	operation actually enqueues async work.
	"""
	t0 = perf_counter()
	redis = _redis_reachable()
	queue_reports = {q: worker_queue_status(q) for q in queues}
	return {
		"status": "WORKER_STATUS_REPORT",
		"ready": bool(redis.get("ok")),
		"mode": "FOREGROUND_SYNC_REPORT_ONLY",
		"redis": redis,
		"scheduler": _scheduler_state(),
		"queues": queue_reports,
		"blockers": (
			[{"code": "REDIS_UNREACHABLE", "detail": redis}] if not redis.get("ok") else []
		),
		"phase": phase,
		"elapsed": round(perf_counter() - t0, 3),
		"note": (
			"Foreground sync RIV does not require long/default workers. "
			"Workers=0 is reported but not blocking for sync execution."
		),
	}