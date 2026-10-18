# Copyright (c) 2026, ERPNext Extensions contributors
"""Bench-callable Historical Rebuild Campaign API.

Commands (development.localhost / production under Maintenance Mode):

  create / build_manifest / start / pause / stop / resume / status
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	DEFAULT_INTERNAL_BATCH,
	ITEM_BLOCKED,
	MAX_OUTSTANDING_RIV,
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


def linked_riv_names(campaign: str) -> list[str]:
	"""RIV names stored on this campaign's items only."""
	raw_rows = frappe.db.sql(
		f"""
		select riv_names from `tab{ITEM_DOCTYPE}`
		where campaign=%s and riv_names is not null and riv_names != ''
		""",
		campaign,
	)
	names: list[str] = []
	seen = set()
	for (raw,) in raw_rows:
		try:
			data = json.loads(raw)
		except Exception:
			continue
		if not isinstance(data, list):
			continue
		for n in data:
			n = str(n)
			if n and n not in seen:
				seen.add(n)
				names.append(n)
	return names


def outstanding_linked_rivs(campaign: str) -> list[str]:
	names = linked_riv_names(campaign)
	if not names:
		return []
	return frappe.db.sql(
		"""
		select name from `tabRepost Item Valuation`
		where name in ({})
		  and docstatus=1
		  and status in ('Queued', 'In Progress')
		order by creation asc
		""".format(",".join(["%s"] * len(names))),
		tuple(names),
		pluck=True,
	)


def recover_unscheduled_rivs(campaign: str, limit: int | None = None) -> dict:
	"""Drive campaign-linked Queued/In Progress RIV via Core execute (no enqueue).

	Does not touch unrelated RIV. Does not purge Redis. Does not force status.
	"""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		execute_reposting_entry,
	)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	targets = outstanding_linked_rivs(campaign)
	cap = cint(limit) if limit else len(targets)
	results = []
	for name in targets[:cap]:
		if _flags_block_selection(campaign) == "stopped":
			break
		before = frappe.db.get_value("Repost Item Valuation", name, "status")
		try:
			execute_reposting_entry(name, continue_reposting=False)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			results.append({"name": name, "before": before, "error": str(e)[:200]})
			continue
		after = frappe.db.get_value("Repost Item Valuation", name, "status")
		results.append({"name": name, "before": before, "after": after})
	return {
		"campaign": campaign,
		"outstanding_before": len(targets),
		"attempted": len(results),
		"results": results,
	}


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
	stalls = 0

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

		# Settle already-created campaign RIV before selecting more work.
		outstanding = outstanding_linked_rivs(campaign)
		if outstanding:
			recover_unscheduled_rivs(campaign, limit=MAX_OUTSTANDING_RIV)
			outstanding = outstanding_linked_rivs(campaign)
		if len(outstanding) >= MAX_OUTSTANDING_RIV:
			stalls += 1
			_append_log(
				campaign,
				f"Backpressure: {len(outstanding)} outstanding campaign RIV "
				f"(cap {MAX_OUTSTANDING_RIV}); not selecting new documents",
				level="WARN",
			)
			if stalls >= 3:
				_append_log(
					campaign,
					"Blocking: outstanding campaign RIV did not settle after repeated Core execute",
					level="ERROR",
				)
				break
			time.sleep(2)
			continue
		stalls = 0

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


def resume_identity_drain(
	campaign: str,
	max_cycles: int | None = None,
	seed_limit: int | None = None,
	cover_limit: int | None = None,
) -> dict:
	"""RESUME via identity-seed + Skipped Covered drain (scale path for full rebuild).

	Creates bounded Core Item+WH RIVs from earliest SLE per identity, enqueues to
	long workers, bulk-marks Skipped Covered, then processes remaining pending
	items that still need source reconstruction. Respects PAUSE/STOP selection
	semantics; does not cancel unrelated Core RIV.
	"""
	from erpnext_extensions.iran_accounting.historical_rebuild.identity_seed import (
		run_identity_drain,
	)

	doc = _require_campaign(campaign)
	if doc.status not in (STATUS_PAUSED, STATUS_STOPPED, STATUS_RUNNING, STATUS_FAILED, STATUS_READY):
		frappe.throw(f"Cannot identity-drain from status={doc.status}")
	_append_log(campaign, "RESUME identity-drain — seed + cover + remaining items")
	return run_identity_drain(
		campaign,
		max_cycles=max_cycles,
		seed_limit=cint(seed_limit) or MAX_OUTSTANDING_RIV,
		cover_limit=cint(cover_limit) or 2500,
	)


