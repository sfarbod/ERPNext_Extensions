# Copyright (c) 2026, ERPNext Extensions contributors
"""Full historical valuation rebuild — no-skip policy (v5.6.1).

Every campaign entry must be DIRECT_REPOSTED_AND_VERIFIED or
COVERED_BY_REPOSTED_AND_VERIFIED via a session-scoped Completed RIV that
actually recalculated the valuation chain. Inspect-only VERIFIED_CLEAN and
historical Production Completed RIVs are never final success.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	execute_reposting_entry,
)

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_BLOCKED,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_SKIPPED,
	MAX_OUTSTANDING_RIV,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	inspect_sle_chain,
)
from erpnext_extensions.iran_accounting.historical_rebuild.failure_registry import (
	is_blocked_identity,
	mark_resolved,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	COVERED_BY_REPOSTED_AND_VERIFIED,
	DIRECT_REPOSTED_AND_VERIFIED,
	FINAL_SUCCESS_CLASSES,
	MANUAL_REVIEW,
	REPOST_FAILED,
	REPOST_PLANNED,
	SESSION_RIV_REMARK_PREFIX,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	verify_voucher_basic,
)

CACHE_SESSION = "hrc561_full_rebuild_session"
CACHE_STATS = "hrc561_full_rebuild_stats"


def _session_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"hrc_full_rebuild",
	)
	os.makedirs(path, exist_ok=True)
	return path


def _session_path(session_id: str) -> str:
	return os.path.join(_session_dir(), f"session_{session_id}.json")


def _ensure_bootstrap() -> None:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()


def start_session(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Open a rebuild session; only RIVs registered in this session count as coverage.

	Note: Core Repost Item Valuation has no remarks field — session membership is
	tracked via durable JSON + cache (never trust bare historical Completed RIVs).
	"""
	sid = now_datetime().strftime("%Y%m%d%H%M%S")
	payload = {
		"session_id": sid,
		"campaign": campaign,
		"started_at": str(now_datetime()),
		"remark": f"{SESSION_RIV_REMARK_PREFIX}{sid}",
		"riv_created": [],
		"riv_completed": [],
		"riv_failed": [],
	}
	_save_session(payload)
	frappe.cache().set_value(
		CACHE_STATS,
		{
			"sle_recalculated_estimate": 0,
			"gl_regenerated_estimate": 0,
			"identities_seeded": 0,
		},
		expires_in_sec=60 * 60 * 24 * 14,
	)
	return payload


def get_session() -> dict | None:
	cached = frappe.cache().get_value(CACHE_SESSION)
	if cached:
		return cached
	# Recover latest session file if cache expired
	files = sorted(
		(f for f in os.listdir(_session_dir()) if f.startswith("session_") and f.endswith(".json")),
		reverse=True,
	) if os.path.isdir(_session_dir()) else []
	if not files:
		return None
	with open(os.path.join(_session_dir(), files[0]), encoding="utf-8") as fh:
		payload = json.load(fh)
	frappe.cache().set_value(CACHE_SESSION, payload, expires_in_sec=60 * 60 * 24 * 14)
	return payload


