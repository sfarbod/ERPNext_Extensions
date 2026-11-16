# Copyright (c) 2026, ERPNext Extensions contributors
"""Staggered HRC RIV orchestration — Stage A bulk create + Stage B dispatch (v5.7.50).

Separates fast RIV document creation from controlled long-queue execution.
Does not replace sequential_rebuild; used by HRC-00017 STRATEGY=STAGGERED_RIV.
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
	ITEM_PENDING,
	STATUS_DRAFT,
	STATUS_PAUSED,
	STATUS_READY,
	STATUS_RUNNING,
	STATUS_STOPPED,
)
from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
	chronological_identities,
)
from erpnext_extensions.iran_accounting.historical_rebuild.staggered_riv_locks import (
	clear_riv_scopes,
	conflict_blocks_dispatch,
	fetch_future_voucher_keys,
	identity_key,
	purge_orphan_voucher_locks,
	remember_riv_scopes,
	scope_keys_for_identity,
	try_acquire_scopes,
)

CACHE_STAGGER = "hrc_staggered_session"
CACHE_STAGGER_LOCK = "hrc_staggered_dispatcher_lock"
STOP_FLAG_NAME = "STOP_STAGGERED_REBUILD"
STAGGER_REMARK_PREFIX = "STAGGER_SESSION="
STRATEGY_MARKER = "STRATEGY=STAGGERED_RIV"
REPOSTING_JOB_ID_PREFIX = "repost_item_valuation_entry_"

TERMINAL_RIV = frozenset({"Completed", "Skipped", "Failed", "Cancelled"})
ACTIVE_RIV = frozenset({"Queued", "In Progress"})


# ---------------------------------------------------------------------------
# Config knobs (site_config / frappe.conf)
# ---------------------------------------------------------------------------


def conf_float(key: str, default: float) -> float:
	try:
		return float(frappe.conf.get(key) if frappe.conf.get(key) is not None else default)
	except Exception:
		return float(default)


def conf_int(key: str, default: int) -> int:
	try:
		return int(cint(frappe.conf.get(key) if frappe.conf.get(key) is not None else default))
	except Exception:
		return int(default)


def dispatch_interval_seconds() -> float:
	return max(0.0, conf_float("hrc_stagger_dispatch_interval_seconds", 5.0))


def max_global_concurrent_riv() -> int:
	return max(1, conf_int("hrc_stagger_max_global_concurrent_riv", 1))


def retry_limit() -> int:
	return max(0, conf_int("hrc_stagger_retry_limit", 3))


# ---------------------------------------------------------------------------
# Dev gate / paths
# ---------------------------------------------------------------------------


def _assert_dev() -> None:
	site = frappe.local.site or ""
	if "espadpharmed.com" in site and "development" not in site:
		frappe.throw(f"Refused staggered HRC on production-like site: {site}")
	if not (
		frappe.conf.get("developer_mode")
		or frappe.conf.get("e2e_marker")
		or frappe.conf.get("restored_from_backup")
	):
		frappe.throw("Refused: site not marked as development/test")


def _session_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"hrc_staggered_rebuild",
	)
	os.makedirs(path, exist_ok=True)
	return path


def _stop_flag_path() -> str:
	return os.path.join(_session_dir(), STOP_FLAG_NAME)


def request_staggered_stop(*, reason: str = "operator") -> dict[str, Any]:
	path = _stop_flag_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump({"reason": reason, "at": str(now_datetime())}, fh)
	return {"ok": True, "path": path, "reason": reason}


def clear_staggered_stop() -> None:
	path = _stop_flag_path()
	if os.path.exists(path):
		os.remove(path)


def _stop_requested() -> bool:
	return os.path.exists(_stop_flag_path())


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
	return now_datetime().strftime("%Y%m%d%H%M%S")


def _session_path(session_id: str) -> str:
	return os.path.join(_session_dir(), f"session_{session_id}.json")


def _ledger_path(session_id: str) -> str:
	return os.path.join(_session_dir(), f"ledger_{session_id}.json")


def _metrics_path(session_id: str) -> str:
	return os.path.join(_session_dir(), f"metrics_{session_id}.json")


def _stamp() -> str:
	try:
		return str(now_datetime())
	except Exception:
		from datetime import datetime

		return datetime.utcnow().isoformat(sep=" ", timespec="seconds")


def _save_session(payload: dict) -> None:
	data = dict(payload)
	data["revision"] = int(data.get("revision") or 0) + 1
	data["updated_at"] = _stamp()
	path = _session_path(data["session_id"])
	tmp = path + ".tmp"
	with open(tmp, "w", encoding="utf-8") as fh:
		json.dump(data, fh, indent=2, default=str)
	os.replace(tmp, path)
	try:
		frappe.cache().set_value(CACHE_STAGGER, data, expires_in_sec=60 * 60 * 24 * 14)
	except Exception:
		# Unit tests / Redis-down: disk session remains source of truth.
		pass


def get_stagger_session(campaign: str | None = None) -> dict | None:
	cached = frappe.cache().get_value(CACHE_STAGGER)
	disk = None
	if isinstance(cached, dict) and cached.get("session_id"):
		path = _session_path(cached["session_id"])
		if os.path.exists(path):
			with open(path, encoding="utf-8") as fh:
				disk = json.load(fh)
	if disk and cached:
		if int(disk.get("revision") or 0) >= int(cached.get("revision") or 0):
			sess = disk
		else:
			sess = cached
	else:
		sess = disk or (cached if isinstance(cached, dict) else None)
	if not sess:
		return None
	if campaign and sess.get("campaign") and sess.get("campaign") != campaign:
		# Try restore from campaign remarks
		sid = parse_stagger_session_id(campaign)
		if sid:
			return load_session_from_file(sid)
		return None
	return sess


def load_session_from_file(session_id: str) -> dict | None:
	path = _session_path(session_id)
	if not os.path.exists(path):
		return None
	with open(path, encoding="utf-8") as fh:
		return json.load(fh)


def parse_stagger_session_id(campaign: str) -> str | None:
	remarks = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "remarks") or ""
	for line in str(remarks).splitlines():
		line = line.strip()
		if line.startswith(STAGGER_REMARK_PREFIX):
			return line[len(STAGGER_REMARK_PREFIX) :].strip() or None
	return None


def _write_campaign_stagger_remarks(campaign: str, session_id: str) -> None:
	remarks = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "remarks") or ""
	lines = [ln for ln in str(remarks).splitlines() if not ln.startswith("SEQ_SESSION=")]
	# Replace prior STAGGER_SESSION / STRATEGY lines
	kept = [
		ln
		for ln in lines
		if not ln.startswith(STAGGER_REMARK_PREFIX) and not ln.startswith("STRATEGY=")
	]
	kept.insert(0, f"{STAGGER_REMARK_PREFIX}{session_id}")
	kept.insert(1, STRATEGY_MARKER)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"remarks": "\n".join(kept), "last_activity": now_datetime()},
		update_modified=False,
	)


def load_ledger(session_id: str) -> dict[str, Any]:
	path = _ledger_path(session_id)
	if not os.path.exists(path):
		return {"entries": {}, "order": []}
	with open(path, encoding="utf-8") as fh:
		return json.load(fh)


def save_ledger(session_id: str, ledger: dict) -> None:
	path = _ledger_path(session_id)
	tmp = path + ".tmp"
	with open(tmp, "w", encoding="utf-8") as fh:
		json.dump(ledger, fh, indent=2, default=str)
	os.replace(tmp, path)


def save_metrics(session_id: str, metrics: dict) -> None:
	path = _metrics_path(session_id)
	tmp = path + ".tmp"
	with open(tmp, "w", encoding="utf-8") as fh:
		json.dump(metrics, fh, indent=2, default=str)
	os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Clone campaign (HRC-00016 → HRC-00017)
# ---------------------------------------------------------------------------


def clone_campaign_manifest(
	*,
	source: str = "HRC-00016",
	target: str = "HRC-00017",
	confirm: str | None = None,
) -> dict[str, Any]:
	"""Dev-gated: force-create target campaign by copying source items+checksum.

	Idempotent when target already exists with matching checksum.
	Never mutates ``source``.
	"""
	_assert_dev()
	if confirm != "CLONE_CAMPAIGN_MANIFEST":
		frappe.throw("confirm must equal CLONE_CAMPAIGN_MANIFEST")
	if source == target:
		frappe.throw("source and target must differ")
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, source):
		frappe.throw(f"Source campaign missing: {source}")

	src = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		source,
		[
			"company",
			"manifest_checksum",
			"posting_boundary_from",
			"total_documents",
			"status",
		],
		as_dict=True,
	)
	src_count = cint(frappe.db.count(ITEM_DOCTYPE, {"campaign": source}))
	if not src.manifest_checksum or src_count <= 0:
		frappe.throw(f"Source {source} has no verified manifest to clone")

	if frappe.db.exists(CAMPAIGN_DOCTYPE, target):
		tgt_cs = frappe.db.get_value(CAMPAIGN_DOCTYPE, target, "manifest_checksum")
		tgt_count = cint(frappe.db.count(ITEM_DOCTYPE, {"campaign": target}))
		if tgt_cs == src.manifest_checksum and tgt_count == src_count:
			return {
				"ok": True,
				"idempotent": True,
				"campaign": target,
				"source": source,
				"item_count": tgt_count,
				"manifest_checksum": tgt_cs,
				"status": frappe.db.get_value(CAMPAIGN_DOCTYPE, target, "status"),
			}
		frappe.throw(
			f"Target {target} exists with mismatched manifest "
			f"(checksum/count); refuse overwrite"
		)

	# Force-named insert (autoname is naming_series)
	doc = frappe.get_doc(
		{
			"doctype": CAMPAIGN_DOCTYPE,
			"name": target,
			"naming_series": "HRC-.#####",
			"company": src.company,
			"status": STATUS_DRAFT,
			"posting_boundary_from": src.posting_boundary_from,
			"manifest_checksum": src.manifest_checksum,
			"total_documents": src_count,
			"completed": 0,
			"pending": src_count,
			"failed": 0,
			"blocked": 0,
			"skipped_covered": 0,
			"riv_created": 0,
			"riv_completed": 0,
			"riv_queued": 0,
			"riv_in_progress": 0,
			"riv_failed": 0,
			"remarks": (
				f"{STRATEGY_MARKER}\n"
				f"Cloned manifest from {source} (checksum={src.manifest_checksum}); "
				f"items={src_count}. Independent staggered session."
			),
		}
	)
	doc.flags.name_set = True
	doc.insert(ignore_permissions=True, ignore_if_duplicate=False)
	frappe.db.commit()

	# Bulk copy items via SQL (preserve ordinals / voucher identity)
	frappe.db.sql(
		f"""
		INSERT INTO `tab{ITEM_DOCTYPE}`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 campaign, ordinal, company, voucher_type, voucher_no, item_status,
			 posting_date, posting_time, creation_ts,
			 source_rebuild_status, source_rebuild_detail, riv_names, covering_riv,
			 retry_count, tva_reconcile_status, error_message, last_processed_at,
			 verification_class, verification_detail)
		SELECT
			CONCAT(%(target)s, '-', LPAD(ordinal, 6, '0'), '-', SUBSTRING(MD5(CONCAT(voucher_type, voucher_no)), 1, 8)),
			NOW(6), NOW(6), 'Administrator', 'Administrator', 0, idx,
			%(target)s, ordinal, company, voucher_type, voucher_no, %(pending)s,
			posting_date, posting_time, creation_ts,
			NULL, NULL, NULL, NULL,
			0, NULL, NULL, NULL,
			NULL, NULL
		FROM `tab{ITEM_DOCTYPE}`
		WHERE campaign=%(source)s
		ORDER BY ordinal
		""",
		{"target": target, "source": source, "pending": ITEM_PENDING},
	)
	frappe.db.commit()

	copied = cint(frappe.db.count(ITEM_DOCTYPE, {"campaign": target}))
	if copied != src_count:
		frappe.throw(f"Clone incomplete: expected {src_count} items, got {copied}")

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		target,
		{
			"status": STATUS_READY,
			"total_documents": copied,
			"pending": copied,
			"completed": 0,
			"last_activity": now_datetime(),
		},
		update_modified=True,
	)
	frappe.db.commit()

	return {
		"ok": True,
		"idempotent": False,
		"campaign": target,
		"source": source,
		"item_count": copied,
		"manifest_checksum": src.manifest_checksum,
		"status": STATUS_READY,
	}


# ---------------------------------------------------------------------------
# Stage A — bulk RIV create
# ---------------------------------------------------------------------------


def start_staggered_session(
	campaign: str,
	*,
	max_identities: int | None = None,
	identity_offset: int = 0,
) -> dict[str, Any]:
	_assert_dev()
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(f"Campaign missing: {campaign}")
	clear_staggered_stop()
	session_id = _new_session_id()
	idents = chronological_identities(campaign)
	offset = max(0, cint(identity_offset))
	if offset:
		idents = idents[offset:]
	if max_identities is not None:
		idents = idents[: max(0, cint(max_identities))]
	plan = [
		{
			"item_code": r.item_code,
			"warehouse": r.warehouse,
			"posting_date": str(r.posting_date),
			"posting_time": str(r.posting_time or "00:00:00"),
			"voucher_count": cint(r.get("voucher_count")),
		}
		for r in idents
	]
	company = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	payload = {
		"session_id": session_id,
		"campaign": campaign,
		"company": company,
		"strategy": "STAGGERED_RIV",
		"status": "Starting",
		"plan_size": len(plan),
		"identity_offset": offset,
		"max_identities": cint(max_identities) if max_identities is not None else None,
		"plan": plan,
		"riv_created": 0,
		"riv_dispatched": 0,
		"riv_completed": 0,
		"riv_failed": 0,
		"revision": 0,
		"created_at": str(now_datetime()),
		"stage_a": {},
		"stage_b": {},
		"errors": {"deadlock_1020": 0, "timeout": 0, "worker_timeout": 0, "duplicate_exec": 0},
	}
	_save_session(payload)
	save_ledger(session_id, {"entries": {}, "order": []})
	_write_campaign_stagger_remarks(campaign, session_id)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"status": STATUS_RUNNING, "started_at": now_datetime(), "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	return payload


def _find_existing_riv(item_code, warehouse, posting_date, posting_time, company) -> str | None:
	"""Reuse submitted open RIV for same identity+posting if already campaign-owned in ledger.

	Does not adopt unrelated site Queued RIVs (vot*).
	"""
	# Only match exact posting identity among recently created staggered RIVs via ledger.
	return None


def _create_riv_for_identity(
	*,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	company: str,
) -> str:
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
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": posting_date,
			"docstatus": 1,
		},
		"name",
		order_by="creation desc",
	)
	if not name:
		frappe.throw(f"RIV create failed for {item_code}|{warehouse}")
	return name


def stage_a_bulk_create(
	campaign: str,
	*,
	session_id: str | None = None,
	chunk_commit: int = 25,
	max_identities: int | None = None,
) -> dict[str, Any]:
	"""Create all plan RIVs without waiting for execution."""
	_assert_dev()
	sess = get_stagger_session(campaign)
	if not sess:
		sess = start_staggered_session(campaign, max_identities=max_identities)
	if session_id and sess.get("session_id") != session_id:
		frappe.throw("session_id mismatch")
	sid = sess["session_id"]
	ledger = load_ledger(sid)
	entries: dict = ledger.setdefault("entries", {})
	order: list = ledger.setdefault("order", [])
	company = sess.get("company") or frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	plan = sess.get("plan") or []
	t0 = time.time()
	created = 0
	reused = 0
	sess["status"] = "StageA"
	_save_session(sess)

	for i, row in enumerate(plan):
		if _stop_requested():
			sess["status"] = "Paused"
			sess["stage_a"] = {
				"stopped": True,
				"processed": i,
				"duration_s": round(time.time() - t0, 3),
			}
			_save_session(sess)
			save_ledger(sid, ledger)
			return {"ok": False, "stopped": True, "session_id": sid, "created": created}

		ikey = identity_key(row["item_code"], row["warehouse"])
		existing = entries.get(ikey)
		if existing and existing.get("riv_name"):
			# Idempotent resume
			riv = existing["riv_name"]
			st = frappe.db.get_value("Repost Item Valuation", riv, "status")
			if st and riv:
				reused += 1
				continue

		riv_name = _create_riv_for_identity(
			item_code=row["item_code"],
			warehouse=row["warehouse"],
			posting_date=row["posting_date"],
			posting_time=row["posting_time"],
			company=company,
		)
		# Precompute overlap sample once at create-time (avoid per-tick SLE scans).
		try:
			vouchers = sorted(
				fetch_future_voucher_keys(
					row["item_code"],
					row["warehouse"],
					row["posting_date"],
					row.get("posting_time"),
					limit=500,
				)
			)
		except Exception:
			vouchers = []
		entry = {
			"identity_key": ikey,
			"item_code": row["item_code"],
			"warehouse": row["warehouse"],
			"posting_date": row["posting_date"],
			"posting_time": row["posting_time"],
			"riv_name": riv_name,
			"created_at": str(now_datetime()),
			"dispatched_at": None,
			"job_id": None,
			"queue": None,
			"status": "created",
			"dispatch_status": "pending",
			"retries": 0,
			"conflict_scopes": scope_keys_for_identity(row["item_code"], row["warehouse"]),
			"voucher_keys": vouchers,
		}
		entries[ikey] = entry
		if ikey not in order:
			order.append(ikey)
		created += 1
		sess["riv_created"] = cint(sess.get("riv_created")) + 1

		if created % max(1, cint(chunk_commit)) == 0:
			frappe.db.set_value(
				CAMPAIGN_DOCTYPE,
				campaign,
				{
					"riv_created": cint(sess.get("riv_created")),
					"riv_queued": cint(sess.get("riv_created")),
					"last_activity": now_datetime(),
				},
				update_modified=False,
			)
			frappe.db.commit()
			save_ledger(sid, ledger)
			_save_session(sess)

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"riv_created": cint(sess.get("riv_created")),
			"riv_queued": cint(sess.get("riv_created")),
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	duration = round(time.time() - t0, 3)
	sess["status"] = "StageAComplete"
	sess["stage_a"] = {
		"created": created,
		"reused": reused,
		"duration_s": duration,
		"riv_total": len(entries),
		"completed_at": str(now_datetime()),
	}
	_save_session(sess)
	save_ledger(sid, ledger)
	save_metrics(
		sid,
		{
			"stage_a_duration_s": duration,
			"stage_a_created": created,
			"stage_a_reused": reused,
			"plan_size": len(plan),
		},
	)
	return {
		"ok": True,
		"session_id": sid,
		"created": created,
		"reused": reused,
		"riv_total": len(entries),
		"duration_s": duration,
	}


# ---------------------------------------------------------------------------
# Stage B — dispatcher
# ---------------------------------------------------------------------------


def _dispatcher_lock_key(campaign: str) -> str:
	return f"{CACHE_STAGGER_LOCK}:{campaign}"


def _acquire_dispatcher_lock(campaign: str, session_id: str) -> None:
	key = _dispatcher_lock_key(campaign)
	held = frappe.cache().get_value(key)
	if held and held != session_id:
		frappe.throw(
			f"Staggered dispatcher already active for {campaign} (session={held})"
		)
	frappe.cache().set_value(key, session_id, expires_in_sec=6 * 60 * 60)


def _refresh_dispatcher_lock(campaign: str, session_id: str) -> bool:
	key = _dispatcher_lock_key(campaign)
	held = frappe.cache().get_value(key)
	if held and held != session_id:
		return False
	frappe.cache().set_value(key, session_id, expires_in_sec=6 * 60 * 60)
	return True


def _release_dispatcher_lock(campaign: str, session_id: str) -> None:
	key = _dispatcher_lock_key(campaign)
	held = frappe.cache().get_value(key)
	if held == session_id:
		frappe.cache().delete_value(key)


def _enqueue_riv_long(riv_name: str) -> str:
	"""Enqueue core execute on long with continue_reposting=False (no flood)."""
	job_id = f"{REPOSTING_JOB_ID_PREFIX}{riv_name}"
	frappe.enqueue(
		"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.execute_reposting_entry",
		name=riv_name,
		continue_reposting=False,
		queue="long",
		timeout=1800,
		job_id=job_id,
		deduplicate=True,
	)
	return job_id


def _count_dispatched_active(ledger: dict) -> int:
	"""Count truly active dispatched RIVs; heal stale dispatched→terminal rows."""
	n = 0
	for e in (ledger.get("entries") or {}).values():
		if e.get("dispatch_status") != "dispatched":
			continue
		riv = e.get("riv_name")
		if not riv:
			continue
		st = frappe.db.get_value("Repost Item Valuation", riv, "status")
		if st in ACTIVE_RIV:
			n += 1
		elif st in TERMINAL_RIV:
			e["dispatch_status"] = "terminal"
			e["riv_status"] = st
			e["finished_at"] = _stamp()
			try:
				clear_riv_scopes(riv)
			except Exception:
				pass
		elif not st:
			# Missing row — do not block the dispatcher forever.
			e["dispatch_status"] = "terminal"
			e["riv_status"] = "Missing"
	return n


def _reconcile_terminals(sess: dict, ledger: dict) -> dict[str, int]:
	"""Update ledger for finished RIVs; release conflict locks; bump counters."""
	completed = failed = stuck = 0
	for e in (ledger.get("entries") or {}).values():
		riv = e.get("riv_name")
		if not riv:
			continue
		raw = frappe.db.get_value(
			"Repost Item Valuation", riv, ["status", "error_log"], as_dict=True
		)
		if not raw:
			continue
		if isinstance(raw, dict) or hasattr(raw, "status"):
			status = raw.get("status") if isinstance(raw, dict) else raw.status
			err = (raw.get("error_log") if isinstance(raw, dict) else raw.error_log) or ""
		else:
			status = raw
			err = ""
		prev = e.get("riv_status")
		e["riv_status"] = status
		if status in TERMINAL_RIV and e.get("dispatch_status") == "dispatched":
			e["dispatch_status"] = "terminal"
			e["finished_at"] = _stamp()
			clear_riv_scopes(riv)
			if status in ("Completed", "Skipped"):
				if prev != status:
					sess["riv_completed"] = cint(sess.get("riv_completed")) + 1
				completed += 1
			elif status in ("Failed", "Cancelled"):
				if prev != status:
					sess["riv_failed"] = cint(sess.get("riv_failed")) + 1
				failed += 1
				if "1020" in err or "QueryDeadlockError" in err:
					sess.setdefault("errors", {})
					sess["errors"]["deadlock_1020"] = cint(sess["errors"].get("deadlock_1020")) + 1
				if "QueryTimeoutError" in err or "timeout" in err.lower():
					sess.setdefault("errors", {})
					sess["errors"]["timeout"] = cint(sess["errors"].get("timeout")) + 1
		elif status == "In Progress" and e.get("dispatch_status") == "dispatched":
			stuck += 0
	return {"completed_seen": completed, "failed_seen": failed, "in_progress_dispatched": stuck}


def dispatch_once(campaign: str, *, session_id: str | None = None) -> dict[str, Any]:
	"""Single dispatcher tick: reconcile, maybe enqueue one eligible RIV."""
	_assert_dev()
	# End any open REPEATABLE READ snapshot so we see worker-committed RIV
	# status changes (Completed/Failed). Without this, wait_capacity ticks can
	# permanently observe stale In Progress/Queued and stall the drain.
	try:
		frappe.db.commit()
	except Exception:
		try:
			frappe.db.rollback()
		except Exception:
			pass
	sess = get_stagger_session(campaign)
	if not sess:
		frappe.throw("No staggered session for campaign")
	sid = sess["session_id"]
	if session_id and session_id != sid:
		frappe.throw("session_id mismatch")
	if not _refresh_dispatcher_lock(campaign, sid):
		return {"ok": False, "reason": "lock_lost"}

	ledger = load_ledger(sid)
	_reconcile_terminals(sess, ledger)

	if _stop_requested():
		sess["status"] = "Paused"
		_save_session(sess)
		save_ledger(sid, ledger)
		_release_dispatcher_lock(campaign, sid)
		return {"ok": False, "stopped": True}

	active = _count_dispatched_active(ledger)
	max_c = max_global_concurrent_riv()
	if active >= max_c:
		save_ledger(sid, ledger)
		_save_session(sess)
		try:
			frappe.db.commit()
		except Exception:
			pass
		return {"ok": True, "action": "wait_capacity", "active": active, "max": max_c}

	# Prevent completed RIVs' voucher samples from permanently serializing the queue.
	active_names = {
		e.get("riv_name")
		for e in (ledger.get("entries") or {}).values()
		if e.get("dispatch_status") == "dispatched" and e.get("riv_name")
	}
	purge_orphan_voucher_locks(active_rivs=active_names)

	dispatched = None
	blocked = 0
	for ikey in ledger.get("order") or []:
		e = ledger["entries"].get(ikey)
		if not e or e.get("dispatch_status") in ("dispatched", "terminal"):
			continue
		if e.get("dispatch_status") == "pending" and e.get("riv_name"):
			cached = e.get("voucher_keys")
			probe = conflict_blocks_dispatch(
				item_code=e["item_code"],
				warehouse=e["warehouse"],
				posting_date=e["posting_date"],
				posting_time=e.get("posting_time"),
				candidate_vouchers=set(cached) if cached is not None else None,
			)
			if probe["blocked"]:
				blocked += 1
				e["last_block_reason"] = probe["reason"]
				continue
			scopes = probe["scopes"]
			vouchers = probe["vouchers"]
			token = try_acquire_scopes(scopes)
			if not token:
				blocked += 1
				e["last_block_reason"] = "acquire_failed"
				continue
			remember_riv_scopes(e["riv_name"], scopes, vouchers, token)
			# Ensure RIV still Queued before enqueue
			st = frappe.db.get_value("Repost Item Valuation", e["riv_name"], "status")
			if st not in ("Queued", "In Progress"):
				clear_riv_scopes(e["riv_name"])
				e["last_block_reason"] = f"unexpected_status:{st}"
				blocked += 1
				continue
			job_id = _enqueue_riv_long(e["riv_name"])
			e["job_id"] = job_id
			e["queue"] = "long"
			e["dispatched_at"] = str(now_datetime())
			e["dispatch_status"] = "dispatched"
			e["lock_token"] = token
			e["conflict_scopes"] = scopes
			sess["riv_dispatched"] = cint(sess.get("riv_dispatched")) + 1
			dispatched = e["riv_name"]
			break

	save_ledger(sid, ledger)
	sess["status"] = "StageB"
	sess["stage_b"] = {
		"last_tick_at": str(now_datetime()),
		"active": _count_dispatched_active(ledger),
		"blocked_skipped": blocked,
		"last_dispatched": dispatched,
		"max_global": max_c,
		"interval_s": dispatch_interval_seconds(),
	}
	_save_session(sess)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"riv_completed": cint(sess.get("riv_completed")),
			"riv_failed": cint(sess.get("riv_failed")),
			"riv_in_progress": cint(sess["stage_b"].get("active")),
			"last_activity": now_datetime(),
			"current_voucher_type": "Repost Item Valuation" if dispatched else None,
			"current_voucher_no": dispatched,
		},
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"ok": True,
		"action": "dispatched" if dispatched else "idle",
		"riv": dispatched,
		"active": sess["stage_b"]["active"],
		"blocked_skipped": blocked,
	}


def run_dispatcher_loop(
	campaign: str,
	*,
	session_id: str | None = None,
	max_ticks: int | None = None,
	poll_terminal_s: float | None = None,
) -> dict[str, Any]:
	"""Run Stage B until plan done, stopped, or max_ticks."""
	_assert_dev()
	sess = get_stagger_session(campaign)
	if not sess:
		frappe.throw("No staggered session")
	sid = sess["session_id"]
	_acquire_dispatcher_lock(campaign, sid)
	t0 = time.time()
	ticks = 0
	interval = dispatch_interval_seconds()
	poll = float(poll_terminal_s if poll_terminal_s is not None else min(interval, 2.0))
	actions: list[dict] = []
	try:
		while True:
			if max_ticks is not None and ticks >= cint(max_ticks):
				break
			if _stop_requested():
				break
			out = dispatch_once(campaign, session_id=sid)
			ticks += 1
			actions.append(out)
			ledger = load_ledger(sid)
			pending = [
				e
				for e in (ledger.get("entries") or {}).values()
				if e.get("dispatch_status") == "pending"
			]
			active = _count_dispatched_active(ledger)
			_reconcile_terminals(sess, ledger)
			# Reload session counters after reconcile
			sess = get_stagger_session(campaign) or sess
			save_ledger(sid, ledger)
			if not pending and active == 0:
				sess["status"] = "Completed"
				sess["stage_b"]["completed_at"] = str(now_datetime())
				sess["stage_b"]["duration_s"] = round(time.time() - t0, 3)
				_save_session(sess)
				frappe.db.set_value(
					CAMPAIGN_DOCTYPE,
					campaign,
					{
						"status": "Completed",
						"completed_at": now_datetime(),
						"last_activity": now_datetime(),
					},
					update_modified=False,
				)
				frappe.db.commit()
				break
			# Pace dispatches
			if out.get("action") == "dispatched":
				time.sleep(interval)
			else:
				time.sleep(poll)
	finally:
		_release_dispatcher_lock(campaign, sid)

	sess = get_stagger_session(campaign) or sess
	metrics = {
		"stage_b_duration_s": round(time.time() - t0, 3),
		"ticks": ticks,
		"dispatch_interval_s": interval,
		"max_global_concurrent": max_global_concurrent_riv(),
		"riv_dispatched": cint(sess.get("riv_dispatched")),
		"riv_completed": cint(sess.get("riv_completed")),
		"riv_failed": cint(sess.get("riv_failed")),
		"errors": sess.get("errors"),
	}
	# merge with stage_a metrics if present
	mpath = _metrics_path(sid)
	if os.path.exists(mpath):
		with open(mpath, encoding="utf-8") as fh:
			prev = json.load(fh)
		prev.update(metrics)
		metrics = prev
	save_metrics(sid, metrics)
	return {"ok": True, "session_id": sid, "ticks": ticks, "metrics": metrics, "last": actions[-1] if actions else None}


def enqueue_dispatcher(campaign: str, *, async_run: bool = True) -> dict[str, Any]:
	"""Enqueue Stage B loop on short queue (orchestration)."""
	_assert_dev()
	sess = get_stagger_session(campaign)
	if not sess:
		frappe.throw("Start session / Stage A first")
	if async_run:
		frappe.enqueue(
			"erpnext_extensions.iran_accounting.historical_rebuild.staggered_riv.run_dispatcher_loop",
			campaign=campaign,
			session_id=sess["session_id"],
			queue="short",
			timeout=60 * 60 * 12,
			job_id=f"hrc_stagger_dispatch:{campaign}:{sess['session_id']}",
			deduplicate=True,
		)
		return {"ok": True, "async": True, "queue": "short", "session_id": sess["session_id"]}
	return run_dispatcher_loop(campaign, session_id=sess["session_id"])


def run_staggered_campaign(
	campaign: str,
	*,
	max_identities: int | None = None,
	async_dispatch: bool = False,
	max_ticks: int | None = None,
) -> dict[str, Any]:
	"""Full Stage A then Stage B for a campaign (Dev)."""
	_assert_dev()
	sess = start_staggered_session(campaign, max_identities=max_identities)
	a = stage_a_bulk_create(campaign, session_id=sess["session_id"], max_identities=max_identities)
	if not a.get("ok"):
		return {"ok": False, "stage_a": a, "session_id": sess["session_id"]}
	if async_dispatch:
		b = enqueue_dispatcher(campaign, async_run=True)
		return {"ok": True, "stage_a": a, "stage_b": b, "session_id": sess["session_id"]}
	b = run_dispatcher_loop(
		campaign, session_id=sess["session_id"], max_ticks=max_ticks
	)
	return {"ok": True, "stage_a": a, "stage_b": b, "session_id": sess["session_id"]}


def get_staggered_status(campaign: str) -> dict[str, Any]:
	sess = get_stagger_session(campaign)
	sid = sess.get("session_id") if sess else parse_stagger_session_id(campaign)
	ledger = load_ledger(sid) if sid else {"entries": {}, "order": []}
	metrics = None
	if sid and os.path.exists(_metrics_path(sid)):
		with open(_metrics_path(sid), encoding="utf-8") as fh:
			metrics = json.load(fh)
	camp = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		[
			"status",
			"manifest_checksum",
			"total_documents",
			"pending",
			"completed",
			"riv_created",
			"riv_completed",
			"riv_queued",
			"riv_in_progress",
			"riv_failed",
			"remarks",
			"last_activity",
		],
		as_dict=True,
	)
	entries = list((ledger.get("entries") or {}).values())
	return {
		"campaign": campaign,
		"campaign_row": camp,
		"session": {
			k: sess.get(k)
			for k in (
				"session_id",
				"status",
				"plan_size",
				"riv_created",
				"riv_dispatched",
				"riv_completed",
				"riv_failed",
				"stage_a",
				"stage_b",
				"errors",
				"strategy",
			)
		}
		if sess
		else None,
		"ledger_summary": {
			"total": len(entries),
			"pending_dispatch": sum(1 for e in entries if e.get("dispatch_status") == "pending"),
			"dispatched": sum(1 for e in entries if e.get("dispatch_status") == "dispatched"),
			"terminal": sum(1 for e in entries if e.get("dispatch_status") == "terminal"),
		},
		"metrics": metrics,
		"config": {
			"dispatch_interval_seconds": dispatch_interval_seconds(),
			"max_global_concurrent_riv": max_global_concurrent_riv(),
			"retry_limit": retry_limit(),
		},
	}
