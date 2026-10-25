# Copyright (c) 2026, ERPNext Extensions contributors
"""Single-RIV chronological historical rebuild (v5.6.1 strategy change).

Rules:
- MAX_ACTIVE_RIV = 1
- No mass RIV pre-creation
- No uncontrolled RQ enqueue (continue_reposting=False + empty queues)
- Coverage only after Completed session RIV + SLE continuity verification
- Stop on I3/I4 / genuine valuation failures for that identity (fix then retry)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_SKIPPED,
	STATUS_COMPLETED,
	STATUS_FAILED,
	STATUS_PAUSED,
	STATUS_RUNNING,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	inspect_sle_chain,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	COVERED_BY_REPOSTED_AND_VERIFIED,
	DIRECT_REPOSTED_AND_VERIFIED,
	FINAL_SUCCESS_CLASSES,
	REPOST_FAILED,
	REPOST_PLANNED,
	SESSION_RIV_REMARK_PREFIX,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	verify_voucher_basic,
)

MAX_ACTIVE_RIV = 1
CACHE_SEQ = "hrc561_sequential_session"
CACHE_SEQ_LOCK = "hrc561_sequential_executor_lock"
STOP_FLAG_NAME = "STOP_SEQUENTIAL_REBUILD"
SEQ_REMARK_PREFIX = "SEQ_SESSION="


def _stop_flag_path() -> str:
	return os.path.join(_session_dir(), STOP_FLAG_NAME)


def request_sequential_stop(*, reason: str = "operator") -> dict[str, Any]:
	"""Ask run_sequential to halt after the current RIV reaches a terminal state."""
	path = _stop_flag_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump({"reason": reason, "at": str(now_datetime())}, fh)
	return {"ok": True, "path": path, "reason": reason}


def clear_sequential_stop() -> None:
	path = _stop_flag_path()
	if os.path.exists(path):
		os.remove(path)


def _stop_requested() -> bool:
	return os.path.exists(_stop_flag_path())


def _ensure_bootstrap() -> None:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()


def _session_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"hrc_sequential_rebuild",
	)
	os.makedirs(path, exist_ok=True)
	return path


def _empty_rq() -> None:
	try:
		from frappe.utils.background_jobs import get_queue

		for name in ("default", "long", "short"):
			try:
				get_queue(name).empty()
			except Exception:
				pass
	except Exception:
		pass


def delete_all_rivs(*, reason: str = "sequential_rebuild_reset") -> dict[str, Any]:
	"""Delete every Repost Item Valuation row on this DEVELOPMENT site.

	Authorized cleanup only. Does not touch SLE/GL/business vouchers.
	"""
	_assert_dev()
	before = cint(frappe.db.count("Repost Item Valuation"))
	# Detach File attachments pointing at RIV
	frappe.db.sql(
		"""
		delete from `tabFile`
		where attached_to_doctype='Repost Item Valuation'
		"""
	)
	# Direct delete of RIV table (no business vouchers)
	frappe.db.sql("delete from `tabRepost Item Valuation`")
	frappe.db.commit()
	after = cint(frappe.db.count("Repost Item Valuation"))
	audit = {
		"reason": reason,
		"before": before,
		"after": after,
		"at": str(now_datetime()),
		"site": frappe.local.site,
	}
	path = os.path.join(_session_dir(), f"riv_delete_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(audit, fh, indent=2)
	if after != 0:
		frappe.throw(f"RIV cleanup incomplete: remaining={after}")
	return {"ok": True, "deleted": before, "remaining": after, "audit_path": path}


def _assert_dev() -> None:
	site = frappe.local.site or ""
	if "espadpharmed.com" in site and "development" not in site:
		frappe.throw(f"Refused RIV mass-delete on production-like site: {site}")
	if not (
		frappe.conf.get("developer_mode")
		or frappe.conf.get("e2e_marker")
		or frappe.conf.get("restored_from_backup")
	):
		frappe.throw("Refused: site not marked as development/test")


def reset_campaign_planned(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Force all campaign items to Pending / REPOST_PLANNED; clear covering_riv."""
	# Preserve prior manifest status snapshot
	snap = frappe.db.sql(
		"""
		select item_status, verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s
		group by item_status, verification_class
		""",
		campaign,
		as_dict=True,
	)
	path = os.path.join(
		_session_dir(), f"manifest_before_reset_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json"
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump({"campaign": campaign, "counts": snap}, fh, indent=2, default=str)

	frappe.db.sql(
		"""
		update `tabHistorical Rebuild Campaign Item`
		set item_status=%s,
		    verification_class=%s,
		    covering_riv=NULL,
		    riv_names=NULL,
		    error_message=%s,
		    last_processed_at=%s
		where campaign=%s
		""",
		(
			ITEM_PENDING,
			REPOST_PLANNED,
			"sequential_rebuild_reset",
			now_datetime(),
			campaign,
		),
	)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"status": "Paused", "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	total = cint(frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign}))
	pending = cint(
		frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_PENDING})
	)
	return {"campaign": campaign, "total": total, "pending": pending, "snapshot": path}


