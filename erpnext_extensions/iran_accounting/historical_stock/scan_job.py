# Copyright (c) 2026, ERPNext Extensions contributors
"""Async Historical Repair Scan All jobs (long queue).

Scan All is too heavy for a synchronous HTTP request on production-size
tenants. This module enqueues a read-only job and persists status/result in
cache so the UI can poll without holding a web worker.

Job lifecycle:
  QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED | STALE_JOB | WORKER_UNAVAILABLE
"""

from __future__ import annotations

import json
from time import perf_counter, time

import frappe
from frappe.utils import cint, get_datetime, now_datetime

CACHE_TTL = 60 * 60 * 6  # 6 hours
ACTIVE_STATUSES = ("QUEUED", "RUNNING")
STALE_QUEUED_SECONDS = 120  # QUEUED with no worker progress → STALE_JOB / WORKER_UNAVAILABLE


def _key(job_id: str) -> str:
	return f"hr_scan_all:{job_id}"


def _active_key(company: str) -> str:
	return f"hr_scan_all_active:{company or '_'}"


def _load(job_id: str) -> dict | None:
	raw = frappe.cache().get_value(_key(job_id))
	if not raw:
		return None
	if isinstance(raw, str):
		try:
			return json.loads(raw)
		except Exception:
			return None
	return raw if isinstance(raw, dict) else None


def _save(job_id: str, payload: dict) -> dict:
	payload = dict(payload or {})
	payload["job_id"] = job_id
	payload["updated_at"] = str(now_datetime())
	payload["heartbeat_at"] = payload.get("heartbeat_at") or payload["updated_at"]
	frappe.cache().set_value(_key(job_id), payload, expires_in_sec=CACHE_TTL)
	return payload


def _mark_stale_if_needed(job: dict) -> dict:
	"""If QUEUED too long without a long-queue worker, reclassify."""
	if not job or job.get("status") not in ("QUEUED", "RUNNING"):
		return job
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		worker_queue_status,
	)

	workers = worker_queue_status("long")
	job = dict(job)
	job["worker"] = workers
	if job.get("status") == "QUEUED" and not workers.get("available"):
		# Immediate classification when no long worker exists — do not leave QUEUED 0%.
		job["status"] = "WORKER_UNAVAILABLE"
		job["phase"] = "worker_unavailable"
		job["progress"] = 0
		job["error"] = workers.get("message") or "Background workers are paused."
		_save(job["job_id"], job)
		company = job.get("company")
		active = frappe.cache().get_value(_active_key(company or "_"))
		if str(active or "") == str(job.get("job_id")):
			frappe.cache().delete_value(_active_key(company or "_"))
	elif job.get("status") == "QUEUED" and workers.get("available"):
		queued_at = job.get("queued_at")
		try:
			age = (get_datetime(now_datetime()) - get_datetime(queued_at)).total_seconds()
		except Exception:
			age = 0
		# A live worker draining short/default is not a dead long queue. Only
		# stale when the long queue is idle (no recent dequeue) for 30 minutes.
		busy = (workers.get("last_dequeue_age_seconds") or 10**9) <= 600
		queued_ahead = cint(workers.get("queued_jobs") or 0)
		if age >= 1800 and not busy and queued_ahead == 0:
			job["status"] = "STALE_JOB"
			job["phase"] = "stale_queued"
			job["error"] = f"Job remained QUEUED for {int(age)}s despite workers — check RQ."
			_save(job["job_id"], job)
	return job


def get_scan_all_job(job_id: str) -> dict:
	job = _load(job_id)
	if not job:
		return {"job_id": job_id, "status": "FAILED", "error": "unknown or expired job"}
	job = _mark_stale_if_needed(job)
	out = {k: v for k, v in job.items() if k != "result"}
	if job.get("status") == "COMPLETED":
		out["result"] = job.get("result")
	return out


def get_active_scan_job(company=None) -> dict | None:
	active_id = frappe.cache().get_value(_active_key(company or "_"))
	if not active_id:
		return None
	job = _load(str(active_id))
	if not job:
		return None
	job = _mark_stale_if_needed(job)
	if job.get("status") not in ACTIVE_STATUSES and job.get("status") != "WORKER_UNAVAILABLE":
		return {k: v for k, v in job.items() if k != "result"}
	return {k: v for k, v in job.items() if k != "result"}