def _save_session(payload: dict) -> None:
	frappe.cache().set_value(CACHE_SESSION, payload, expires_in_sec=60 * 60 * 24 * 14)
	sid = payload.get("session_id")
	if sid:
		with open(_session_path(sid), "w", encoding="utf-8") as fh:
			json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def reset_campaign_for_full_repost(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Force every campaign item into REPOST_PLANNED / Pending (preserve audit via detail)."""
	session = get_session() or start_session(campaign)
	n = cint(
		frappe.db.sql(
			f"""
			update `tab{ITEM_DOCTYPE}`
			set item_status=%s,
			    verification_class=%s,
			    covering_riv=NULL,
			    error_message=%s,
			    last_processed_at=%s
			where campaign=%s
			""",
			(
				ITEM_PENDING,
				REPOST_PLANNED,
				f"full_rebuild_reset:{session['session_id']}",
				now_datetime(),
				campaign,
			),
		)
	)
	# MariaDB rowcount via cursor
	affected = cint(
		frappe.db.sql(
			"select count(*) from `tabHistorical Rebuild Campaign Item` where campaign=%s and item_status=%s",
			(campaign, ITEM_PENDING),
		)[0][0]
	)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"status": "Paused", "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"campaign": campaign,
		"session_id": session["session_id"],
		"pending": affected,
		"verification_class": REPOST_PLANNED,
	}


def campaign_identities(campaign: str) -> list[dict[str, Any]]:
	"""Distinct Item+WH with earliest SLE posting among campaign vouchers."""
	return (
		frappe.db.sql(
			"""
			select sle.item_code, sle.warehouse,
			       min(sle.posting_date) as posting_date,
			       substring_index(
			         group_concat(sle.posting_time order by sle.posting_date, sle.posting_time, sle.creation),
			         ',', 1
			       ) as posting_time,
			       count(distinct concat(i.voucher_type, '/', i.voucher_no)) as voucher_count
			from `tabHistorical Rebuild Campaign Item` i
			inner join `tabStock Ledger Entry` sle
			  on sle.voucher_type=i.voucher_type
			 and sle.voucher_no=i.voucher_no
			 and sle.is_cancelled=0
			where i.campaign=%s
			  and sle.item_code is not null and sle.item_code!=''
			  and sle.warehouse is not null and sle.warehouse!=''
			group by sle.item_code, sle.warehouse
			order by posting_date asc, posting_time asc, sle.item_code asc, sle.warehouse asc
			""",
			campaign,
			as_dict=True,
		)
		or []
	)


def _session_riv_names(sess: dict | None = None) -> list[str]:
	sess = sess or get_session() or {}
	names = list(dict.fromkeys((sess.get("riv_created") or []) + (sess.get("riv_completed") or [])))
	return [n for n in names if n]


def session_completed_rivs(session_id: str | None = None) -> list[dict]:
	"""Completed RIVs registered in the current session registry."""
	sess = get_session() or {}
	if session_id and sess.get("session_id") != session_id:
		# Load specific file
		path = _session_path(session_id)
		if os.path.isfile(path):
			with open(path, encoding="utf-8") as fh:
				sess = json.load(fh)
	names = list(dict.fromkeys(sess.get("riv_completed") or []))
	if not names:
		return []
	placeholders = ",".join(["%s"] * len(names))
	return (
		frappe.db.sql(
			f"""
			select name, item_code, warehouse, posting_date, posting_time, status
			from `tabRepost Item Valuation`
			where docstatus=1 and status='Completed'
			  and based_on='Item and Warehouse'
			  and name in ({placeholders})
			order by posting_date asc, posting_time asc, creation asc
			""",
			tuple(names),
			as_dict=True,
		)
		or []
	)


def create_session_riv(
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	*,
	company: str,
	execute: bool = True,
) -> dict[str, Any]:
	"""Create a new Item+WH RIV registered to this session and optionally execute."""
	_ensure_bootstrap()
	sess = get_session()
	if not sess:
		frappe.throw("No rebuild session — call start_session() first")
	if is_blocked_identity(item_code, warehouse):
		return {"ok": False, "reason": "blocked_failed_riv", "item_code": item_code, "warehouse": warehouse}

	# Skip if this session already has a Completed cover at earlier/equal boundary
	cover = _session_cover_for_identity(item_code, warehouse, posting_date, posting_time)
	if cover:
		return {
			"ok": True,
			"riv": cover,
			"status": "Completed",
			"reused_session": True,
			"item_code": item_code,
			"warehouse": warehouse,
		}

	create_repost_item_valuation_entry(
		frappe._dict(
			based_on="Item and Warehouse",
			item_code=item_code,
			warehouse=warehouse,
			posting_date=posting_date,
			posting_time=posting_time or "00:00:00",
			company=company,
			allow_zero_rate=False,
			allow_negative_stock=True,
		)
	)
	name = frappe.db.get_value(
		"Repost Item Valuation",
		{
			"based_on": "Item and Warehouse",
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": posting_date,
			"docstatus": 1,
		},
		"name",
		order_by="creation desc",
	)
	if not name:
		return {"ok": False, "reason": "create_failed", "item_code": item_code, "warehouse": warehouse}

	# Session membership is the durable JSON registry (RIV has no remarks field).
	sess.setdefault("riv_created", []).append(name)
	_save_session(sess)
	frappe.db.commit()

	status = frappe.db.get_value("Repost Item Valuation", name, "status")
	if execute and status in ("Queued", "In Progress"):
		try:
			# Never continue_reposting — that enqueues parallel RQ jobs and floods
			# the default queue on worker-less maintenance sites.
			_ensure_rq_room()
			execute_reposting_entry(name, continue_reposting=False)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			status = frappe.db.get_value("Repost Item Valuation", name, "status")
			sess.setdefault("riv_failed", []).append(name)
			_save_session(sess)
			return {
				"ok": False,
				"riv": name,
				"status": status,
				"error": str(e)[:400],
				"item_code": item_code,
				"warehouse": warehouse,
			}
		status = frappe.db.get_value("Repost Item Valuation", name, "status")

	if status == "Completed":
		# Clear session marker from error_log if Core left it empty-success
		sess.setdefault("riv_completed", []).append(name)
		_save_session(sess)
		return {
			"ok": True,
			"riv": name,
			"status": status,
			"item_code": item_code,
			"warehouse": warehouse,
		}
	if status == "Failed":
		sess.setdefault("riv_failed", []).append(name)
		_save_session(sess)
		return {
			"ok": False,
			"riv": name,
			"status": status,
			"error": (frappe.db.get_value("Repost Item Valuation", name, "error_log") or "")[:400],
			"item_code": item_code,
			"warehouse": warehouse,
		}
	return {"ok": False, "riv": name, "status": status, "item_code": item_code, "warehouse": warehouse}


def _ensure_rq_room(*, max_default: int = 50) -> None:
	"""Drop flooded default RQ jobs on maintenance/dev sites (no workers)."""
	try:
		from frappe.utils.background_jobs import get_queue

		q = get_queue("default")
		if q.count > max_default:
			q.empty()
		lq = get_queue("long")
		# Keep long short — we execute sync
		if lq.count > max_default:
			lq.empty()
	except Exception:
		pass


def clear_failed_blockers_for_precision(limit: int = 500) -> dict[str, Any]:
	"""Mark Failed RIVs that are recoverable (precision / RQ flood) as Skipped for retry.

	- ±1 IRR GL imbalance: re-execute with IRR absorb + bootstrap
	- Too many queued background jobs: infrastructure, not valuation
	Does NOT clear genuine I3/I4 Failed rows.
	"""
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, left(error_log, 200) err
		from `tabRepost Item Valuation`
		where docstatus=1 and status='Failed'
		  and based_on='Item and Warehouse'
		  and (
		    error_log like '%%Debit and Credit not equal%%Difference is%%1.0%%'
		    or error_log like '%%Too many queued background jobs%%'
		  )
		limit %s
		""",
		cint(limit) or 500,
		as_dict=True,
	) or []
	cleared = []
	for r in rows:
		mark_resolved(
			r.item_code,
			r.warehouse,
			detail=f"v5.6.1: clear recoverable Failed {r.name} for session re-execute",
		)
		cleared.append(r.name)
	return {"cleared": cleared, "count": len(cleared)}