def _campaign_remarks_with_session(campaign: str, session_id: str) -> str:
	"""Keep SEQ_SESSION=… discoverable on the campaign row without a migration."""
	existing = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "remarks") or ""
	lines = [
		ln
		for ln in str(existing).splitlines()
		if ln.strip() and not ln.strip().startswith(SEQ_REMARK_PREFIX)
	]
	lines.insert(0, f"{SEQ_REMARK_PREFIX}{session_id}")
	return "\n".join(lines)[:1000]


def parse_campaign_session_id(campaign: str) -> str | None:
	remarks = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "remarks") or ""
	for ln in str(remarks).splitlines():
		s = ln.strip()
		if s.startswith(SEQ_REMARK_PREFIX):
			return s[len(SEQ_REMARK_PREFIX) :].strip() or None
	return None


def sync_campaign_execution_state(
	campaign: str,
	*,
	status: str | None = None,
	session_id: str | None = None,
	current_riv: str | None = None,
	current_item: str | None = None,
	current_warehouse: str | None = None,
	failure: dict | None = None,
	touch_started: bool = False,
) -> None:
	"""Keep HRC-00001 aligned with the active sequential execution session."""
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		return
	fields: dict[str, Any] = {"last_activity": now_datetime()}
	if status:
		fields["status"] = status
	if touch_started:
		started = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "started_at")
		if not started:
			fields["started_at"] = now_datetime()
	if session_id:
		fields["remarks"] = _campaign_remarks_with_session(campaign, session_id)
	if current_riv is not None:
		fields["current_voucher_type"] = "Repost Item Valuation" if current_riv else None
		fields["current_voucher_no"] = current_riv
	if current_item or current_warehouse:
		fields["last_voucher_type"] = current_item or ""
		fields["last_voucher_no"] = current_warehouse or ""
	if status == STATUS_COMPLETED:
		fields["completed_at"] = now_datetime()
		fields["current_voucher_type"] = None
		fields["current_voucher_no"] = None
	frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, fields, update_modified=True)
	if failure:
		try:
			from erpnext_extensions.iran_accounting.historical_rebuild.campaign import (
				_append_log,
			)

			_append_log(
				campaign,
				(
					f"sequential fail-fast riv={failure.get('riv')} "
					f"item={failure.get('item_code')} wh={failure.get('warehouse')} "
					f"err={(str(failure.get('error') or ''))[:240]}"
				),
				level="ERROR",
				voucher_type="Repost Item Valuation",
				voucher_no=failure.get("riv"),
			)
		except Exception:
			pass


def _executor_lock_key(campaign: str) -> str:
	return f"{CACHE_SEQ_LOCK}:{campaign}"


def _acquire_executor_lock(campaign: str, session_id: str) -> None:
	"""Only one sequential executor may operate on a campaign at a time."""
	key = _executor_lock_key(campaign)
	held = frappe.cache().get_value(key)
	if held and held != session_id:
		frappe.throw(
			f"Sequential executor already active for {campaign} "
			f"(session={held}); refuse duplicate start"
		)
	frappe.cache().set_value(key, session_id, expires_in_sec=60 * 60 * 6)


def _release_executor_lock(campaign: str, session_id: str) -> None:
	key = _executor_lock_key(campaign)
	held = frappe.cache().get_value(key)
	if held == session_id:
		frappe.cache().delete_value(key)


def _executor_lock_owned(campaign: str, session_id: str) -> bool:
	held = frappe.cache().get_value(_executor_lock_key(campaign))
	return bool(held == session_id)


def _refresh_executor_lock(campaign: str, session_id: str) -> None:
	"""Heartbeat: extend TTL only if we still own the lock."""
	key = _executor_lock_key(campaign)
	held = frappe.cache().get_value(key)
	if held == session_id:
		frappe.cache().set_value(key, session_id, expires_in_sec=60 * 60 * 6)


