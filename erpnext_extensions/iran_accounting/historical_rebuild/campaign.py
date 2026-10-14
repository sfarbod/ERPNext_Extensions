# Copyright (c) 2026, ERPNext Extensions contributors
"""Bench-callable Historical Rebuild Campaign API.

Commands (development.localhost / production under Maintenance Mode):

  create / build_manifest / start / pause / stop / resume / status
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	DEFAULT_INTERNAL_BATCH,
	ITEM_BLOCKED,
	ITEM_COMPLETED,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_RIV_LINKED,
	ITEM_SKIPPED,
	MAX_LOG_ROWS,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_FAILED,
	STATUS_PAUSED,
	STATUS_READY,
	STATUS_RUNNING,
	STATUS_STOPPED,
)
from erpnext_extensions.iran_accounting.historical_rebuild.discovery import (
	build_source_manifest,
	doctype_counts,
	manifest_checksum,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import process_campaign_item


def _require_campaign(campaign: str):
	if not campaign:
		frappe.throw("campaign name is required")
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(f"Campaign {campaign} not found")
	return frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)


def _append_log(campaign: str, message: str, level: str = "INFO", voucher_type=None, voucher_no=None):
	doc = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	doc.append(
		"execution_logs",
		{
			"log_time": now_datetime(),
			"level": level,
			"message": (message or "")[:500],
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
		},
	)
	# Trim oldest logs in memory before save.
	while len(doc.execution_logs) > MAX_LOG_ROWS:
		doc.execution_logs.pop(0)
	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _refresh_counters(campaign: str) -> dict[str, int]:
	rows = frappe.db.sql(
		f"""
		select item_status, count(*) as c
		from `tab{ITEM_DOCTYPE}`
		where campaign=%s
		group by item_status
		""",
		campaign,
		as_dict=True,
	)
	counts = {r.item_status: cint(r.c) for r in rows}
	total = sum(counts.values())
	completed = counts.get(ITEM_COMPLETED, 0)
	pending = counts.get(ITEM_PENDING, 0) + counts.get(ITEM_RIV_LINKED, 0)
	failed = counts.get(ITEM_FAILED, 0)
	blocked = counts.get(ITEM_BLOCKED, 0)
	skipped = counts.get(ITEM_SKIPPED, 0)

	# RIV stats from linked names (best-effort aggregate).
	riv_names = set()
	raw_rows = frappe.db.sql(
		f"""
		select riv_names from `tab{ITEM_DOCTYPE}`
		where campaign=%s and riv_names is not null and riv_names != ''
		""",
		campaign,
	)
	for (raw,) in raw_rows:
		try:
			data = json.loads(raw)
			if isinstance(data, list):
				riv_names.update(str(x) for x in data if x)
		except Exception:
			pass

	riv_stats = {
		"riv_created": len(riv_names),
		"riv_completed": 0,
		"riv_queued": 0,
		"riv_in_progress": 0,
		"riv_failed": 0,
	}
	if riv_names:
		for st, key in (
			("Completed", "riv_completed"),
			("Queued", "riv_queued"),
			("In Progress", "riv_in_progress"),
			("Failed", "riv_failed"),
			("Skipped", "riv_completed"),  # count skipped as settled
		):
			n = cint(
				frappe.db.sql(
					"""
					select count(*) from `tabRepost Item Valuation`
					where name in ({})
					  and status=%s
					""".format(",".join(["%s"] * len(riv_names))),
					tuple(list(riv_names) + [st]),
				)[0][0]
			)
			if st == "Skipped":
				riv_stats["riv_completed"] += n
			else:
				riv_stats[key] = n

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"total_documents": total,
			"completed": completed,
			"pending": pending,
			"failed": failed,
			"blocked": blocked,
			"skipped_covered": skipped,
			**riv_stats,
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"total": total,
		"completed": completed,
		"pending": pending,
		"failed": failed,
		"blocked": blocked,
		"skipped_covered": skipped,
		**riv_stats,
	}


def create(company: str | None = None, posting_boundary_from: str | None = None, remarks: str | None = None) -> dict:
	"""Create a Draft Historical Rebuild Campaign."""
	if not company:
		company = frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		company = frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="creation asc")
	if not company:
		frappe.throw("company is required")
	doc = frappe.get_doc(
		{
			"doctype": CAMPAIGN_DOCTYPE,
			"company": company,
			"status": STATUS_DRAFT,
			"posting_boundary_from": posting_boundary_from,
			"remarks": remarks or "Phase 2 complete historical rebuild",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	_append_log(doc.name, f"Created campaign for company={company}")
	return {"campaign": doc.name, "status": doc.status, "company": company}


def build_manifest(campaign: str) -> dict:
	"""Discover all eligible documents and persist campaign items."""
	doc = _require_campaign(campaign)
	if doc.status not in (STATUS_DRAFT, STATUS_READY, STATUS_STOPPED):
		frappe.throw(f"Cannot build manifest while status={doc.status}")

	# Clear previous items if rebuilding draft/ready.
	existing = frappe.get_all(ITEM_DOCTYPE, filters={"campaign": campaign}, pluck="name")
	for name in existing:
		frappe.delete_doc(ITEM_DOCTYPE, name, ignore_permissions=True, force=True)
	frappe.db.commit()

	rows = build_source_manifest(
		company=doc.company,
		posting_boundary_from=doc.posting_boundary_from,
	)
	checksum = manifest_checksum(rows)
	counts = doctype_counts(rows)

	# Bulk insert items in chunks.
	chunk = []
	for r in rows:
		chunk.append(
			{
				"doctype": ITEM_DOCTYPE,
				"campaign": campaign,
				"ordinal": r["ordinal"],
				"company": r.get("company") or doc.company,
				"voucher_type": r["doctype"],
				"voucher_no": r["name"],
				"posting_date": r["posting_date"],
				"posting_time": str(r.get("posting_time") or ""),
				"creation_ts": r.get("creation"),
				"item_status": ITEM_PENDING,
			}
		)
		if len(chunk) >= 500:
			for row in chunk:
				frappe.get_doc(row).insert(ignore_permissions=True)
			frappe.db.commit()
			chunk = []
	for row in chunk:
		frappe.get_doc(row).insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"status": STATUS_READY,
			"manifest_checksum": checksum,
			"total_documents": len(rows),
			"completed": 0,
			"pending": len(rows),
			"failed": 0,
			"blocked": 0,
			"skipped_covered": 0,
			"last_ordinal": 0,
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	_append_log(campaign, f"Manifest built: total={len(rows)} checksum={checksum[:12]}… counts={counts}")
	_refresh_counters(campaign)
	return {
		"campaign": campaign,
		"status": STATUS_READY,
		"total": len(rows),
		"checksum": checksum,
		"doctype_counts": counts,
	}


def pause(campaign: str) -> dict:
	"""Request pause — stop selecting new campaign items only."""
	doc = _require_campaign(campaign)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"pause_requested": 1, "status": STATUS_PAUSED, "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	_append_log(campaign, "PAUSE requested — no new campaign selections")
	return {"campaign": campaign, "status": STATUS_PAUSED, "pause_requested": 1}


def stop(campaign: str) -> dict:
	"""Hard stop selection until explicit start/resume policy."""
	doc = _require_campaign(campaign)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"stop_requested": 1,
			"pause_requested": 0,
			"status": STATUS_STOPPED,
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	_append_log(campaign, "STOP requested — selection halted until START/RESUME")
	return {"campaign": campaign, "status": STATUS_STOPPED, "stop_requested": 1}


def _flags_block_selection(campaign: str) -> str | None:
	row = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		["status", "pause_requested", "stop_requested"],
		as_dict=True,
	)
	if not row:
		return "missing"
	if cint(row.stop_requested) or row.status == STATUS_STOPPED:
		return "stopped"
	if cint(row.pause_requested) or row.status == STATUS_PAUSED:
		return "paused"
	return None


def _run_loop(campaign: str, *, batch_size: int | None = None, max_items: int | None = None) -> dict:
	"""Continuous processing until pause/stop/complete/max_items."""
	batch_size = cint(batch_size) or DEFAULT_INTERNAL_BATCH
	processed = 0
	results = []

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"status": STATUS_RUNNING,
			"pause_requested": 0,
			"stop_requested": 0,
			"started_at": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "started_at") or now_datetime(),
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	_append_log(campaign, f"Processing loop started batch_size={batch_size}")

	def stop_check():
		return _flags_block_selection(campaign) is not None

	while True:
		block = _flags_block_selection(campaign)
		if block:
			_append_log(campaign, f"Selection halted ({block})")
			break

		items = frappe.get_all(
			ITEM_DOCTYPE,
			filters={
				"campaign": campaign,
				"item_status": ("in", [ITEM_PENDING, ITEM_RIV_LINKED]),
			},
			fields=["name", "ordinal", "voucher_type", "voucher_no"],
			order_by="ordinal asc",
			limit_page_length=batch_size,
		)
		if not items:
			frappe.db.set_value(
				CAMPAIGN_DOCTYPE,
				campaign,
				{
					"status": STATUS_COMPLETED,
					"completed_at": now_datetime(),
					"current_voucher_type": None,
					"current_voucher_no": None,
					"last_activity": now_datetime(),
				},
				update_modified=False,
			)
			frappe.db.commit()
			_append_log(campaign, "Campaign completed — no pending items")
			break

		for it in items:
			block = _flags_block_selection(campaign)
			if block:
				_append_log(campaign, f"Selection halted mid-batch ({block})")
				break

			frappe.db.set_value(
				CAMPAIGN_DOCTYPE,
				campaign,
				{
					"current_voucher_type": it.voucher_type,
					"current_voucher_no": it.voucher_no,
					"last_activity": now_datetime(),
				},
				update_modified=False,
			)
			frappe.db.commit()

			try:
				res = process_campaign_item(it.name, stop_check=stop_check)
			except Exception as e:
				frappe.db.rollback()
				frappe.db.set_value(
					ITEM_DOCTYPE,
					it.name,
					{
						"item_status": ITEM_FAILED,
						"error_message": str(e)[:1000],
					},
					update_modified=False,
				)
				frappe.db.commit()
				res = {"final_status": ITEM_FAILED, "error": str(e)}
				_append_log(
					campaign,
					f"Item failed: {e}",
					level="ERROR",
					voucher_type=it.voucher_type,
					voucher_no=it.voucher_no,
				)

			frappe.db.set_value(
				CAMPAIGN_DOCTYPE,
				campaign,
				{
					"last_ordinal": it.ordinal,
					"last_voucher_type": it.voucher_type,
					"last_voucher_no": it.voucher_no,
					"last_activity": now_datetime(),
				},
				update_modified=False,
			)
			frappe.db.commit()
			processed += 1
			results.append(
				{
					"ordinal": it.ordinal,
					"voucher": f"{it.voucher_type}/{it.voucher_no}",
					"status": res.get("final_status"),
				}
			)
			if max_items and processed >= cint(max_items):
				_append_log(campaign, f"max_items={max_items} reached — leaving Running/caller decides")
				_refresh_counters(campaign)
				return {
					"campaign": campaign,
					"processed": processed,
					"halt": "max_items",
					"results": results[-20:],
					"counters": _refresh_counters(campaign),
				}

		_refresh_counters(campaign)

	counters = _refresh_counters(campaign)
	# If paused/stopped mid-way, keep that status.
	block = _flags_block_selection(campaign)
	if block == "paused":
		frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, "status", STATUS_PAUSED, update_modified=False)
	elif block == "stopped":
		frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, "status", STATUS_STOPPED, update_modified=False)
	frappe.db.commit()
	return {
		"campaign": campaign,
		"processed": processed,
		"halt": block or "completed",
		"results": results[-50:],
		"counters": counters,
		"status": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"),
	}


def start(campaign: str, batch_size: int | None = None, max_items: int | None = None) -> dict:
	"""START continuous processing until pause/stop/complete."""
	doc = _require_campaign(campaign)
	if doc.status not in (STATUS_READY, STATUS_STOPPED, STATUS_PAUSED, STATUS_FAILED, STATUS_RUNNING):
		if doc.status == STATUS_COMPLETED:
			frappe.throw("Campaign already Completed")
		if doc.status == STATUS_DRAFT:
			frappe.throw("Build manifest before START")
	return _run_loop(campaign, batch_size=batch_size, max_items=max_items)


def resume(campaign: str, batch_size: int | None = None, max_items: int | None = None) -> dict:
	"""RESUME from Paused/Stopped — continue pending items."""
	doc = _require_campaign(campaign)
	if doc.status not in (STATUS_PAUSED, STATUS_STOPPED, STATUS_RUNNING, STATUS_FAILED):
		frappe.throw(f"Cannot RESUME from status={doc.status}")
	_append_log(campaign, "RESUME — continuing pending items")
	return _run_loop(campaign, batch_size=batch_size, max_items=max_items)


def status(campaign: str) -> dict:
	"""Read-only campaign progress + recent logs."""
	doc = _require_campaign(campaign)
	counters = _refresh_counters(campaign)
	doc.reload()
	logs = []
	for row in reversed(list(doc.execution_logs or [])[-15:]):
		logs.append(
			{
				"time": str(row.log_time),
				"level": row.level,
				"message": row.message,
				"voucher": f"{row.voucher_type or ''}/{row.voucher_no or ''}".strip("/"),
			}
		)
	recent_errors = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign, "item_status": ("in", [ITEM_FAILED, ITEM_BLOCKED])},
		fields=["ordinal", "voucher_type", "voucher_no", "item_status", "error_message"],
		order_by="ordinal desc",
		limit_page_length=10,
	)
	remaining = (
		cint(counters.get("total", 0))
		- cint(counters.get("completed", 0))
		- cint(counters.get("skipped_covered", 0))
	)
	text = f"""