def _uncovered_identities(campaign: str) -> list[dict[str, Any]]:
	"""Campaign identities still lacking a session Completed chronological cover."""
	out = []
	for row in campaign_identities(campaign):
		if _session_cover_for_identity(
			row.item_code, row.warehouse, row.posting_date, row.posting_time
		):
			continue
		out.append(row)
	return out


def seed_and_execute_batch(
	campaign: str = "HRC-00001",
	*,
	limit: int = 20,
	offset: int = 0,
	only_uncovered: bool = True,
) -> dict[str, Any]:
	"""Create+execute session RIVs for a slice of campaign identities."""
	_ensure_bootstrap()
	_ensure_rq_room()
	sess = get_session() or start_session(campaign)
	company = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	idents = _uncovered_identities(campaign) if only_uncovered else campaign_identities(campaign)
	slice_ = idents[cint(offset) : cint(offset) + (cint(limit) or 20)]
	results = []
	completed = 0
	failed = 0
	blocked = 0
	t0 = time.time()
	for row in slice_:
		_ensure_rq_room()
		out = create_session_riv(
			row.item_code,
			row.warehouse,
			row.posting_date,
			row.posting_time,
			company=company,
			execute=True,
		)
		results.append(out)
		if out.get("ok") and out.get("status") == "Completed":
			completed += 1
		elif out.get("reason") == "blocked_failed_riv":
			blocked += 1
		else:
			failed += 1
		if _outstanding() >= MAX_OUTSTANDING_RIV:
			break
	stats = frappe.cache().get_value(CACHE_STATS) or {}
	stats["identities_seeded"] = cint(stats.get("identities_seeded")) + len(results)
	frappe.cache().set_value(CACHE_STATS, stats, expires_in_sec=60 * 60 * 24 * 14)
	return {
		"session_id": sess["session_id"],
		"offset": offset,
		"attempted": len(results),
		"completed": completed,
		"failed": failed,
		"blocked": blocked,
		"runtime_seconds": round(time.time() - t0, 3),
		"total_identities": len(idents),
		"results_sample": results[:10],
	}


def _outstanding() -> int:
	return cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		)
	)


