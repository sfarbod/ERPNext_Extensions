# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Repair metrics snapshot — dashboard without full ledger rescan.

Persists the last successful Scan All / incremental update so page load can
render KPIs immediately (FRESH or STALE) without scanning 100k+ SLE rows.
"""

from __future__ import annotations

import json
from time import perf_counter

import frappe
from frappe.utils import cint, get_datetime, now_datetime

DOCTYPE = "Historical Repair Metrics Snapshot"
CACHE_TTL = 60 * 60 * 24
FRESHNESS_FRESH = "FRESH"
FRESHNESS_STALE = "STALE"
FRESHNESS_SCANNING = "SCANNING"
FRESHNESS_FAILED = "FAILED"
FRESHNESS_NOT_SCANNED = "NOT_SCANNED"

# Dashboard chips shown by default (operational view).
PRIORITY_KPI_LABELS = [
	"Integrity Score",
	"User Action Required",
	"Tool Limit",
	"I1 Negative Rate",
	"I4 Leftover",
	"Posting Order",
	"Wrong Rate",
	"Zero Rate",
	"Broken GL",
	"Failed RIV",
	"Failed RIV Actionable",
	"Broken Bin",
	"Patient Zero",
]

# Age (minutes) after which a successful snapshot is labeled STALE.
STALE_AFTER_MINUTES = 60


def _cache_key(company: str | None) -> str:
	return f"hr_metrics_snapshot:{(company or '_').strip()}"


def _ensure_doctype() -> bool:
	return bool(frappe.db.exists("DocType", DOCTYPE))


def _queue_name_matches(qname: str, queue: str) -> bool:
	"""Match bare queue names and site-prefixed RQ names (``site:long``)."""
	if not qname:
		return False
	if qname == queue:
		return True
	# frappe/rq often registers ``workspace-…:long`` while callers probe ``long``.
	return qname.endswith(f":{queue}")


def worker_queue_status(queue: str = "long") -> dict:
	"""Detect whether an RQ worker is listening for ``queue``.

	During controlled campaigns workers/scheduler are often paused. The UI must
	surface WORKER_UNAVAILABLE instead of QUEUED 0% forever.
	"""
	out = {
		"queue": queue,
		"workers_total": 0,
		"workers_for_queue": 0,
		"available": False,
		"queued_jobs": 0,
		"worker_names": [],
		"message": "",
	}
	try:
		from frappe.utils.background_jobs import get_redis_conn
		from rq import Queue, Worker

		conn = get_redis_conn()
		workers = Worker.all(connection=conn)
		out["workers_total"] = len(workers)
		matched_qnames = set()
		for w in workers:
			qnames = []
			try:
				qnames = [q.name for q in (w.queues or [])]
			except Exception:
				qnames = []
			# Stale RQ registrations have no queues and must not count as available.
			if not qnames:
				continue
			for qn in qnames:
				if _queue_name_matches(qn, queue):
					out["workers_for_queue"] += 1
					out["worker_names"].append(w.name)
					matched_qnames.add(qn)
					break
		# Count jobs on bare + any matched prefixed queue name.
		queued = 0
		seen = set()
		for qn in [queue, *sorted(matched_qnames)]:
			if qn in seen:
				continue
			seen.add(qn)
			try:
				queued += len(Queue(qn, connection=conn))
			except Exception:
				pass
		out["queued_jobs"] = queued
		out["available"] = out["workers_for_queue"] > 0
		if not out["available"]:
			out["message"] = (
				"Background workers are paused or not listening to the long queue. "
				"Start workers or use a supported foreground/read-only summary refresh."
			)
		else:
			out["message"] = f"{out['workers_for_queue']} worker(s) listening on '{queue}'"
	except Exception as exc:
		out["message"] = f"Worker probe failed: {exc}"
		out["available"] = False
	return out


def _freshness_for(scanned_at, *, scanning: bool = False, failed: bool = False) -> str:
	if scanning:
		return FRESHNESS_SCANNING
	if failed:
		return FRESHNESS_FAILED
	if not scanned_at:
		return FRESHNESS_NOT_SCANNED
	try:
		dt = get_datetime(scanned_at)
		age_min = (get_datetime(now_datetime()) - dt).total_seconds() / 60.0
		if age_min > STALE_AFTER_MINUTES:
			return FRESHNESS_STALE
		return FRESHNESS_FRESH
	except Exception:
		return FRESHNESS_STALE


def save_metrics_snapshot(
	*,
	company: str | None,
	dashboard: dict | None,
	timing: dict | None = None,
	source: str = "scan_all",
	job_id: str | None = None,
	extra: dict | None = None,
) -> dict:
	"""Persist aggregate KPIs after a full/partial scan or incremental update."""
	# DocType.company is required — use sentinel for site-wide / null-company scans.
	company_key = (company or "").strip() or "_ALL_"
	dashboard = dict(dashboard or {})
	# Enrich with blocker lane counts (cheap SQL) when DocType exists.
	try:
		if frappe.db.exists("DocType", "Historical Repair Blocker"):
			filters = {"company": company} if company else {}
			dashboard["User Action Required"] = frappe.db.count(
				"Historical Repair Blocker", {**filters, "lane": "USER_ACTION_REQUIRED", "status": ("!=", "RESOLVED")}
			) if company else frappe.db.count(
				"Historical Repair Blocker", {"lane": "USER_ACTION_REQUIRED", "status": ("!=", "RESOLVED")}
			)
			dashboard["Tool Limit"] = frappe.db.count(
				"Historical Repair Blocker", {**filters, "lane": "TOOL_LIMIT", "status": ("!=", "RESOLVED")}
			) if company else frappe.db.count(
				"Historical Repair Blocker", {"lane": "TOOL_LIMIT", "status": ("!=", "RESOLVED")}
			)
	except Exception:
		pass

	scanned_at = now_datetime()
	payload = {
		"company": company,
		"company_key": company_key,
		"scanned_at": str(scanned_at),
		"freshness": FRESHNESS_FRESH,
		"source": source,
		"job_id": job_id,
		"dashboard": dashboard,
		"timing": timing or {},
		"priority_labels": list(PRIORITY_KPI_LABELS),
		"extra": extra or {},
		"saved_at": str(scanned_at),
	}
	frappe.cache().set_value(_cache_key(company), payload, expires_in_sec=CACHE_TTL)
	# Also index under the company used by the UI when Scan All had company=None.
	if company_key != (company or "").strip():
		frappe.cache().set_value(_cache_key(company_key), payload, expires_in_sec=CACHE_TTL)

	if _ensure_doctype():
		name = frappe.db.get_value(DOCTYPE, {"company": company_key}, "name")
		doc = frappe.get_doc(DOCTYPE, name) if name else frappe.new_doc(DOCTYPE)
		doc.company = company_key
		doc.scanned_at = scanned_at
		doc.freshness = FRESHNESS_FRESH
		doc.source = source
		doc.job_id = job_id
		doc.metrics_json = frappe.as_json(dashboard)
		doc.timing_json = frappe.as_json(timing or {})
		doc.extra_json = frappe.as_json(extra or {})
		doc.flags.ignore_permissions = True
		if name:
			doc.save(ignore_permissions=True)
		else:
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
		payload["name"] = doc.name
	return payload


def load_metrics_snapshot(company: str | None = None) -> dict | None:
	company = company or None
	cached = frappe.cache().get_value(_cache_key(company))
	if isinstance(cached, str):
		try:
			cached = json.loads(cached)
		except Exception:
			cached = None
	if isinstance(cached, dict) and cached.get("dashboard") is not None:
		cached = dict(cached)
		cached["freshness"] = _freshness_for(cached.get("scanned_at"))
		return cached

	if not _ensure_doctype():
		return None
	name = frappe.db.get_value(DOCTYPE, {"company": company or ""}, "name")
	if not name:
		return None
	doc = frappe.get_doc(DOCTYPE, name)
	try:
		dashboard = json.loads(doc.metrics_json or "{}")
	except Exception:
		dashboard = {}
	try:
		timing = json.loads(doc.timing_json or "{}")
	except Exception:
		timing = {}
	try:
		extra = json.loads(doc.extra_json or "{}")
	except Exception:
		extra = {}
	payload = {
		"company": doc.company or company,
		"scanned_at": str(doc.scanned_at) if doc.scanned_at else None,
		"freshness": _freshness_for(doc.scanned_at),
		"source": doc.source,
		"job_id": doc.job_id,
		"dashboard": dashboard,
		"timing": timing,
		"extra": extra,
		"priority_labels": list(PRIORITY_KPI_LABELS),
		"name": doc.name,
	}
	frappe.cache().set_value(_cache_key(company), payload, expires_in_sec=CACHE_TTL)
	return payload


def patch_metrics_snapshot(company: str | None, updates: dict, *, source: str = "incremental") -> dict:
	"""Merge KPI updates into the latest snapshot (incremental rescan)."""
	current = load_metrics_snapshot(company) or {
		"company": company,
		"dashboard": {},
		"timing": {},
		"extra": {},
	}
	dash = dict(current.get("dashboard") or {})
	for k, v in (updates or {}).items():
		dash[k] = v
	return save_metrics_snapshot(
		company=company,
		dashboard=dash,
		timing=current.get("timing") or {},
		source=source,
		job_id=current.get("job_id"),
		extra={**(current.get("extra") or {}), "last_incremental": str(now_datetime())},
	)


def get_dashboard_summary(company: str | None = None) -> dict:
	"""Fast page-load payload: snapshot + worker + pending job. No full scan."""
	t0 = perf_counter()
	company = company or None
	workers = worker_queue_status("long")
	snapshot = load_metrics_snapshot(company)
	pending = None
	try:
		from erpnext_extensions.iran_accounting.historical_stock.scan_job import get_active_scan_job

		pending = get_active_scan_job(company)
	except Exception:
		pending = None

	freshness = FRESHNESS_NOT_SCANNED
	dashboard = {}
	scanned_at = None
	source = None
	if snapshot:
		freshness = snapshot.get("freshness") or FRESHNESS_STALE
		dashboard = snapshot.get("dashboard") or {}
		scanned_at = snapshot.get("scanned_at")
		source = snapshot.get("source")
	if pending and pending.get("status") in ("QUEUED", "RUNNING"):
		freshness = FRESHNESS_SCANNING

	# Always refresh blocker lane chips (cheap).
	try:
		if frappe.db.exists("DocType", "Historical Repair Blocker"):
			filters = {"company": company} if company else {}
			ua = {"lane": "USER_ACTION_REQUIRED", "status": ("!=", "RESOLVED")}
			tl = {"lane": "TOOL_LIMIT", "status": ("!=", "RESOLVED")}
			if company:
				ua["company"] = company
				tl["company"] = company
			dashboard = dict(dashboard)
			dashboard["User Action Required"] = frappe.db.count("Historical Repair Blocker", ua)
			dashboard["Tool Limit"] = frappe.db.count("Historical Repair Blocker", tl)
	except Exception:
		pass

	return {
		"ok": True,
		"company": company,
		"freshness": freshness,
		"scanned_at": scanned_at,
		"source": source,
		"dashboard": dashboard,
		"priority_labels": list(PRIORITY_KPI_LABELS),
		"worker": workers,
		"pending_job": pending,
		"has_snapshot": bool(snapshot),
		"elapsed_ms": round((perf_counter() - t0) * 1000, 1),
		"stale_after_minutes": STALE_AFTER_MINUTES,
		"message": (
			workers.get("message")
			if not workers.get("available")
			else (
				"Showing last successful metrics snapshot."
				if snapshot
				else "No scan snapshot yet — run Scan All when workers are available."
			)
		),
	}
