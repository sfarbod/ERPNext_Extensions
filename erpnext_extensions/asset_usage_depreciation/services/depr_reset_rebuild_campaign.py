# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset Depreciation Repair Campaign — background orchestration for Reset & Rebuild.

LOCKED: accounting mutations go ONLY through ``reset_and_rebuild_asset``.
This module orchestrates enqueue/claim/chunk/resume and does not re-implement
JE cancel, VAD reconcile, ADS rebuild, or Iran rounding.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild import (
	_reset_and_rebuild_asset,
	analyze_asset,
)

CAMPAIGN_DT = "Asset Depreciation Repair Campaign"
ITEM_DT = "Asset Depreciation Repair Campaign Item"

ITEM_IN_FLIGHT = {"Queued", "Processing"}

NON_RETRYABLE_CODES = {
	"HAS_USAGE_PERIODS",
	"LIFECYCLE_SCRAPPED",
	"LIFECYCLE_SOLD",
	"LIFECYCLE_CANCELLED",
	"LIFECYCLE_DISPOSAL",
	"DISPOSAL_OR_SCRAP",
	"MULTIPLE_ACTIVE_ADS",
	"NEGATIVE_FINAL_BALANCE",
	"INCONSISTENT_GL",
	"FROZEN_PERIOD",
	"HAS_ASSET_VALUE_ADJUSTMENT",
	"HAS_VALUE_ADJUSTMENT",
	"HAS_CAPITALIZED_REPAIR",
	"SCHEDULE_TOTAL_MISMATCH",
	"FINAL_NBV_MISMATCH",
	"ASSET_STATE_RECONCILIATION_FAILED",
	"GL_RESET_FAILED",
	"BUSINESS_CALENDAR_VALIDATION_FAILED",
	"UNEXPECTED_ACCOUNTING_STATE",
	"ADS_COUNT",
	"METHOD_",
	"NO_CALCULATE_DEPRECIATION",
	"ASSET_NOT_SUBMITTED",
	"NON_IRR_COMPANY",
}

RECOMMENDED_REVIEW = {
	"NEGATIVE_FINAL_BALANCE": (
		"Review historical acquisition/depreciation basis, opening accumulated "
		"depreciation, prior value changes, and salvage before any manual repair."
	),
	"HAS_USAGE_PERIODS": (
		"Review historical Usage Period chronology and determine whether the "
		"historical schedule should be rebuilt with usage adjustments."
	),
	"INCONSISTENT_GL": "Reconcile Asset depreciation JEs against GL before rerunning repair.",
	"MULTIPLE_ACTIVE_ADS": "Determine authoritative schedule and lifecycle history before repair.",
	"FROZEN_PERIOD": "Finance must determine approved accounting treatment before cancellation.",
	"LIFECYCLE_SCRAPPED": (
		"Review lifecycle-specific depreciation/disposal accounting; "
		"do not use standard active-Asset reset."
	),
	"LIFECYCLE_SOLD": (
		"Review lifecycle-specific depreciation/disposal accounting; "
		"do not use standard active-Asset reset."
	),
	"LIFECYCLE_DISPOSAL": (
		"Review lifecycle-specific depreciation/disposal accounting; "
		"do not use standard active-Asset reset."
	),
	"HAS_ASSET_VALUE_ADJUSTMENT": "Review Asset Value Adjustment history before automatic reset.",
	"HAS_CAPITALIZED_REPAIR": "Review capitalized Asset Repair history before automatic reset.",
}

DEFAULT_CHUNK_SIZE = 20
DEFAULT_MAX_INFLIGHT = 4
DEFAULT_QUEUE = "long"
DEFAULT_MAX_ATTEMPTS = 3
ABANDONED_PROCESSING_MINUTES = 45


def _require_campaign_permission():
	frappe.has_permission("Asset", "write", throw=True)
	frappe.has_permission("Journal Entry", "cancel", throw=True)


def _clamp_chunk_size(n: int) -> int:
	n = cint(n) or DEFAULT_CHUNK_SIZE
	return max(10, min(25, n))


