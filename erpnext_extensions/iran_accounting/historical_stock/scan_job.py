# Copyright (c) 2026, ERPNext Extensions contributors
"""Async Historical Repair Scan All jobs (long queue).

Scan All is too heavy for a synchronous HTTP request on production-size
tenants. This module enqueues a read-only job and persists status/result in
cache so the UI can poll without holding a web worker.
"""

from __future__ import annotations

import json
from time import perf_counter, time

import frappe
from frappe.utils import cint, now_datetime

CACHE_TTL = 60 * 60 * 6  # 6 hours
ACTIVE_STATUSES = ("QUEUED", "RUNNING")


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
	frappe.cache().set_value(_key(job_id), payload, expires_in_sec=CACHE_TTL)
	return payload


def get_scan_all_job(job_id: str) -> dict:
	job = _load(job_id)
	if not job:
		return {"job_id": job_id, "status": "FAILED", "error": "unknown or expired job"}
	# Strip heavy result unless completed (status poll stays light)
	out = {k: v for k, v in job.items() if k != "result"}
	if job.get("status") == "COMPLETED":
		out["result"] = job.get("result")
	return out


def start_scan_all_job(company=None, *, force: int | bool = 0) -> dict:
	"""Enqueue Scan All on the long queue. Returns immediately."""
	company = company or None
	force = cint(force)
	active_id = frappe.cache().get_value(_active_key(company or "_"))
	if active_id and not force:
		existing = _load(str(active_id))
		if existing and existing.get("status") in ACTIVE_STATUSES:
			return {
				"job_id": existing.get("job_id") or active_id,
				"status": existing.get("status"),
				"deduplicated": True,
				"company": company,
				"message": "Scan All already in progress for this company",
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
	}


def run_scan_all_job(company=None, scan_job_id=None, user=None, **_kwargs):
	"""Background worker entrypoint — read-only Scan All."""
	job_id = scan_job_id
	if not job_id:
		return
	job = _load(job_id) or {"job_id": job_id, "company": company, "user": user}
	job.update(
		{
			"status": "RUNNING",
			"phase": "scan_all",
			"progress": 5,
			"started_at": str(now_datetime()),
			"error": None,
		}
	)
	_save(job_id, job)
	t0 = perf_counter()
	try:
		from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

		# Progress heartbeat phases mirror scan.py topic order.
		job["phase"] = "running_full_integrity_scan"
		job["progress"] = 20
		_save(job_id, job)
		result = run_full_integrity_scan(company=company, include_manufacture=False)
		elapsed = round(perf_counter() - t0, 3)
		job.update(
			{
				"status": "COMPLETED",
				"phase": "completed",
				"progress": 100,
				"finished_at": str(now_datetime()),
				"elapsed_s": elapsed,
				"result": result,
				"timing": (result or {}).get("timing") or {},
				"error": None,
			}
		)
		_save(job_id, job)
	except Exception as exc:
		job.update(
			{
				"status": "FAILED",
				"phase": "failed",
				"progress": 100,
				"finished_at": str(now_datetime()),
				"elapsed_s": round(perf_counter() - t0, 3),
				"error": str(exc)[:2000],
			}
		)
		_save(job_id, job)
		frappe.log_error(title=f"Historical Repair Scan All failed ({job_id})", message=frappe.get_traceback())
	finally:
		# Clear active lock only if we still own it
		active = frappe.cache().get_value(_active_key(company or "_"))
		if str(active or "") == str(job_id):
			frappe.cache().delete_value(_active_key(company or "_"))
	return {"job_id": job_id, "status": job.get("status")}