def cancel_scan_all_job(job_id: str) -> dict:
	"""Best-effort cancel: mark CANCELLED if still QUEUED; RUNNING jobs finish current phase."""
	job = _load(job_id)
	if not job:
		return {"job_id": job_id, "status": "FAILED", "error": "unknown or expired job"}
	status = job.get("status")
	if status == "QUEUED" or status == "WORKER_UNAVAILABLE":
		job.update(
			{
				"status": "CANCELLED",
				"phase": "cancelled",
				"finished_at": str(now_datetime()),
				"error": "Cancelled by user",
			}
		)
		_save(job_id, job)
		company = job.get("company")
		active = frappe.cache().get_value(_active_key(company or "_"))
		if str(active or "") == str(job_id):
			frappe.cache().delete_value(_active_key(company or "_"))
		return {"job_id": job_id, "status": "CANCELLED"}
	if status == "RUNNING":
		job["cancel_requested"] = True
		_save(job_id, job)
		return {"job_id": job_id, "status": "RUNNING", "message": "Cancel requested; will stop after current phase"}
	return {"job_id": job_id, "status": status, "message": "Job already finished"}


def start_scan_all_job(company=None, *, force: int | bool = 0) -> dict:
	"""Enqueue Scan All on the long queue. Returns immediately.

	If no long-queue worker is available, returns WORKER_UNAVAILABLE without
	leaving a silent QUEUED 0% job.
	"""
	company = company or None
	force = cint(force)
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		worker_queue_status,
	)

	workers = worker_queue_status("long")
	active_id = frappe.cache().get_value(_active_key(company or "_"))
	if active_id and not force:
		existing = _load(str(active_id))
		if existing:
			existing = _mark_stale_if_needed(existing)
			if existing.get("status") in ACTIVE_STATUSES:
				return {
					"job_id": existing.get("job_id") or active_id,
					"status": existing.get("status"),
					"phase": existing.get("phase"),
					"progress": existing.get("progress"),
					"deduplicated": True,
					"company": company,
					"worker": workers,
					"message": "Scan All already in progress for this company",
				}

	if not workers.get("available"):
		job_id = f"HRS-{frappe.generate_hash(length=10)}"
		payload = {
			"job_id": job_id,
			"status": "WORKER_UNAVAILABLE",
			"phase": "worker_unavailable",
			"progress": 0,
			"company": company,
			"user": frappe.session.user,
			"queued_at": str(now_datetime()),
			"started_at": None,
			"finished_at": str(now_datetime()),
			"elapsed_s": 0,
			"error": workers.get("message"),
			"result": None,
			"timing": {},
			"worker": workers,
			"message": workers.get("message"),
		}
		_save(job_id, payload)
		return {
			"job_id": job_id,
			"status": "WORKER_UNAVAILABLE",
			"deduplicated": False,
			"company": company,
			"queue": "long",
			"worker": workers,
			"message": workers.get("message"),
			"enqueued": False,
		}

	job_id = f"HRS-{frappe.generate_hash(length=10)}"
	payload = {
		"job_id": job_id,
		"status": "QUEUED",
		"phase": "queued",
		"progress": 0,
		"company": company,
		"user": frappe.session.user,
		"queued_at": str(now_datetime()),
		"started_at": None,
		"finished_at": None,
		"elapsed_s": None,
		"error": None,
		"result": None,
		"timing": {},
		"worker": workers,
		"processed": 0,
		"total": None,
		"current_scanner": None,
	}
	_save(job_id, payload)
	frappe.cache().set_value(_active_key(company or "_"), job_id, expires_in_sec=CACHE_TTL)

	frappe.enqueue(
		"erpnext_extensions.iran_accounting.historical_stock.scan_job.run_scan_all_job",
		queue="long",
		job_id=job_id,
		timeout=1800,
		job_name=f"Historical Repair Scan All {job_id}",
		company=company,
		scan_job_id=job_id,
		user=frappe.session.user,
	)
	return {
		"job_id": job_id,
		"status": "QUEUED",
		"deduplicated": False,
		"company": company,
		"queue": "long",
		"worker": workers,
		"enqueued": True,
		"message": "Scan All queued on long worker",
	}