def start_sequential_session(campaign: str = "HRC-00001") -> dict[str, Any]:
	sid = now_datetime().strftime("%Y%m%d%H%M%S")
	payload = {
		"session_id": sid,
		"campaign": campaign,
		"remark": f"{SESSION_RIV_REMARK_PREFIX}SEQ:{sid}",
		"started_at": str(now_datetime()),
		"status": "Ready",
		"riv_created": [],
		"riv_completed": [],
		"riv_failed": [],
		"identities_done": [],
		"max_active_riv": MAX_ACTIVE_RIV,
		"strategy": "sequential_chronological",
		"last_failure": None,
	}
	_save_seq(payload)
	sync_campaign_execution_state(
		campaign,
		status=STATUS_PAUSED,
		session_id=sid,
		touch_started=True,
	)
	frappe.db.commit()
	return payload


def _save_seq(payload: dict) -> None:
	"""Persist session JSON first, then Redis — JSON is source of truth on restart.

	Increments ``revision`` so a stale Redis cache cannot overwrite newer disk state.
	"""
	data = dict(payload)
	data["revision"] = int(data.get("revision") or 0) + 1
	data["persisted_at"] = str(now_datetime())
	path = os.path.join(_session_dir(), f"session_{data['session_id']}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
	frappe.cache().set_value(CACHE_SEQ, data, expires_in_sec=60 * 60 * 24 * 14)
	# Keep caller's dict in sync with persisted revision.
	payload.clear()
	payload.update(data)


def get_seq_session() -> dict | None:
	"""Return session preferring newer JSON revision over stale Redis cache."""
	cached = frappe.cache().get_value(CACHE_SEQ)
	if not cached:
		return None
	sid = cached.get("session_id")
	if not sid:
		return cached
	path = os.path.join(_session_dir(), f"session_{sid}.json")
	if not os.path.exists(path):
		return cached
	try:
		with open(path, encoding="utf-8") as fh:
			disk = json.load(fh)
	except Exception:
		return cached
	disk_rev = int(disk.get("revision") or 0)
	cache_rev = int(cached.get("revision") or 0)
	if disk_rev > cache_rev:
		frappe.cache().set_value(CACHE_SEQ, disk, expires_in_sec=60 * 60 * 24 * 14)
		return disk
	return cached


def restore_session_from_file(session_id: str | None = None) -> dict[str, Any]:
	"""Reload sequential session JSON into cache (survives process restarts)."""
	directory = _session_dir()
	path = None
	if session_id:
		path = os.path.join(directory, f"session_{session_id}.json")
	else:
		candidates = sorted(
			(
				os.path.join(directory, name)
				for name in os.listdir(directory)
				if name.startswith("session_") and name.endswith(".json")
			),
			key=os.path.getmtime,
			reverse=True,
		)
		path = candidates[0] if candidates else None
	if not path or not os.path.exists(path):
		frappe.throw(f"No sequential session file found (session_id={session_id})")
	with open(path, encoding="utf-8") as fh:
		payload = json.load(fh)
	_save_seq(payload)
	return {"ok": True, "session_id": payload.get("session_id"), "path": path, "payload": payload}


def chronological_identities(campaign: str) -> list[dict[str, Any]]:
	"""Distinct Item+WH ordered by earliest campaign SLE posting.

	Scoped campaigns restrict to the persisted identity allowlist so shared
	vouchers do not pull unrelated Item/Warehouse RIVs into the session.
	"""
	rows = (
		frappe.db.sql(
			"""
			select sle.item_code, sle.warehouse,
			       min(sle.posting_date) as posting_date,
			       substring_index(
			         group_concat(sle.posting_time order by sle.posting_date, sle.posting_time, sle.creation),
			         ',', 1
			       ) as posting_time,
			       count(distinct concat(i.voucher_type,'/',i.voucher_no)) as voucher_count
			from `tabHistorical Rebuild Campaign Item` i
			inner join `tabStock Ledger Entry` sle
			  on sle.voucher_type=i.voucher_type and sle.voucher_no=i.voucher_no
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
	try:
		from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
			load_scoped_allowlist,
		)

		allow = load_scoped_allowlist(campaign)
	except Exception:
		allow = None
	if allow:
		allowed = set(allow)
		rows = [r for r in rows if (r.item_code, r.warehouse) in allowed]
	return rows


def _wait_until_riv_queue_clear(
	*,
	campaign: str | None = None,
	sess: dict | None = None,
) -> dict[str, Any]:
	"""WAIT → RECHECK → CONTINUE for site-wide Queued/In Progress RIVs (v5.6.14).

	Replaces the old hard fail of ``_assert_no_active_riv``. Does not create RIVs
	and does not mark Campaign Failed while waiting.
	"""
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait import (
		wait_for_riv_queue_drain,
	)

	session = sess or get_seq_session()
	sid = (session or {}).get("session_id")
	camp = campaign or (session or {}).get("campaign")

	def _lock_ok() -> bool:
		if not camp or not sid:
			return True
		return _executor_lock_owned(camp, sid)

	def _heartbeat() -> None:
		if camp and sid:
			_refresh_executor_lock(camp, sid)

	# Idle DB before sleep loops.
	try:
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()

	return wait_for_riv_queue_drain(
		sess=session,
		save_session=_save_seq if session else None,
		stop_check=_stop_requested,
		lock_ok=_lock_ok if camp and sid else None,
		heartbeat=_heartbeat if camp and sid else None,
		campaign=camp,
	)


def execute_one_identity(
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	*,
	company: str,
	campaign: str | None = None,
) -> dict[str, Any]:
	"""Create exactly one RIV, execute sync, verify chain — terminal before return."""
	_ensure_bootstrap()
	_empty_rq()
	sess = get_seq_session()
	if not sess:
		frappe.throw("No sequential session — call start_sequential_session()")

	# Skip if already done in this session
	key = f"{item_code}|{warehouse}"
	if key in (sess.get("identities_done") or []):
		return {"ok": True, "skipped_done": True, "item_code": item_code, "warehouse": warehouse}

	# v5.6.14: wait for site-wide RIV queue (do not fail-start on temporary occupancy).
	qwait = _wait_until_riv_queue_clear(campaign=campaign or sess.get("campaign"), sess=sess)
	if qwait.get("stopped") or qwait.get("lock_lost"):
		return {
			"ok": False,
			"stopped": bool(qwait.get("stopped")),
			"lock_lost": bool(qwait.get("lock_lost")),
			"queue_wait": qwait,
			"item_code": item_code,
			"warehouse": warehouse,
			"error": "stopped_during_riv_queue_wait"
			if qwait.get("stopped")
			else "executor_lock_lost_during_riv_queue_wait",
		}

	# Recheck immediately before create (close check→create race under our lock).
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_queue_wait import (
		count_active_rivs,
	)

	if count_active_rivs() > 0:
		qwait2 = _wait_until_riv_queue_clear(campaign=campaign or sess.get("campaign"), sess=sess)
		if qwait2.get("stopped") or qwait2.get("lock_lost") or count_active_rivs() > 0:
			return {
				"ok": False,
				"stopped": bool(qwait2.get("stopped")),
				"lock_lost": bool(qwait2.get("lock_lost")),
				"queue_wait": qwait2,
				"item_code": item_code,
				"warehouse": warehouse,
				"error": "riv_queue_not_drained_before_create",
				"stop_chain": not bool(qwait2.get("stopped") or qwait2.get("lock_lost")),
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

	sess.setdefault("riv_created", []).append(name)
	_save_seq(sess)
	frappe.db.commit()

	# v5.6.12: resilient transient retry (1020/1205/1213) + Completed reconciliation.
	# Waits occur outside DB transactions. Production HRC-00004 race: Core raised
	# QueryDeadlockError(1020) on progress db_set while the same RIV later became
	# Completed — executor must reconcile, not permanently Fail the campaign.
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		execute_reposting_entry as _exec_riv,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry import (
		run_resilient_riv_execution,
	)

	def _execute_fn(riv: str) -> None:
		_empty_rq()
		_exec_riv(riv, continue_reposting=False)

	# Refresh session from cache/JSON in case another path updated revision.
	sess = get_seq_session() or sess
	exec_out = run_resilient_riv_execution(
		riv_name=name,
		item_code=item_code,
		warehouse=warehouse,
		posting_date=posting_date,
		posting_time=posting_time,
		company=company,
		sess=sess,
		execute_fn=_execute_fn,
		verify_chain=inspect_sle_chain,
		stop_check=_stop_requested,
		save_session=_save_seq,
	)
	duration = exec_out.get("duration")
	if exec_out.get("ok"):
		_save_seq(sess)
		return {
			"ok": True,
			"riv": name,
			"status": "Completed",
			"item_code": item_code,
			"warehouse": warehouse,
			"duration": duration,
			"sle_count": exec_out.get("sle_count"),
			"reconciled": bool(exec_out.get("reconciled")),
			"reconciled_after_transient": bool(exec_out.get("reconciled_after_transient")),
			"attempt": exec_out.get("attempt"),
		}

	# Terminal failure / stop — preserve Failed RIV for audit; do not delete.
	status = frappe.db.get_value("Repost Item Valuation", name, "status")
	if exec_out.get("verify_failed") and status == "Completed":
		# Case 4: Completed but invalid — mark RIV Failed for audit alignment.
		issues = exec_out.get("issues_sample") or []
		try:
			frappe.db.set_value(
				"Repost Item Valuation",
				name,
				{
					"status": "Failed",
					"error_log": json.dumps(issues, ensure_ascii=False, default=str)[:4000],
				},
				update_modified=True,
			)
			frappe.db.commit()
			status = "Failed"
		except Exception:
			frappe.db.rollback()

	if name not in (sess.get("riv_failed") or []):
		sess.setdefault("riv_failed", []).append(name)
	_save_seq(sess)
	return {
		"ok": False,
		"riv": name,
		"status": status,
		"error": exec_out.get("error")
		or (str(exec_out.get("exc"))[:500] if exec_out.get("exc") else None),
		"item_code": item_code,
		"warehouse": warehouse,
		"duration": duration,
		"stop_chain": bool(exec_out.get("stop_chain") or not exec_out.get("stopped")),
		"stopped": bool(exec_out.get("stopped")),
		"non_retryable": bool(exec_out.get("non_retryable")),
		"attempts_exhausted": bool(exec_out.get("attempts_exhausted")),
		"verify_failed": bool(exec_out.get("verify_failed")),
		"issues_sample": exec_out.get("issues_sample"),
		"retry_audit": exec_out.get("retry_audit"),
		"db_retry": exec_out.get("retry_reason"),
	}


def normalize_posting_time(value) -> str:
	"""Normalize posting_time for chronological comparison.

	MariaDB / Frappe may return unpadded times like ``7:54:56`` while SLE
	rows often store ``17:17:48``. Lexicographic compare then falsely treats
	morning RIV anchors as *after* afternoon SLEs (``'7…' > '1…'``), leaving
	manifest entries Pending forever (HRC-00003: 57 stuck vouchers).
	"""
	if value is None or value == "":
		return "00:00:00"
	# datetime.timedelta from MySQL TIME columns
	if hasattr(value, "total_seconds"):
		secs = int(value.total_seconds()) % (24 * 3600)
		h, rem = divmod(secs, 3600)
		m, s = divmod(rem, 60)
		return f"{h:02d}:{m:02d}:{s:02d}"
	text = str(value).strip()
	# Drop fractional seconds for stable compare
	if "." in text:
		text = text.split(".", 1)[0]
	parts = text.split(":")
	if len(parts) == 2:
		parts.append("00")
	if len(parts) != 3:
		return "00:00:00"
	try:
		h, m, s = (int(p) for p in parts)
	except ValueError:
		return "00:00:00"
	return f"{h:02d}:{m:02d}:{s:02d}"


def posting_sort_key(posting_date, posting_time) -> tuple[str, str]:
	"""Comparable (date, time) key — zero-padded time, ISO date string."""
	return (str(posting_date), normalize_posting_time(posting_time))


def mark_campaign_coverage(campaign: str, *, limit: int = 5000) -> dict[str, Any]:
	"""Mark Pending items COVERED when every SLE identity is in identities_done
	with a session Completed RIV at/before posting.
	"""
	sess = get_seq_session() or {}
	done = set(sess.get("identities_done") or [])
	completed = list(dict.fromkeys(sess.get("riv_completed") or []))
	if not completed and not done:
		return {"covered": 0, "pending": 0, "detail": "no session progress"}

	# Index earliest session Completed RIV per identity
	index: dict[tuple[str, str], tuple[str, str, str]] = {}
	if completed:
		ph = ",".join(["%s"] * len(completed))
		rows = frappe.db.sql(
			f"""
			select name, item_code, warehouse, posting_date, posting_time
			from `tabRepost Item Valuation`
			where name in ({ph}) and status='Completed' and docstatus=1
			order by posting_date asc, posting_time asc
			""",
			tuple(completed),
			as_dict=True,
		) or []
		for r in rows:
			key = (r.item_code, r.warehouse)
			if key not in index:
				index[key] = (
					r.name,
					str(r.posting_date),
					normalize_posting_time(r.posting_time),
				)

	# Scoped campaigns: only require coverage for allowlisted identities.
	# Unrelated Item/WH lines sharing a dependency voucher are ignored for
	# COVERED decisions (scoped test does not claim full-company verification).
	try:
		from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
			load_scoped_allowlist,
		)

		allow = load_scoped_allowlist(campaign)
	except Exception:
		allow = None
	allowed = set(allow) if allow else None

	items = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign, "item_status": ITEM_PENDING},
		fields=["name", "voucher_type", "voucher_no", "company"],
		order_by="ordinal asc",
		limit_page_length=cint(limit) or 5000,
	)
	covered = 0
	still = 0
	direct = 0
	for it in items:
		sles = (
			frappe.db.sql(
				"""
				select distinct item_code, warehouse, posting_date, posting_time
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
		if allowed is not None:
			sles = [s for s in sles if (s.item_code, s.warehouse) in allowed]
		if not sles:
			basic = verify_voucher_basic(it.voucher_type, it.voucher_no)
			if basic.get("ok"):
				_mark(
					it.name,
					DIRECT_REPOSTED_AND_VERIFIED,
					None,
					{
						"reason": "no_sle"
						if allowed is None
						else "no_allowlisted_sle",
						"scoped": bool(allowed is not None),
					},
				)
				covered += 1
				direct += 1
			else:
				still += 1
			continue
		ok = True
		covers = []
		for sle in sles:
			info = index.get((sle.item_code, sle.warehouse))
			if not info:
				ok = False
				break
			# chronological: RIV posting <= SLE posting (normalized times)
			if posting_sort_key(info[1], info[2]) > posting_sort_key(
				sle.posting_date, sle.posting_time
			):
				ok = False
				break
			covers.append(info[0])
		if not ok:
			still += 1
			continue
		# Refuse false COVERED when Manufacture still has zero MAIN_PRODUCT_REJECT.
		basic = verify_voucher_basic(it.voucher_type, it.voucher_no)
		if not basic.get("ok"):
			still += 1
			continue
		_mark(
			it.name,
			COVERED_BY_REPOSTED_AND_VERIFIED,
			covers[0],
			{
				"covering_rivs": list(dict.fromkeys(covers)),
				"reason": "sequential_session_cover",
				"basic": basic.get("detail"),
				"scoped": bool(allowed is not None),
			},
		)
		covered += 1
	frappe.db.commit()
	return {"covered": covered, "still_pending": still, "direct_no_sle": direct, "scanned": len(items)}


def finalize_sequential_campaign(
	campaign: str, *, session_id: str | None = None
) -> dict[str, Any]:
	"""Re-mark coverage with corrected chronology and complete when all verified.

	Safe after a session that finished every identity but left Pending items due
	to the unpadded posting_time compare bug. Does not create RIVs.
	"""
	sess = get_seq_session()
	if not sess or sess.get("campaign") != campaign:
		# Cache may have expired after a long sequential run — reload from disk.
		restored = restore_session_from_file(session_id)
		sess = restored.get("payload") or get_seq_session()
	if not sess or sess.get("campaign") != campaign:
		frappe.throw(f"No active sequential session for campaign {campaign}")
	if sess.get("riv_failed"):
		frappe.throw("Cannot finalize while session has failed RIVs")

	cover = mark_campaign_coverage(campaign, limit=50000)
	vc = frappe.db.sql(
		"""
		select verification_class, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s group by verification_class
		""",
		campaign,
		as_dict=True,
	)
	success = sum(cint(r.c) for r in vc if r.verification_class in FINAL_SUCCESS_CLASSES)
	total = sum(cint(r.c) for r in vc)
	pending = cint(
		frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_PENDING})
	)
	active_riv = cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		)
	)
	out: dict[str, Any] = {
		"campaign": campaign,
		"session_id": sess.get("session_id"),
		"cover": cover,
		"success_entries": success,
		"total_entries": total,
		"pending": pending,
		"active_riv": active_riv,
		"verification_class_counts": {r.verification_class: cint(r.c) for r in vc},
	}
	if pending == 0 and success == total and total > 0 and active_riv == 0:
		sess["status"] = "Completed"
		_save_seq(sess)
		sync_campaign_execution_state(
			campaign, status=STATUS_COMPLETED, session_id=sess.get("session_id")
		)
		frappe.db.commit()
		out["campaign_status"] = STATUS_COMPLETED
		out["session_status"] = "Completed"
		out["finalized"] = True
	else:
		out["campaign_status"] = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status")
		out["session_status"] = sess.get("status")
		out["finalized"] = False
	return out