def _sequential_status_block(campaign: str) -> dict:
	"""Active sequential session progress (canonical under HRC-00001)."""
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		get_seq_session,
		parse_campaign_session_id,
		restore_session_from_file,
	)

	sess = get_seq_session()
	active_sid = parse_campaign_session_id(campaign)
	if not sess and active_sid:
		try:
			restore_session_from_file(active_sid)
			sess = get_seq_session()
		except Exception:
			sess = None
	# Never attribute an unrelated session to this campaign.
	if sess and sess.get("campaign") and sess.get("campaign") != campaign:
		sess = None
	if not sess:
		return {
			"active_session_id": active_sid,
			"session_status": None,
			"session_riv_completed": 0,
			"session_riv_failed": 0,
			"session_riv_created": 0,
			"identities_done": 0,
			"last_failure": None,
			"elapsed_seconds": None,
			"avg_riv_seconds": None,
			"estimated_remaining_seconds": None,
		}

	completed = list(sess.get("riv_completed") or [])
	failed = list(sess.get("riv_failed") or [])
	created = list(sess.get("riv_created") or [])
	done = list(sess.get("identities_done") or [])
	started_at = sess.get("started_at")
	elapsed = None
	avg = None
	eta = None
	try:
		from frappe.utils import get_datetime

		if started_at:
			elapsed = max((now_datetime() - get_datetime(started_at)).total_seconds(), 0)
			n = len(completed)
			if n > 0 and elapsed is not None:
				avg = elapsed / n
				# Remaining distinct identities from chronological plan.
				from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
					chronological_identities,
				)

				total_id = len(chronological_identities(campaign) or [])
				remaining_id = max(total_id - len(done), 0)
				eta = avg * remaining_id
	except Exception:
		pass

	vc = frappe.db.sql(
		"""
		select verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s group by verification_class
		""",
		campaign,
		as_dict=True,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
		COVERED_BY_REPOSTED_AND_VERIFIED,
		DIRECT_REPOSTED_AND_VERIFIED,
		REPOST_PLANNED,
	)

	direct = covered = pending_v = 0
	for r in vc or []:
		if r.verification_class == DIRECT_REPOSTED_AND_VERIFIED:
			direct = cint(r.c)
		elif r.verification_class == COVERED_BY_REPOSTED_AND_VERIFIED:
			covered = cint(r.c)
		elif r.verification_class in (REPOST_PLANNED, None, ""):
			pending_v += cint(r.c)

	return {
		"active_session_id": sess.get("session_id") or active_sid,
		"session_status": sess.get("status"),
		"session_riv_completed": len(completed),
		"session_riv_failed": len(failed),
		"session_riv_created": len(created),
		"identities_done": len(done),
		"last_failure": sess.get("last_failure"),
		"elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
		"avg_riv_seconds": round(avg, 3) if avg is not None else None,
		"estimated_remaining_seconds": round(eta, 3) if eta is not None else None,
		"direct_verified": direct,
		"covered_verified": covered,
		"pending_verification": pending_v,
		"current_riv": created[-1] if created else None,
	}