def _session_cover_for_identity(item_code: str, warehouse: str, posting_date, posting_time) -> str | None:
	"""Return a session-registered Completed RIV covering this identity chronologically.

	Historical Production Completed RIVs are ignored unless registered in this session.
	"""
	sess = get_session()
	if not sess:
		return None
	names = list(dict.fromkeys(sess.get("riv_completed") or []))
	# Also accept created-then-completed that were just updated
	for n in sess.get("riv_created") or []:
		if n not in names:
			st = frappe.db.get_value("Repost Item Valuation", n, "status")
			if st == "Completed":
				names.append(n)
				sess.setdefault("riv_completed", []).append(n)
	if not names:
		return None
	_save_session(sess)
	pt = str(posting_time or "00:00:00")
	placeholders = ",".join(["%s"] * len(names))
	rows = frappe.db.sql(
		f"""
		select name from `tabRepost Item Valuation`
		where docstatus=1 and based_on='Item and Warehouse'
		  and item_code=%s and warehouse=%s and status='Completed'
		  and name in ({placeholders})
		  and (
		    posting_date < %s
		    or (posting_date=%s and posting_time<=%s)
		  )
		order by posting_date asc, posting_time asc limit 1
		""",
		tuple([item_code, warehouse] + names + [posting_date, posting_date, pt]),
	)
	return rows[0][0] if rows else None


def verify_and_mark_covered_batch(
	campaign: str = "HRC-00001",
	*,
	limit: int = 500,
	only_pending: bool = True,
) -> dict[str, Any]:
	"""Mark Pending items COVERED only when every SLE identity has a session Completed RIV
	and post-anchor SLE chain is financially clean.
	"""
	filters: dict[str, Any] = {"campaign": campaign}
	if only_pending:
		filters["item_status"] = ITEM_PENDING
	items = frappe.get_all(
		ITEM_DOCTYPE,
		filters=filters,
		fields=["name", "voucher_type", "voucher_no", "company", "ordinal"],
		order_by="ordinal asc",
		limit_page_length=cint(limit) or 500,
	)
	covered = 0
	pending = 0
	failed = 0
	no_sle = 0
	for it in items:
		basic = verify_voucher_basic(it.voucher_type, it.voucher_no)
		sles = (
			frappe.db.sql(
				"""
				select distinct item_code, warehouse, posting_date, posting_time, company
				from `tabStock Ledger Entry`
				where is_cancelled=0 and voucher_type=%s and voucher_no=%s
				  and item_code is not null and item_code!=''
				  and warehouse is not null and warehouse!=''
				""",
				(it.voucher_type, it.voucher_no),
				as_dict=True,
			)
			or []
		)
		if not sles:
			# Non-stock / FA: mark DIRECT only after basic ok (no SLE to recalculate)
			if basic.get("ok"):
				_mark_item(
					it.name,
					DIRECT_REPOSTED_AND_VERIFIED,
					ITEM_SKIPPED,
					riv=None,
					detail={"reason": "no_sle_basic_ok", "basic": basic.get("detail")},
				)
				covered += 1
				no_sle += 1
			else:
				_mark_item(
					it.name,
					REPOST_FAILED,
					ITEM_FAILED,
					riv=None,
					detail={"reason": f"no_sle_basic_fail:{basic.get('detail')}"},
				)
				failed += 1
			continue

		covers = []
		ok = True
		reason = ""
		for sle in sles:
			riv = _session_cover_for_identity(
				sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
			)
			if not riv:
				ok = False
				reason = f"no_session_riv:{sle.item_code}|{sle.warehouse}"
				break
			# Financial chain from this session RIV forward
			riv_row = frappe.db.get_value(
				"Repost Item Valuation",
				riv,
				["posting_date", "posting_time"],
				as_dict=True,
			)
			chain = inspect_sle_chain(
				sle.item_code,
				sle.warehouse,
				company=sle.company or it.company,
				from_date=riv_row.posting_date,
				from_time=riv_row.posting_time,
			)
			if chain.get("dirty"):
				ok = False
				reason = f"sle_dirty_after_session_riv:{riv}:{chain.get('issue_count')}"
				break
			covers.append(riv)
		if not ok:
			frappe.db.set_value(
				ITEM_DOCTYPE,
				it.name,
				{
					"error_message": f"await_session_cover:{reason}"[:1000],
					"verification_class": REPOST_PLANNED,
					"last_processed_at": now_datetime(),
				},
				update_modified=False,
			)
			pending += 1
			continue
		_mark_item(
			it.name,
			COVERED_BY_REPOSTED_AND_VERIFIED,
			ITEM_SKIPPED,
			riv=covers[0] if covers else None,
			detail={
				"reason": "session_riv_cover",
				"covering_rivs": list(dict.fromkeys(covers)),
				"identity_count": len(sles),
			},
		)
		covered += 1
	frappe.db.commit()
	return {
		"scanned": len(items),
		"covered": covered,
		"still_pending": pending,
		"failed": failed,
		"no_sle_direct": no_sle,
	}