def _mark(name: str, vclass: str, riv: str | None, detail: dict) -> None:
	fields = {
		"item_status": ITEM_SKIPPED if vclass in FINAL_SUCCESS_CLASSES else ITEM_PENDING,
		"verification_class": vclass,
		"covering_riv": riv,
		"error_message": "",
		"last_processed_at": now_datetime(),
	}
	meta = frappe.get_meta(ITEM_DOCTYPE)
	if meta.has_field("verification_detail"):
		fields["verification_detail"] = json.dumps(
			{**detail, "at": str(now_datetime())}, ensure_ascii=False, default=str
		)[:2000]
	if vclass == REPOST_FAILED:
		fields["item_status"] = ITEM_FAILED
	frappe.db.set_value(ITEM_DOCTYPE, name, fields, update_modified=False)


def run_sequential(
	campaign: str = "HRC-00001",
	*,
	max_identities: int | None = None,
	stop_on_failure: bool = True,
	mark_every: int = 10,
) -> dict[str, Any]:
	"""Process identities one-by-one chronologically until done or blocked."""
	t0 = time.time()
	_ensure_bootstrap()
	_empty_rq()
	sess = get_seq_session() or start_sequential_session(campaign)
	session_id = sess.get("session_id")
	_acquire_executor_lock(campaign, session_id)
	company = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	idents = chronological_identities(campaign)
	done_keys = set(sess.get("identities_done") or [])
	todo = [r for r in idents if f"{r.item_code}|{r.warehouse}" not in done_keys]
	if max_identities is not None:
		todo = todo[: cint(max_identities)]

	clear_sequential_stop()
	sess["status"] = "Running"
	_save_seq(sess)
	sync_campaign_execution_state(
		campaign,
		status=STATUS_RUNNING,
		session_id=session_id,
		touch_started=True,
	)
	frappe.db.commit()

	# Drain any site-wide Queued/In Progress RIVs before the first create.
	qwait0 = _wait_until_riv_queue_clear(campaign=campaign, sess=sess)
	if qwait0.get("stopped") or qwait0.get("lock_lost"):
		sess = get_seq_session() or sess
		sess["status"] = "Paused"
		_save_seq(sess)
		sync_campaign_execution_state(
			campaign, status=STATUS_PAUSED, session_id=session_id
		)
		frappe.db.commit()
		_release_executor_lock(campaign, session_id)
		return {
			"session_id": session_id,
			"campaign": campaign,
			"campaign_status": STATUS_PAUSED,
			"stopped_by_flag": bool(qwait0.get("stopped")),
			"lock_lost": bool(qwait0.get("lock_lost")),
			"queue_wait": qwait0,
			"gates_ok": False,
			"identities_attempted": 0,
			"identities_completed": 0,
			"identities_failed": 0,
		}

	results = []
	completed = 0
	failed = 0
	blocked_at = None
	stopped_by_flag = False
	try:
		for i, row in enumerate(todo):
			if _stop_requested():
				stopped_by_flag = True
				blocked_at = {
					"index": i,
					"reason": "STOP_FLAG",
					"detail": "halted before creating next RIV",
				}
				break
			if not _executor_lock_owned(campaign, session_id):
				stopped_by_flag = True
				blocked_at = {
					"index": i,
					"reason": "LOCK_LOST",
					"detail": "executor lock no longer owned; refuse further creates",
				}
				break
			_empty_rq()
			sync_campaign_execution_state(
				campaign,
				status=STATUS_RUNNING,
				session_id=session_id,
				current_item=row.item_code,
				current_warehouse=row.warehouse,
			)
			out = execute_one_identity(
				row.item_code,
				row.warehouse,
				row.posting_date,
				row.posting_time,
				company=company,
				campaign=campaign,
			)
			if out.get("riv"):
				sync_campaign_execution_state(
					campaign,
					status=STATUS_RUNNING,
					session_id=session_id,
					current_riv=out.get("riv"),
					current_item=row.item_code,
					current_warehouse=row.warehouse,
				)
			results.append(out)
			if out.get("ok"):
				completed += 1
			elif out.get("stopped") or out.get("lock_lost"):
				# STOP during queue/retry wait, or lock loss — pause, not Failed.
				sess = get_seq_session() or sess
				sess["status"] = "Paused"
				_save_seq(sess)
				sync_campaign_execution_state(
					campaign, status=STATUS_PAUSED, session_id=session_id
				)
				frappe.db.commit()
				stopped_by_flag = True
				blocked_at = {
					"index": i,
					"reason": "LOCK_LOST" if out.get("lock_lost") else "STOP_FLAG",
					"detail": (
						"executor lock lost during wait"
						if out.get("lock_lost")
						else "halted during RIV queue or resilient retry wait"
					),
					"riv": out.get("riv"),
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"queue_wait": out.get("queue_wait"),
				}
				break
			else:
				failed += 1
				# FAIL-FAST only after exhausted retries / non-retryable / integrity.
				sess = get_seq_session() or sess
				failure = {
					"at": str(now_datetime()),
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"riv": out.get("riv"),
					"error": out.get("error") or out.get("issues_sample"),
					"attempts_exhausted": out.get("attempts_exhausted"),
					"non_retryable": out.get("non_retryable"),
				}
				sess["last_failure"] = failure
				sess["status"] = "Failed"
				_save_seq(sess)
				sync_campaign_execution_state(
					campaign,
					status=STATUS_FAILED,
					session_id=session_id,
					current_riv=out.get("riv"),
					current_item=row.item_code,
					current_warehouse=row.warehouse,
					failure=failure,
				)
				frappe.db.commit()
				if stop_on_failure and (out.get("stop_chain") or not out.get("ok")):
					blocked_at = {
						"index": i,
						"item_code": row.item_code,
						"warehouse": row.warehouse,
						"riv": out.get("riv"),
						"error": out.get("error") or out.get("issues_sample"),
					}
					break
			if mark_every and (i + 1) % cint(mark_every) == 0:
				mark_campaign_coverage(campaign, limit=20000)
				sync_campaign_execution_state(
					campaign, status=STATUS_RUNNING, session_id=session_id
				)
				frappe.db.commit()
			if _stop_requested():
				stopped_by_flag = True
				blocked_at = {
					"index": i,
					"reason": "STOP_FLAG",
					"detail": "halted after terminal RIV; no further creations",
					"last_riv": out.get("riv"),
				}
				break

		cover = mark_campaign_coverage(campaign, limit=20000)
		# Direct-mark remaining no-SLE
		cover2 = mark_campaign_coverage(campaign, limit=20000)

		vc = frappe.db.sql(
			"""
			select verification_class, count(*) c
			from `tabHistorical Rebuild Campaign Item`
			where campaign=%s group by verification_class
			""",
			campaign,
			as_dict=True,
		)
		success = sum(cint(r.c) for r in vc if r.verification_class in FINAL_SUCCESS_CLASSES)
		total = sum(cint(r.c) for r in vc)
		sess = get_seq_session() or sess

		if blocked_at and blocked_at.get("reason") == "STOP_FLAG":
			sess["status"] = "Paused"
			_save_seq(sess)
			sync_campaign_execution_state(
				campaign, status=STATUS_PAUSED, session_id=session_id
			)
		elif blocked_at:
			# fail-fast already set Failed
			pass
		elif success == total and total > 0:
			sess["status"] = "Completed"
			_save_seq(sess)
			sync_campaign_execution_state(
				campaign, status=STATUS_COMPLETED, session_id=session_id
			)
		else:
			sess["status"] = "Paused"
			_save_seq(sess)
			sync_campaign_execution_state(
				campaign, status=STATUS_PAUSED, session_id=session_id
			)
		frappe.db.commit()

		return {
			"session_id": sess.get("session_id"),
			"campaign": campaign,
			"campaign_status": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"),
			"runtime_seconds": round(time.time() - t0, 3),
			"identities_total": len(idents),
			"identities_attempted": len(results),
			"identities_completed": completed,
			"identities_failed": failed,
			"blocked_at": blocked_at,
			"cover": cover,
			"cover2": cover2,
			"verification_class_counts": {r.verification_class: cint(r.c) for r in vc},
			"session_riv_created": len(sess.get("riv_created") or []),
			"session_riv_completed": len(sess.get("riv_completed") or []),
			"session_riv_failed": len(sess.get("riv_failed") or []),
			"last_failure": sess.get("last_failure"),
			"success_entries": success,
			"total_entries": total,
			"gates_ok": success == total and total > 0 and not blocked_at,
			"stopped_by_flag": stopped_by_flag,
			"results_sample": results[:20],
		}
	finally:
		_release_executor_lock(campaign, session_id)