def status(campaign: str) -> dict:
	"""Read-only campaign progress + recent logs."""
	doc = _require_campaign(campaign)
	counters = _refresh_counters(campaign)
	doc.reload()
	seq = _sequential_status_block(campaign)
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
	# Distinct Failed Item+WH identities (not RIV row count).
	# Prefer current-session failures when a sequential session is active.
	sess_failed = (seq.get("last_failure") and [seq["last_failure"]]) or []
	distinct_failed = frappe.db.sql(
		"""
		select item_code, warehouse, count(*) as riv_count,
		       left(max(error_log), 160) as error_sample
		from `tabRepost Item Valuation`
		where docstatus=1 and status='Failed' and based_on='Item and Warehouse'
		group by item_code, warehouse
		order by riv_count desc
		limit 20
		""",
		as_dict=True,
	)
	outstanding_riv = cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		)
	)
	remaining = (
		cint(counters.get("total", 0))
		- cint(counters.get("completed", 0))
		- cint(counters.get("skipped_covered", 0))
	)
	# Session-aware RIV counters (do not treat old-session Completed as coverage).
	session_completed = cint(seq.get("session_riv_completed") or 0)
	session_failed = cint(seq.get("session_riv_failed") or 0)
	text = f"""
HISTORICAL REBUILD CAMPAIGN

Campaign:
{doc.name}

Status:
{doc.status}

Active Session:
{seq.get('active_session_id') or '—'}

Session Status:
{seq.get('session_status') or '—'}

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

Direct Verified:
{seq.get('direct_verified', 0):,}

Covered Verified:
{seq.get('covered_verified', 0):,}

Session RIV Completed:
{session_completed:,}

Session RIV Failed:
{session_failed:,}

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

Distinct Failed Identities:
{len(distinct_failed)}

Queue Pressure (outstanding RIV):
{outstanding_riv} / {MAX_OUTSTANDING_RIV}

Current:
{(doc.current_voucher_type or '')} {(doc.current_voucher_no or '')}

Current Item / Warehouse:
{(doc.last_voucher_type or '')} / {(doc.last_voucher_no or '')}

Last Failure:
{json.dumps(seq.get('last_failure'), default=str)}

Elapsed / Avg RIV / ETA (s):
{seq.get('elapsed_seconds')} / {seq.get('avg_riv_seconds')} / {seq.get('estimated_remaining_seconds')}

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
		"sequential": seq,
		"active_session_id": seq.get("active_session_id"),
		"session_status": seq.get("session_status"),
		"distinct_failed_identities": distinct_failed,
		"session_last_failure": seq.get("last_failure") or (sess_failed[0] if sess_failed else None),
		"queue_pressure": {"outstanding": outstanding_riv, "cap": MAX_OUTSTANDING_RIV},
		"current": {"type": doc.current_voucher_type, "name": doc.current_voucher_no},
		"current_identity": {
			"item_code": doc.last_voucher_type,
			"warehouse": doc.last_voucher_no,
		},
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


def scan_integrity(company: str | None = None, campaign: str = "HRC-00001") -> dict:
	"""Phase-2 historical integrity scanner (bench-callable)."""
	from erpnext_extensions.iran_accounting.historical_rebuild.integrity_scanner import (
		run_full_scan,
	)

	co = company or frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	return run_full_scan(co, campaign=campaign)


def reevaluate(campaign: str = "HRC-00001", limit: int | None = None) -> dict:
	"""Re-evaluate campaign entries with evidence-based coverage (v5.6.1)."""
	from erpnext_extensions.iran_accounting.historical_rebuild.reevaluate import (
		reevaluate_campaign,
	)

	return reevaluate_campaign(campaign, limit=limit)


def execute_phase3(
	campaign: str = "HRC-00001",
	*,
	batch_size: int = 10,
	max_batches: int = 100,
	pilot_first: bool = True,
) -> dict:
	"""Dependency-aware Phase-3 executor (development only)."""
	from erpnext_extensions.iran_accounting.historical_rebuild.executor import (
		run_until_idle,
	)

	return run_until_idle(
		campaign,
		batch_size=batch_size,
		max_batches=max_batches,
		pilot_first=pilot_first,
	)


def run_full_historical_rebuild(
	campaign: str = "HRC-00001",
	*,
	batch_size: int = 25,
	max_batches: int = 500,
	reset: bool = True,
) -> dict:
	"""No-skip full historical valuation rebuild (v5.6.1). Development only."""
	from erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild import (
		run_full_rebuild,
	)

	return run_full_rebuild(
		campaign,
		batch_size=batch_size,
		max_batches=max_batches,
		reset=reset,
	)


def full_rebuild_progress(campaign: str = "HRC-00001") -> dict:
	from erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild import (
		campaign_progress,
	)

	return campaign_progress(campaign)


def prepare_sequential_rebuild(campaign: str = "HRC-00001") -> dict:
	"""Delete all RIVs, reset campaign to REPOST_PLANNED, open sequential session."""
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		prepare_clean_start,
	)

	return prepare_clean_start(campaign)


def run_sequential_rebuild(
	campaign: str = "HRC-00001",
	*,
	max_identities: int | None = None,
	stop_on_failure: bool = True,
) -> dict:
	"""Chronological single-RIV rebuild (MAX_ACTIVE_RIV=1)."""
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		run_sequential,
	)

	return run_sequential(
		campaign,
		max_identities=max_identities,
		stop_on_failure=stop_on_failure,
	)