def _heartbeat(job_id: str, job: dict, *, phase: str, progress: int, scanner: str | None = None) -> dict:
	job.update(
		{
			"status": "RUNNING",
			"phase": phase,
			"progress": int(progress),
			"current_scanner": scanner or phase,
			"heartbeat_at": str(now_datetime()),
		}
	)
	if job.get("cancel_requested"):
		job.update(
			{
				"status": "CANCELLED",
				"phase": "cancelled",
				"finished_at": str(now_datetime()),
				"error": "Cancelled by user during scan",
			}
		)
		_save(job_id, job)
		raise frappe.ValidationError("Scan All cancelled")
	return _save(job_id, job)


def run_scan_all_job(company=None, scan_job_id=None, user=None, **_kwargs):
	"""Background worker entrypoint — read-only Scan All."""
	job_id = scan_job_id
	if not job_id:
		return
	job = _load(job_id) or {"job_id": job_id, "company": company, "user": user}
	try:
		from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
			mark_queue_consumed,
		)

		mark_queue_consumed("long")
	except Exception:
		pass
	job.update(
		{
			"status": "RUNNING",
			"phase": "starting",
			"progress": 2,
			"started_at": str(now_datetime()),
			"error": None,
			"heartbeat_at": str(now_datetime()),
		}
	)
	_save(job_id, job)
	t0 = perf_counter()
	try:
		from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
		from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
			save_metrics_snapshot,
		)

		_heartbeat(job_id, job, phase="posting_order", progress=10, scanner="Posting Order")
		# Full scan still one call; progress phases bookend it until scan.py grows hooks.
		_heartbeat(job_id, job, phase="full_integrity_scan", progress=25, scanner="Full Integrity Scan")
		result = run_full_integrity_scan(company=company, include_manufacture=False)
		elapsed = round(perf_counter() - t0, 3)
		timing = (result or {}).get("timing") or {}
		dashboard = (result or {}).get("dashboard") or {}
		# Persist scan result before snapshot so a DocType validation failure
		# cannot discard a completed integrity scan.
		job.update(
			{
				"status": "RUNNING",
				"phase": "root_graph_metrics",
				"progress": 85,
				"elapsed_s": elapsed,
				"result": result,
				"timing": timing,
				"current_scanner": "Metrics Snapshot",
				"heartbeat_at": str(now_datetime()),
			}
		)
		_save(job_id, job)
		snapshot_error = None
		try:
			save_metrics_snapshot(
				company=company,
				dashboard=dashboard,
				timing=timing,
				source="scan_all",
				job_id=job_id,
				extra={"elapsed_s": elapsed},
			)
		except Exception as snap_exc:
			snapshot_error = str(snap_exc)[:500]
			frappe.log_error(
				title=f"Historical Repair Metrics Snapshot failed ({job_id})",
				message=frappe.get_traceback(),
			)
		job.update(
			{
				"status": "COMPLETED",
				"phase": "completed",
				"progress": 100,
				"finished_at": str(now_datetime()),
				"elapsed_s": elapsed,
				"result": result,
				"timing": timing,
				"error": None,
				"snapshot_error": snapshot_error,
				"current_scanner": None,
			}
		)
		_save(job_id, job)
	except Exception as exc:
		status = "CANCELLED" if "cancelled" in str(exc).lower() else "FAILED"
		job.update(
			{
				"status": status,
				"phase": status.lower(),
				"progress": 100 if status == "FAILED" else job.get("progress") or 0,
				"finished_at": str(now_datetime()),
				"elapsed_s": round(perf_counter() - t0, 3),
				"error": str(exc)[:2000],
			}
		)
		_save(job_id, job)
		if status == "FAILED":
			frappe.log_error(
				title=f"Historical Repair Scan All failed ({job_id})",
				message=frappe.get_traceback(),
			)
	finally:
		active = frappe.cache().get_value(_active_key(company or "_"))
		if str(active or "") == str(job_id):
			frappe.cache().delete_value(_active_key(company or "_"))
	return {"job_id": job_id, "status": job.get("status")}


def ping_long_queue(flag: str | None = None):
	"""Harmless worker consume probe. Never used as a Scan All substitute."""
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		mark_queue_consumed,
	)

	mark_queue_consumed("long")
	if flag:
		frappe.cache().set_value(str(flag), "done", expires_in_sec=120)
	return {"ok": True, "flag": flag}