@frappe.whitelist()
def create_campaign(
	company: str,
	assets: str | list | None = None,
	discover: int | bool = 0,
	chunk_size: int = DEFAULT_CHUNK_SIZE,
	max_inflight_chunks: int = DEFAULT_MAX_INFLIGHT,
	queue_name: str = DEFAULT_QUEUE,
	notes: str | None = None,
) -> dict[str, Any]:
	"""Create a Draft campaign and populate items (explicit list and/or discovery)."""
	_require_campaign_permission()
	company = company or frappe.defaults.get_user_default("Company")
	if not company:
		frappe.throw(_("Company is required."))

	asset_names = _parse_assets(assets)
	if cint(discover):
		asset_names = list(dict.fromkeys(asset_names + _discover_company_assets(company)))

	if not asset_names:
		frappe.throw(_("Provide assets=... and/or discover=1."))

	doc = frappe.get_doc(
		{
			"doctype": CAMPAIGN_DT,
			"company": company,
			"status": "Draft",
			"chunk_size": _clamp_chunk_size(chunk_size),
			"max_inflight_chunks": max(1, cint(max_inflight_chunks) or DEFAULT_MAX_INFLIGHT),
			"queue_name": (queue_name or DEFAULT_QUEUE).strip() or DEFAULT_QUEUE,
			"worker_concurrency": 2,
			"max_attempts": DEFAULT_MAX_ATTEMPTS,
			"created_by_user": frappe.session.user,
			"notes": notes,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	batch: list[str] = []
	for name in asset_names:
		batch.append(name)
		if len(batch) >= 200:
			_insert_items(doc.name, company, batch)
			batch = []
	if batch:
		_insert_items(doc.name, company, batch)

	refresh_campaign_counts(doc.name)
	frappe.db.commit()
	return get_campaign_status(doc.name)


@frappe.whitelist()
def analyze_campaign(campaign: str) -> dict[str, Any]:
	"""Classify every Pending item via analyze_asset (no economic writes)."""
	_require_campaign_permission()
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status not in ("Draft", "Ready", "Paused", "Stopped"):
		frappe.throw(_("Campaign {0} cannot be analyzed in status {1}.").format(campaign, camp.status))

	names = frappe.get_all(
		ITEM_DT,
		filters={"campaign": campaign, "status": ("in", ["Pending", "Retryable Failed"])},
		pluck="name",
	)
	for item_name in names:
		item = frappe.get_doc(ITEM_DT, item_name)
		_classify_item(item, apply_terminal=True)
		item.save(ignore_permissions=True)
		frappe.db.commit()

	camp.reload()
	if camp.status == "Draft":
		camp.db_set("status", "Ready")
	refresh_campaign_counts(campaign)
	frappe.db.commit()
	return get_campaign_status(campaign)


@frappe.whitelist()
def start_campaign(campaign: str, run_inline: int | bool = 0) -> dict[str, Any]:
	"""Mark Running and enqueue bounded chunks. Returns immediately.

	``run_inline=1`` processes one feeder wave in-process (tests / no worker).
	"""
	_require_campaign_permission()
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status not in ("Draft", "Ready", "Paused", "Stopped"):
		frappe.throw(_("Cannot start campaign in status {0}.").format(camp.status))

	_quick_skip_already_repaired(campaign)

	camp.db_set("status", "Running")
	camp.db_set("started_at", camp.started_at or now_datetime())
	camp.db_set("paused_at", None)
	camp.db_set("completed_at", None)
	frappe.db.commit()

	if cint(run_inline):
		processed = _run_inline_wave(campaign)
		return {**get_campaign_status(campaign), "enqueued_chunks": 0, "inline_processed": processed}

	enqueued = enqueue_next_chunks(campaign)
	return {**get_campaign_status(campaign), "enqueued_chunks": enqueued}


def _run_inline_wave(campaign: str) -> int:
	"""Synchronously drain eligible items in chunk-sized batches (dev/test helper)."""
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	chunk_size = _clamp_chunk_size(camp.chunk_size)
	processed = 0
	while True:
		if frappe.db.get_value(CAMPAIGN_DT, campaign, "status") != "Running":
			break
		names = _select_and_queue_chunk(campaign, chunk_size)
		if not names:
			break
		chunk_id = f"{campaign}-inline-{frappe.generate_hash(length=6)}"
		frappe.db.sql(
			f"""
			update `tab{ITEM_DT}`
			set chunk_id=%s, queued_at=%s
			where name in ({", ".join(["%s"] * len(names))})
			""",
			tuple([chunk_id, now_datetime(), *names]),
		)
		frappe.db.commit()
		process_campaign_chunk(campaign, chunk_id, names, feed_next=0)
		processed += len(names)
	_maybe_complete_campaign(campaign)
	return processed


@frappe.whitelist()
def pause_campaign(campaign: str) -> dict[str, Any]:
	_require_campaign_permission()
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status != "Running":
		frappe.throw(_("Only Running campaigns can be paused."))
	camp.db_set("status", "Paused")
	camp.db_set("paused_at", now_datetime())
	frappe.db.commit()
	return get_campaign_status(campaign)


@frappe.whitelist()
def resume_campaign(campaign: str) -> dict[str, Any]:
	_require_campaign_permission()
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status != "Paused":
		frappe.throw(_("Only Paused campaigns can be resumed."))
	recover_abandoned_processing(campaign)
	camp.db_set("status", "Running")
	camp.db_set("paused_at", None)
	frappe.db.commit()
	enqueued = enqueue_next_chunks(campaign)
	return {**get_campaign_status(campaign), "enqueued_chunks": enqueued}


@frappe.whitelist()
def stop_campaign(campaign: str) -> dict[str, Any]:
	_require_campaign_permission()
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status not in ("Running", "Paused"):
		frappe.throw(_("Only Running/Paused campaigns can be stopped."))
	camp.db_set("status", "Stopped")
	camp.db_set("completed_at", now_datetime())
	frappe.db.commit()
	_finalize_exception_register(campaign)
	refresh_campaign_counts(campaign)
	frappe.db.commit()
	return get_campaign_status(campaign)


@frappe.whitelist()
def get_campaign_status(campaign: str) -> dict[str, Any]:
	frappe.has_permission(CAMPAIGN_DT, "read", throw=True)
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	refresh_campaign_counts(campaign, commit=False)
	camp.reload()
	return {
		"campaign": camp.name,
		"company": camp.company,
		"status": camp.status,
		"chunk_size": camp.chunk_size,
		"max_inflight_chunks": camp.max_inflight_chunks,
		"queue_name": camp.queue_name,
		"started_at": str(camp.started_at) if camp.started_at else None,
		"completed_at": str(camp.completed_at) if camp.completed_at else None,
		"paused_at": str(camp.paused_at) if camp.paused_at else None,
		"total_assets": camp.total_assets,
		"pending_count": camp.pending_count,
		"queued_count": camp.queued_count,
		"processing_count": camp.processing_count,
		"success_count": camp.success_count,
		"already_repaired_count": camp.already_repaired_count,
		"manual_review_count": camp.manual_review_count,
		"blocked_count": camp.blocked_count,
		"failed_count": camp.failed_count,
		"retryable_failed_count": camp.retryable_failed_count,
		"total_jes_before": camp.total_jes_before,
		"total_jes_cancelled": camp.total_jes_cancelled,
		"exception_register_generated": cint(camp.exception_register_generated),
		"last_error": camp.last_error,
	}


@frappe.whitelist()
def get_campaign_exceptions(campaign: str) -> list[dict[str, Any]]:
	"""Manual review / exception register rows."""
	frappe.has_permission(ITEM_DT, "read", throw=True)
	return frappe.get_all(
		ITEM_DT,
		filters={
			"campaign": campaign,
			"status": ("in", ["Manual Review", "Blocked", "Failed"]),
		},
		fields=[
			"name",
			"asset",
			"status",
			"classification",
			"reason_code",
			"reason_detail",
			"submitted_jes_before",
			"old_ads",
			"new_ads",
			"ads_rows",
			"fractional_rows_before",
			"old_vad",
			"new_vad",
			"failed_gate",
			"error_message",
			"recommended_review",
			"manual_review_summary",
			"attempt_count",
			"completed_at",
		],
		order_by="asset asc",
		limit_page_length=0,
	)


@frappe.whitelist()
def get_campaign_assets(
	campaign: str,
	status: str | None = None,
	limit: int = 100,
	start: int = 0,
) -> list[dict[str, Any]]:
	frappe.has_permission(ITEM_DT, "read", throw=True)
	filters: dict[str, Any] = {"campaign": campaign}
	if status:
		filters["status"] = status
	return frappe.get_all(
		ITEM_DT,
		filters=filters,
		fields=[
			"name",
			"asset",
			"status",
			"reason_code",
			"submitted_jes_before",
			"jes_cancelled",
			"old_ads",
			"new_ads",
			"g0_g12_pass",
			"attempt_count",
			"chunk_id",
			"error_message",
		],
		order_by="modified desc",
		limit_page_length=cint(limit) or 100,
		limit_start=cint(start) or 0,
	)


@frappe.whitelist()
def export_campaign_exceptions_csv(campaign: str) -> dict[str, Any]:
	"""Return CSV content for specialist review."""
	frappe.has_permission(ITEM_DT, "read", throw=True)
	rows = get_campaign_exceptions(campaign)
	headers = [
		"Asset",
		"Status",
		"Classification",
		"Reason Code",
		"Reason",
		"Submitted Depreciation JEs",
		"Active ADS",
		"ADS Rows",
		"Fractional Rows",
		"VAD",
		"Failed Gate",
		"Recommended Review",
		"Campaign",
		"Last Attempt",
	]
	lines = [",".join(headers)]
	for r in rows:
		lines.append(
			",".join(
				_csv(
					[
						r.get("asset"),
						r.get("status"),
						r.get("classification"),
						r.get("reason_code"),
						r.get("reason_detail") or r.get("error_message"),
						r.get("submitted_jes_before"),
						r.get("old_ads") or r.get("new_ads"),
						r.get("ads_rows"),
						r.get("fractional_rows_before"),
						r.get("old_vad"),
						r.get("failed_gate"),
						r.get("recommended_review"),
						campaign,
						r.get("attempt_count"),
					]
				)
			)
		)
	return {"campaign": campaign, "csv": "\n".join(lines), "rows": len(rows)}


@frappe.whitelist()
def enqueue_next_chunks(campaign: str) -> int:
	"""Bounded feeder — enqueue chunks until max_inflight reached."""
	_require_campaign_permission()
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status != "Running":
		return 0

	recover_abandoned_processing(campaign)
	inflight = frappe.db.count(ITEM_DT, {"campaign": campaign, "status": ("in", list(ITEM_IN_FLIGHT))})
	chunk_size = _clamp_chunk_size(camp.chunk_size)
	approx_chunks = (inflight + chunk_size - 1) // chunk_size if inflight else 0
	max_inflight = max(1, cint(camp.max_inflight_chunks) or DEFAULT_MAX_INFLIGHT)
	enqueued = 0

	while approx_chunks < max_inflight:
		names = _select_and_queue_chunk(campaign, chunk_size)
		if not names:
			break
		chunk_id = f"{campaign}-{frappe.generate_hash(length=8)}"
		frappe.db.sql(
			f"""
			update `tab{ITEM_DT}`
			set chunk_id=%s, queued_at=%s
			where name in ({", ".join(["%s"] * len(names))})
			""",
			tuple([chunk_id, now_datetime(), *names]),
		)
		frappe.db.commit()
		frappe.enqueue(
			"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.process_campaign_chunk",
			queue=camp.queue_name or DEFAULT_QUEUE,
			timeout=1800,
			job_name=f"Asset Depr Repair {chunk_id}",
			campaign=campaign,
			chunk_id=chunk_id,
			item_names=names,
			enqueue_after_commit=True,
		)
		enqueued += 1
		approx_chunks += 1

	refresh_campaign_counts(campaign)
	frappe.db.commit()
	_maybe_complete_campaign(campaign)
	return enqueued


def process_campaign_chunk(
	campaign: str,
	chunk_id: str,
	item_names: list[str] | None = None,
	feed_next: int | bool = 1,
):
	"""Background worker: process each Asset independently with commit/rollback."""
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status == "Paused":
		return {"campaign": campaign, "chunk_id": chunk_id, "skipped": "paused"}
	if camp.status in ("Stopped", "Completed", "Completed With Exceptions", "Failed"):
		return {"campaign": campaign, "chunk_id": chunk_id, "skipped": camp.status}

	if not item_names:
		item_names = frappe.get_all(
			ITEM_DT,
			filters={"campaign": campaign, "chunk_id": chunk_id, "status": "Queued"},
			pluck="name",
		)

	results = []
	for item_name in item_names:
		camp_status = frappe.db.get_value(CAMPAIGN_DT, campaign, "status")
		if camp_status in ("Paused", "Stopped", "Completed", "Completed With Exceptions"):
			break
		try:
			res = _process_one_item(campaign, item_name, chunk_id)
			results.append(res)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			try:
				_record_item_failure(item_name, e, retryable=_is_transient(e), campaign=campaign)
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"campaign item failure record: {item_name}")
			results.append({"item": item_name, "status": "error", "error": str(e)})

	refresh_campaign_counts(campaign)
	frappe.db.commit()
	if cint(feed_next) and frappe.db.get_value(CAMPAIGN_DT, campaign, "status") == "Running":
		try:
			enqueue_next_chunks(campaign)
		except Exception:
			frappe.log_error(title=f"enqueue_next_chunks failed: {campaign}")
	_maybe_complete_campaign(campaign)
	return {"campaign": campaign, "chunk_id": chunk_id, "results": results}


def _process_one_item(campaign: str, item_name: str, chunk_id: str) -> dict[str, Any]:
	token = frappe.generate_hash(length=12)
	claimed = _claim_item(item_name, token, chunk_id)
	if not claimed:
		return {"item": item_name, "status": "claim_failed"}

	item = frappe.get_doc(ITEM_DT, item_name)
	asset = item.asset
	item.attempt_count = cint(item.attempt_count) + 1
	item.started_at = now_datetime()
	item.chunk_id = chunk_id
	item.save(ignore_permissions=True)
	frappe.db.commit()

	plan = analyze_asset(asset)
	item.submitted_jes_before = cint(plan.get("je_count"))
	item.fractional_rows_before = cint(plan.get("fractional_rows"))
	item.old_ads = plan.get("ads")
	item.ads_rows = cint(plan.get("rows"))
	item.due_rows = cint(plan.get("due_rows"))
	_enrich_asset_snapshot(item)

	if _is_already_repaired(plan):
		item.status = "Already Repaired"
		item.classification = "ALREADY_REPAIRED"
		item.reason_code = "ALREADY_REPAIRED"
		item.reason_detail = "No submitted depreciation JEs and no fractional ADS rows."
		item.completed_at = now_datetime()
		item.claim_token = None
		item.save(ignore_permissions=True)
		return {"item": item_name, "asset": asset, "status": "Already Repaired"}

	if plan.get("status") in ("BLOCKED", "MANUAL_REVIEW"):
		_apply_plan_terminal(item, plan)
		item.claim_token = None
		item.completed_at = now_datetime()
		item.save(ignore_permissions=True)
		return {"item": item_name, "asset": asset, "status": item.status}

	before_vad = flt(frappe.db.get_value("Asset", asset, "value_after_depreciation"))
	item.old_vad = before_vad
	out = _reset_and_rebuild_asset(asset)

	status = out.get("status")
	if status == "SUCCESS":
		item.status = "Success"
		item.classification = "SUCCESS"
		item.jes_cancelled = len(out.get("JEs_cancelled") or [])
		item.old_ads = out.get("old_ADS") or item.old_ads
		item.new_ads = out.get("new_ADS")
		item.ads_rows = cint(out.get("rows"))
		item.due_rows = cint(out.get("due_rows"))
		item.new_vad = flt((out.get("after") or {}).get("value_after_depreciation"))
		gates = out.get("gates") or {}
		item.g0_g12_pass = 1 if gates.get("ok") and out.get("whole_number_check") else 0
		item.schedule_total = flt(gates.get("depreciable_total"))
		item.final_installment = flt(gates.get("final_amount"))
		item.fractional_rows_after = 0
		_fill_final_nbv_salvage(item, asset)
		if not item.g0_g12_pass:
			item.status = "Failed"
			item.classification = "FAILED"
			item.reason_code = "UNEXPECTED_ACCOUNTING_STATE"
			item.error_type = "GATES_FAILED"
			item.error_message = "; ".join(gates.get("errors") or out.get("errors") or [])
			item.failed_gate = (gates.get("errors") or ["G?"])[0][:140]
			item.recommended_review = "Review gate errors before retry."
		item.completed_at = now_datetime()
		item.claim_token = None
		item.save(ignore_permissions=True)
		return {"item": item_name, "asset": asset, "status": item.status, "result": out}

	if status in ("BLOCKED", "MANUAL_REVIEW"):
		_apply_plan_terminal(item, out)
		item.claim_token = None
		item.completed_at = now_datetime()
		item.save(ignore_permissions=True)
		return {"item": item_name, "asset": asset, "status": item.status}

	err = "; ".join(out.get("errors") or ["FAILED"])
	code = _reason_code_from_error(err)
	if _is_non_retryable_code(code) or _is_non_retryable_error(err):
		item.status = "Failed"
		if "NEGATIVE_FINAL_BALANCE" in err or (
			"negative" in err.lower() and "balancing" in err.lower()
		):
			item.status = "Manual Review"
			code = "NEGATIVE_FINAL_BALANCE"
		item.classification = item.status.replace(" ", "_").upper()
		item.reason_code = code
		item.error_type = "NON_RETRYABLE"
		item.error_message = err[:500]
		item.recommended_review = RECOMMENDED_REVIEW.get(code, "Specialist review required.")
		item.manual_review_summary = _manual_summary(item, err)
		item.completed_at = now_datetime()
		item.claim_token = None
		item.save(ignore_permissions=True)
		return {"item": item_name, "asset": asset, "status": item.status}

	max_attempts = cint(frappe.db.get_value(CAMPAIGN_DT, campaign, "max_attempts")) or DEFAULT_MAX_ATTEMPTS
	if cint(item.attempt_count) >= max_attempts:
		item.status = "Failed"
		item.classification = "FAILED"
		item.reason_code = "UNEXPECTED_ERROR"
		item.error_type = "RETRY_EXHAUSTED"
		item.error_message = err[:500]
		item.completed_at = now_datetime()
	else:
		item.status = "Retryable Failed"
		item.classification = "RETRYABLE_FAILED"
		item.reason_code = "TRANSIENT_ERROR"
		item.error_type = "RETRYABLE"
		item.error_message = err[:500]
	item.claim_token = None
	item.save(ignore_permissions=True)
	return {"item": item_name, "asset": asset, "status": item.status}


def is_asset_claimed_by_active_campaign(asset_name: str) -> bool:
	"""True if Asset is Queued/Processing in a Running campaign."""
	return bool(
		frappe.db.sql(
			f"""
			select i.name
			from `tab{ITEM_DT}` i
			inner join `tab{CAMPAIGN_DT}` c on c.name = i.campaign
			where i.asset = %s
			  and i.status in ('Queued', 'Processing')
			  and c.status = 'Running'
			limit 1
			""",
			(asset_name,),
		)
	)


def _parse_assets(assets: str | list | None) -> list[str]:
	if not assets:
		return []
	if isinstance(assets, str):
		assets = (
			json.loads(assets)
			if assets.strip().startswith("[")
			else [a.strip() for a in assets.split(",") if a.strip()]
		)
	return [str(a) for a in assets if a]


def _discover_company_assets(company: str) -> list[str]:
	return frappe.get_all(
		"Asset",
		filters={"company": company, "docstatus": 1, "calculate_depreciation": 1},
		pluck="name",
		limit_page_length=0,
	)


def _insert_items(campaign: str, company: str, asset_names: list[str]) -> None:
	for asset in asset_names:
		if frappe.db.exists(ITEM_DT, {"campaign": campaign, "asset": asset}):
			continue
		frappe.get_doc(
			{
				"doctype": ITEM_DT,
				"campaign": campaign,
				"asset": asset,
				"company": company,
				"status": "Pending",
				"attempt_count": 0,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def refresh_campaign_counts(campaign: str, commit: bool = True) -> None:
	rows = frappe.db.sql(
		f"""
		select status, count(*) as c, coalesce(sum(submitted_jes_before),0) as jes_before,
		       coalesce(sum(jes_cancelled),0) as jes_cancelled
		from `tab{ITEM_DT}`
		where campaign=%s
		group by status
		""",
		(campaign,),
		as_dict=True,
	)
	counts = {r.status: cint(r.c) for r in rows}
	total = sum(counts.values())
	jes_before = sum(cint(r.jes_before) for r in rows)
	jes_cancelled = sum(cint(r.jes_cancelled) for r in rows)
	frappe.db.set_value(
		CAMPAIGN_DT,
		campaign,
		{
			"total_assets": total,
			"pending_count": counts.get("Pending", 0),
			"queued_count": counts.get("Queued", 0),
			"processing_count": counts.get("Processing", 0),
			"success_count": counts.get("Success", 0),
			"already_repaired_count": counts.get("Already Repaired", 0),
			"manual_review_count": counts.get("Manual Review", 0),
			"blocked_count": counts.get("Blocked", 0),
			"failed_count": counts.get("Failed", 0),
			"retryable_failed_count": counts.get("Retryable Failed", 0),
			"total_jes_before": jes_before,
			"total_jes_cancelled": jes_cancelled,
		},
		update_modified=False,
	)
	if commit:
		frappe.db.commit()


def _select_and_queue_chunk(campaign: str, chunk_size: int) -> list[str]:
	eligible = frappe.db.sql(
		f"""
		select name from `tab{ITEM_DT}`
		where campaign=%s and status in ('Pending', 'Retryable Failed')
		order by
			case when status='Retryable Failed' then 0 else 1 end,
			modified asc
		limit %s
		for update
		""",
		(campaign, chunk_size),
	)
	names = [r[0] for r in eligible]
	if not names:
		return []
	frappe.db.sql(
		f"""
		update `tab{ITEM_DT}`
		set status='Queued', queued_at=%s
		where name in ({", ".join(["%s"] * len(names))})
		  and status in ('Pending', 'Retryable Failed')
		""",
		tuple([now_datetime(), *names]),
	)
	return names


def _claim_item(item_name: str, token: str, chunk_id: str) -> bool:
	for attempt in range(5):
		try:
			frappe.db.sql(
				f"""
				update `tab{ITEM_DT}`
				set status='Processing', claim_token=%s, chunk_id=%s, started_at=%s
				where name=%s and status in ('Queued', 'Retryable Failed', 'Pending')
				""",
				(token, chunk_id, now_datetime(), item_name),
			)
			row = frappe.db.get_value(ITEM_DT, item_name, ["status", "claim_token"], as_dict=True) or {}
			return row.get("claim_token") == token
		except Exception as e:
			frappe.db.rollback()
			if _is_transient(e) and attempt < 4:
				time.sleep(0.2 * (attempt + 1))
				continue
			raise
	return False


def recover_abandoned_processing(campaign: str) -> int:
	cutoff = frappe.utils.add_to_date(now_datetime(), minutes=-ABANDONED_PROCESSING_MINUTES)
	rows = frappe.db.sql(
		f"""
		select name, attempt_count from `tab{ITEM_DT}`
		where campaign=%s and status='Processing'
		  and (started_at is null or started_at < %s)
		""",
		(campaign, cutoff),
		as_dict=True,
	)
	max_attempts = cint(frappe.db.get_value(CAMPAIGN_DT, campaign, "max_attempts")) or DEFAULT_MAX_ATTEMPTS
	n = 0
	for r in rows:
		new_status = "Failed" if cint(r.attempt_count) >= max_attempts else "Retryable Failed"
		frappe.db.set_value(
			ITEM_DT,
			r.name,
			{
				"status": new_status,
				"claim_token": None,
				"error_type": "ABANDONED_PROCESSING",
				"error_message": "Worker abandoned Processing; recovered on resume/feeder.",
			},
		)
		n += 1
	if n:
		frappe.db.commit()
	return n


def _is_already_repaired(plan: dict[str, Any]) -> bool:
	return (
		plan.get("status") == "READY"
		and cint(plan.get("je_count")) == 0
		and cint(plan.get("fractional_rows")) == 0
	)


def _quick_skip_already_repaired(campaign: str) -> None:
	names = frappe.get_all(ITEM_DT, filters={"campaign": campaign, "status": "Pending"}, pluck="name")
	for item_name in names[:5000]:
		item = frappe.get_doc(ITEM_DT, item_name)
		plan = analyze_asset(item.asset)
		if _is_already_repaired(plan):
			item.status = "Already Repaired"
			item.classification = "ALREADY_REPAIRED"
			item.reason_code = "ALREADY_REPAIRED"
			item.submitted_jes_before = 0
			item.fractional_rows_before = 0
			item.old_ads = plan.get("ads")
			item.completed_at = now_datetime()
			item.save(ignore_permissions=True)
			frappe.db.commit()
		elif plan.get("status") in ("BLOCKED", "MANUAL_REVIEW"):
			_apply_plan_terminal(item, plan)
			item.completed_at = now_datetime()
			item.save(ignore_permissions=True)
			frappe.db.commit()


def _classify_item(item, apply_terminal: bool = True) -> None:
	plan = analyze_asset(item.asset)
	item.submitted_jes_before = cint(plan.get("je_count"))
	item.fractional_rows_before = cint(plan.get("fractional_rows"))
	item.old_ads = plan.get("ads")
	item.ads_rows = cint(plan.get("rows"))
	item.due_rows = cint(plan.get("due_rows"))
	_enrich_asset_snapshot(item)
	if _is_already_repaired(plan):
		item.status = "Already Repaired"
		item.classification = "ALREADY_REPAIRED"
		item.reason_code = "ALREADY_REPAIRED"
		item.completed_at = now_datetime()
	elif plan.get("status") in ("BLOCKED", "MANUAL_REVIEW") and apply_terminal:
		_apply_plan_terminal(item, plan)
		item.completed_at = now_datetime()
	else:
		item.classification = plan.get("status") or "READY"
		if item.status not in {
			"Success",
			"Already Repaired",
			"Manual Review",
			"Blocked",
			"Failed",
		}:
			item.status = "Pending"


def _apply_plan_terminal(item, plan: dict[str, Any]) -> None:
	status = plan.get("status")
	reasons = plan.get("reasons") or []
	reason = plan.get("reason") or ",".join(reasons)
	code = _primary_reason_code(reasons, reason)
	if status == "BLOCKED":
		item.status = "Blocked"
		item.classification = "BLOCKED"
	else:
		item.status = "Manual Review"
		item.classification = "MANUAL_REVIEW"
	item.reason_code = code
	item.reason_detail = reason
	item.error_message = reason
	item.recommended_review = RECOMMENDED_REVIEW.get(
		code, "Specialist review required before automatic repair."
	)
	item.manual_review_summary = _manual_summary(item, reason)


def _primary_reason_code(reasons: list, reason: str | None) -> str:
	if reasons:
		r0 = reasons[0]
		if r0.startswith("LIFECYCLE_"):
			return r0
		if r0.startswith("ADS_COUNT_"):
			return "MULTIPLE_ACTIVE_ADS" if not r0.endswith("_1") else "ADS_COUNT"
		if r0.startswith("METHOD_"):
			return "UNEXPECTED_ACCOUNTING_STATE"
		if r0 == "HAS_VALUE_ADJUSTMENT":
			return "HAS_ASSET_VALUE_ADJUSTMENT"
		return r0
	if reason:
		return reason.split(",")[0]
	return "UNEXPECTED_ACCOUNTING_STATE"


def _enrich_asset_snapshot(item) -> None:
	try:
		asset = frappe.get_doc("Asset", item.asset)
	except Exception:
		return
	item.asset_status = asset.status
	item.old_vad = flt(asset.value_after_depreciation)
	fb = (asset.get("finance_books") or [None])[0]
	if fb:
		item.depreciation_method = fb.depreciation_method
		item.daily_prorata = cint(fb.daily_prorata_based)
		item.salvage_value = flt(fb.expected_value_after_useful_life)


def _fill_final_nbv_salvage(item, asset_name: str) -> None:
	asset = frappe.get_doc("Asset", asset_name)
	fb = (asset.get("finance_books") or [None])[0]
	salvage = flt(fb.expected_value_after_useful_life) if fb else 0
	item.salvage_value = salvage
	ads = item.new_ads
	if not ads:
		return
	last = frappe.db.sql(
		"""
		select accumulated_depreciation_amount
		from `tabDepreciation Schedule`
		where parent=%s order by idx desc limit 1
		""",
		(ads,),
	)
	if last:
		item.final_nbv = flt(asset.net_purchase_amount) - flt(last[0][0])


def _reason_code_from_error(err: str) -> str:
	u = (err or "").upper()
	if "NEGATIVE" in u and "BALANC" in u:
		return "NEGATIVE_FINAL_BALANCE"
	if "SCHEDULE SUM" in u or "DEPRECIABLE TOTAL" in u:
		return "SCHEDULE_TOTAL_MISMATCH"
	if "NBV" in u or "SALVAGE" in u:
		return "FINAL_NBV_MISMATCH"
	if "GL" in u:
		return "GL_RESET_FAILED"
	if "FROZEN" in u:
		return "FROZEN_PERIOD"
	return "UNEXPECTED_ERROR"


def _is_non_retryable_code(code: str) -> bool:
	if not code:
		return False
	for prefix in NON_RETRYABLE_CODES:
		if code == prefix or code.startswith(prefix):
			return True
	return False


def _is_non_retryable_error(err: str) -> bool:
	el = (err or "").lower()
	return any(
		x in el
		for x in (
			"negative",
			"balancing installment",
			"does not equal depreciable",
			"not whole irr",
			"multiple active",
			"frozen",
		)
	)


def _is_transient(exc: Exception) -> bool:
	msg = str(exc).lower()
	return any(
		x in msg
		for x in (
			"deadlock",
			"lock wait",
			"try restarting transaction",
			"operationalerror",
			"interfaceerror",
			"tabseries",
			"1020",
			"1213",
			"1205",
		)
	)


def _record_item_failure(item_name: str, exc: Exception, retryable: bool, campaign: str) -> None:
	max_attempts = cint(frappe.db.get_value(CAMPAIGN_DT, campaign, "max_attempts")) or DEFAULT_MAX_ATTEMPTS
	item = frappe.get_doc(ITEM_DT, item_name)
	item.attempt_count = cint(item.attempt_count) + 1
	item.error_message = str(exc)[:500]
	item.claim_token = None
	if retryable and cint(item.attempt_count) < max_attempts:
		item.status = "Retryable Failed"
		item.error_type = "RETRYABLE"
		item.reason_code = "TRANSIENT_ERROR"
	else:
		item.status = "Failed"
		item.error_type = "UNEXPECTED_ERROR"
		item.reason_code = "UNEXPECTED_ERROR"
		item.completed_at = now_datetime()
		item.recommended_review = "Unexpected worker error; inspect Error Log."
	item.save(ignore_permissions=True)
	frappe.db.set_value(CAMPAIGN_DT, campaign, "last_error", str(exc)[:500])


def _maybe_complete_campaign(campaign: str) -> None:
	camp = frappe.get_doc(CAMPAIGN_DT, campaign)
	if camp.status not in ("Running", "Paused"):
		return
	pending = frappe.db.count(
		ITEM_DT,
		{"campaign": campaign, "status": ("in", ["Pending", "Queued", "Processing", "Retryable Failed"])},
	)
	if pending:
		return
	refresh_campaign_counts(campaign, commit=False)
	camp.reload()
	exceptions = cint(camp.manual_review_count) + cint(camp.blocked_count) + cint(camp.failed_count)
	camp.db_set("status", "Completed With Exceptions" if exceptions else "Completed")
	camp.db_set("completed_at", now_datetime())
	frappe.db.commit()
	_finalize_exception_register(campaign)


def _finalize_exception_register(campaign: str) -> None:
	for row in get_campaign_exceptions(campaign):
		if row.get("manual_review_summary"):
			continue
		item = frappe.get_doc(ITEM_DT, row["name"])
		item.manual_review_summary = _manual_summary(item, item.error_message or item.reason_detail)
		if not item.recommended_review and item.reason_code:
			item.recommended_review = RECOMMENDED_REVIEW.get(
				item.reason_code, "Specialist review required."
			)
		item.save(ignore_permissions=True)
	frappe.db.set_value(CAMPAIGN_DT, campaign, "exception_register_generated", 1)
	frappe.db.commit()


def _manual_summary(item, err: str | None) -> str:
	parts = [
		f"Asset={item.asset}",
		f"Status={item.status}",
		f"Reason={item.reason_code}",
		f"JEs before={item.submitted_jes_before}",
		f"ADS={item.old_ads}",
		f"Frac rows={item.fractional_rows_before}",
		f"VAD={item.old_vad}",
		f"Detail={err or item.reason_detail or ''}",
	]
	return " | ".join(str(p) for p in parts)


def _csv(values: list) -> list[str]:
	out = []
	for v in values:
		s = "" if v is None else str(v)
		if any(c in s for c in [",", '"', "\n"]):
			s = '"' + s.replace('"', '""') + '"'
		out.append(s)
	return out