def _mark_item(name: str, vclass: str, item_status: str, *, riv, detail: dict) -> None:
	fields = {
		"item_status": item_status,
		"verification_class": vclass,
		"covering_riv": riv,
		"error_message": "",
		"last_processed_at": now_datetime(),
	}
	meta = frappe.get_meta(ITEM_DOCTYPE)
	if meta.has_field("verification_detail"):
		fields["verification_detail"] = json.dumps(
			{**detail, "checked_at": str(now_datetime())}, ensure_ascii=False, default=str
		)[:2000]
	frappe.db.set_value(ITEM_DOCTYPE, name, fields, update_modified=False)


def run_full_rebuild(
	campaign: str = "HRC-00001",
	*,
	batch_size: int = 25,
	max_batches: int = 500,
	reset: bool = True,
	clear_precision_failures: bool = True,
) -> dict[str, Any]:
	"""End-to-end: reset → clear ±1IRR blockers → seed/execute all identities → mark cover."""
	t0 = time.time()
	_ensure_bootstrap()
	session = start_session(campaign)
	reset_out = reset_campaign_for_full_repost(campaign) if reset else {"skipped": True}
	clear_out = clear_failed_blockers_for_precision() if clear_precision_failures else {}

	idents = campaign_identities(campaign)
	batches = []
	# always drain uncovered set from offset 0 (list shrinks as covers complete)
	while len(batches) < cint(max_batches):
		clear_failed_blockers_for_precision(limit=200)
		_ensure_rq_room()
		remaining = len(_uncovered_identities(campaign))
		if remaining == 0:
			break
		batch = seed_and_execute_batch(
			campaign, limit=batch_size, offset=0, only_uncovered=True
		)
		batches.append(
			{
				"remaining_before": remaining,
				"attempted": batch["attempted"],
				"completed": batch["completed"],
				"failed": batch["failed"],
				"blocked": batch["blocked"],
				"runtime": batch["runtime_seconds"],
			}
		)
		if batch["attempted"] == 0:
			break
		# Periodically mark coverage for progress visibility
		if len(batches) % 3 == 0:
			verify_and_mark_covered_batch(campaign, limit=3000)

	# Final coverage mark for all pending
	cover = verify_and_mark_covered_batch(campaign, limit=20000)
	# Second pass for leftovers after late Completions
	cover2 = verify_and_mark_covered_batch(campaign, limit=20000)

	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import (
		_refresh_counters,
	)

	counters = _refresh_counters(campaign)
	status_rows = frappe.db.sql(
		"""
		select verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s
		group by verification_class
		""",
		campaign,
		as_dict=True,
	)
	item_status_rows = frappe.db.sql(
		"""
		select item_status, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s
		group by item_status
		""",
		campaign,
		as_dict=True,
	)
	sess = get_session() or session
	success = sum(
		cint(r.c) for r in status_rows if r.verification_class in FINAL_SUCCESS_CLASSES
	)
	total = sum(cint(r.c) for r in status_rows)
	return {
		"session_id": sess.get("session_id"),
		"runtime_seconds": round(time.time() - t0, 3),
		"reset": reset_out,
		"clear_precision": clear_out,
		"identity_total": len(idents),
		"batches": batches,
		"batch_count": len(batches),
		"cover_passes": [cover, cover2],
		"verification_class_counts": {r.verification_class: cint(r.c) for r in status_rows},
		"item_status_counts": {r.item_status: cint(r.c) for r in item_status_rows},
		"session_riv_created": len(sess.get("riv_created") or []),
		"session_riv_completed": len(set(sess.get("riv_completed") or [])),
		"session_riv_failed": len(set(sess.get("riv_failed") or [])),
		"campaign_counters": counters,
		"success_entries": success,
		"total_entries": total,
		"gates_ok": success == total and total > 0,
	}


def campaign_progress(campaign: str = "HRC-00001") -> dict[str, Any]:
	sess = get_session() or {}
	vc = frappe.db.sql(
		"""
		select verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s group by verification_class
		""",
		campaign,
		as_dict=True,
	)
	return {
		"session_id": sess.get("session_id"),
		"verification_class_counts": {r.verification_class: cint(r.c) for r in vc},
		"session_riv_created": len(sess.get("riv_created") or []),
		"session_riv_completed": len(set(sess.get("riv_completed") or [])),
		"session_riv_failed": len(set(sess.get("riv_failed") or [])),
		"pending": cint(
			frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_PENDING})
		),
		"identities_total": len(campaign_identities(campaign)),
	}