def prepare_clean_start(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Delete all RIVs, reset campaign, start sequential session."""
	_empty_rq()
	deleted = delete_all_rivs(reason="sequential_rebuild_clean_start")
	reset = reset_campaign_planned(campaign)
	session = start_sequential_session(campaign)
	return {"deleted": deleted, "reset": reset, "session": session}


def analyze_session_riv_reconcile(
	session_id: str,
	riv_name: str,
	*,
	item_code: str | None = None,
	warehouse: str | None = None,
) -> dict[str, Any]:
	"""READ-ONLY operator helper: recommend reconcile steps for a Completed RIV race.

	Does not mutate Production/Development data.
	"""
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_riv_retry import (
		prepare_production_session_reconcile,
		read_riv_status,
	)

	path = os.path.join(_session_dir(), f"session_{session_id}.json")
	if not os.path.exists(path):
		frappe.throw(f"session file missing: {session_id}")
	with open(path, encoding="utf-8") as fh:
		payload = json.load(fh)
	meta = read_riv_status(riv_name) or {}
	item = item_code or meta.get("item_code")
	wh = warehouse or meta.get("warehouse")
	plan = prepare_production_session_reconcile(
		session_payload=payload,
		riv_name=riv_name,
		item_code=item or "",
		warehouse=wh or "",
	)
	return {
		"ok": True,
		"session_id": session_id,
		"riv": meta,
		"plan": plan,
		"writes": False,
		"note": "Operator must apply reconcile manually after integrity verification.",
	}