HISTORICAL REBUILD CAMPAIGN

Campaign:
{doc.name}

Status:
{doc.status}

Total Documents:
{counters.get('total', 0):,}

Completed:
{counters.get('completed', 0):,}

Remaining:
{max(remaining, 0):,}

Pending:
{counters.get('pending', 0):,}

Failed:
{counters.get('failed', 0):,}

Blocked:
{counters.get('blocked', 0):,}

Skipped Covered:
{counters.get('skipped_covered', 0):,}

Core RIV Created:
{counters.get('riv_created', 0):,}

Core RIV Completed:
{counters.get('riv_completed', 0):,}

Core RIV Queued:
{counters.get('riv_queued', 0):,}

Core RIV In Progress:
{counters.get('riv_in_progress', 0):,}

Core RIV Failed:
{counters.get('riv_failed', 0):,}

Current:
{(doc.current_voucher_type or '')} {(doc.current_voucher_no or '')}

Last Processed:
{(doc.last_voucher_type or '')} {(doc.last_voucher_no or '')}

Last Activity:
{doc.last_activity}

Recent Errors:
{json.dumps(recent_errors, default=str, indent=2)}

Recent Logs:
{json.dumps(logs, default=str, indent=2)}
""".strip()
	return {
		"campaign": doc.name,
		"status": doc.status,
		"counters": counters,
		"current": {"type": doc.current_voucher_type, "name": doc.current_voucher_no},
		"last": {
			"ordinal": doc.last_ordinal,
			"type": doc.last_voucher_type,
			"name": doc.last_voucher_no,
		},
		"last_activity": str(doc.last_activity) if doc.last_activity else None,
		"started_at": str(doc.started_at) if doc.started_at else None,
		"completed_at": str(doc.completed_at) if doc.completed_at else None,
		"manifest_checksum": doc.manifest_checksum,
		"recent_errors": recent_errors,
		"recent_logs": logs,
		"text": text,
	}
