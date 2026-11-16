# Copyright (c) 2026, ERPNext Extensions contributors
"""Dev-only HRC staggered concurrency benchmark harness (v5.7.50)."""

from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.staggered_riv import (
	_assert_dev,
	_count_dispatched_active,
	_reconcile_terminals,
	_session_dir,
	clear_staggered_stop,
	dispatch_interval_seconds,
	dispatch_once,
	get_stagger_session,
	load_ledger,
	max_global_concurrent_riv,
	request_staggered_stop,
	save_ledger,
	save_metrics,
	stage_a_bulk_create,
	start_staggered_session,
	_acquire_dispatcher_lock,
	_release_dispatcher_lock,
)


def _rq_stats() -> dict[str, Any]:
	try:
		from redis import Redis
		from rq import Queue, Worker
		from frappe.utils.background_jobs import get_redis_conn

		conn = get_redis_conn()
		workers = []
		long_n = short_n = default_n = 0
		for w in Worker.all(connection=conn):
			qs = [q.name for q in w.queues]
			if not qs:
				continue
			name = qs[0].split(":")[-1] if ":" in qs[0] else qs[0]
			if name == "long":
				long_n += 1
			elif name == "short":
				short_n += 1
			elif name == "default":
				default_n += 1
			workers.append({"queues": qs, "state": w.get_state()})
		depths = {}
		started = {}
		for qn in ("long", "short", "default"):
			q = Queue(qn, connection=conn)
			depths[qn] = q.count
			started[qn] = q.started_job_registry.count
		return {
			"long_workers": long_n,
			"short_workers": short_n,
			"default_workers": default_n,
			"depths": depths,
			"started": started,
		}
	except Exception as exc:
		return {"error": str(exc)[:200]}


def _db_load_sample() -> dict[str, Any]:
	try:
		row = frappe.db.sql(
			"""
			select count(*) threads,
			       sum(command!='Sleep') active_non_sleep
			from information_schema.processlist
			where db = database()
			""",
			as_dict=True,
		)[0]
		return dict(row)
	except Exception as exc:
		return {"error": str(exc)[:120]}


def _outcome(ledger: dict) -> dict[str, Any]:
	from collections import Counter

	c = Counter()
	durs = []
	deadlock = 0
	timeout = 0
	for e in (ledger.get("entries") or {}).values():
		riv = e.get("riv_name")
		if not riv:
			continue
		row = frappe.db.get_value(
			"Repost Item Valuation",
			riv,
			["status", "creation", "modified", "error_log", "vouchers_posted", "total_vouchers"],
			as_dict=True,
		)
		if not row:
			continue
		c[row.status] += 1
		if row.status == "Completed" and row.creation and row.modified:
			durs.append((row.modified - row.creation).total_seconds())
		err = row.error_log or ""
		if "1020" in err or "QueryDeadlockError" in err:
			deadlock += 1
		if "QueryTimeoutError" in err:
			timeout += 1
	return {
		"statuses": dict(c),
		"completed": c.get("Completed", 0),
		"failed": c.get("Failed", 0),
		"in_progress": c.get("In Progress", 0),
		"queued": c.get("Queued", 0),
		"avg_dur_s": (sum(durs) / len(durs)) if durs else None,
		"median_dur_s": statistics.median(durs) if durs else None,
		"min_dur_s": min(durs) if durs else None,
		"max_dur_s": max(durs) if durs else None,
		"deadlock_1020": deadlock,
		"timeout": timeout,
		"sample_durs_n": len(durs),
	}


def run_concurrency_test(
	*,
	campaign: str,
	label: str,
	max_concurrent: int,
	identity_offset: int,
	max_identities: int = 100,
	dispatch_interval_s: float = 5.0,
	max_wall_s: float | None = None,
) -> dict[str, Any]:
	"""One isolated concurrency level: Stage A + Stage B with safety stops."""
	_assert_dev()
	clear_staggered_stop()
	frappe.conf.hrc_stagger_max_global_concurrent_riv = int(max_concurrent)
	frappe.conf.hrc_stagger_dispatch_interval_seconds = float(dispatch_interval_s)

	workers_before = _rq_stats()
	if cint(workers_before.get("long_workers")) < 8:
		return {
			"ok": False,
			"label": label,
			"stop_reason": "insufficient_long_workers",
			"workers": workers_before,
		}

	sle0 = frappe.db.sql("select count(*) c, max(modified) m from `tabStock Ledger Entry`")[0]
	bin0 = frappe.db.sql("select count(*) c, max(modified) m from `tabBin`")[0]
	gl0 = frappe.db.sql("select count(*) c, max(modified) m from `tabGL Entry`")[0]
	hrc16 = frappe.db.get_value(
		"Historical Rebuild Campaign",
		"HRC-00016",
		["status", "modified", "pending"],
		as_dict=True,
	)

	sess = start_staggered_session(
		campaign, max_identities=max_identities, identity_offset=identity_offset
	)
	sid = sess["session_id"]
	a = stage_a_bulk_create(campaign, session_id=sid, max_identities=max_identities)

	# All should be Queued after Stage A
	ledger = load_ledger(sid)
	queued_after_a = 0
	for e in ledger["entries"].values():
		st = frappe.db.get_value("Repost Item Valuation", e["riv_name"], "status")
		if st == "Queued":
			queued_after_a += 1

	_acquire_dispatcher_lock(campaign, sid)
	t0 = time.time()
	ticks = 0
	max_active_seen = 0
	depth_samples = []
	db_samples = []
	stop_reason = None
	interval = float(dispatch_interval_s)
	try:
		while True:
			if max_wall_s is not None and (time.time() - t0) >= float(max_wall_s):
				stop_reason = "max_wall_s"
				request_staggered_stop(reason="max_wall_s")
				break
			out = dispatch_once(campaign, session_id=sid)
			ticks += 1
			ledger = load_ledger(sid)
			sess = get_stagger_session(campaign) or sess
			_reconcile_terminals(sess, ledger)
			# Heal capacity accounting before deciding sleep/exit.
			active = _count_dispatched_active(ledger)
			save_ledger(sid, ledger)
			_save = __import__(
				"erpnext_extensions.iran_accounting.historical_rebuild.staggered_riv",
				fromlist=["_save_session"],
			)._save_session
			_save(sess)
			max_active_seen = max(max_active_seen, active)
			rq = _rq_stats()
			depth_samples.append(
				{
					"t": round(time.time() - t0, 1),
					"depths": rq.get("depths"),
					"started": rq.get("started"),
					"active_riv": active,
					"action": out.get("action"),
				}
			)
			if ticks % 5 == 0:
				db_samples.append({"t": round(time.time() - t0, 1), **_db_load_sample()})
			# Safety: deadlock / failed surge
			outcome = _outcome(ledger)
			if ticks % 10 == 0:
				print(
					f"[bench {label}] t={round(time.time() - t0)}s tick={ticks} "
					f"action={out.get('action')} active={active} "
					f"max_active={max_active_seen} "
					f"completed={outcome.get('completed')} pending={len(pending) if False else sum(1 for e in (ledger.get('entries') or {}).values() if e.get('dispatch_status')=='pending')}",
					flush=True,
				)
			if outcome["deadlock_1020"] > 0:
				stop_reason = "deadlock_1020"
				request_staggered_stop(reason="deadlock_1020")
				break
			if outcome["failed"] > 0:
				stop_reason = "riv_failed"
				request_staggered_stop(reason="riv_failed")
				break

			pending = [
				e
				for e in (ledger.get("entries") or {}).values()
				if e.get("dispatch_status") == "pending"
			]
			if not pending and active == 0:
				stop_reason = "completed"
				break

			if out.get("action") == "dispatched":
				time.sleep(interval)
			elif out.get("action") == "wait_capacity" and active > 0:
				time.sleep(min(interval, 2.0))
			else:
				# idle / capacity freed — poll quickly
				time.sleep(0.5)
	finally:
		_release_dispatcher_lock(campaign, sid)

	elapsed = time.time() - t0
	ledger = load_ledger(sid)
	sess = get_stagger_session(campaign) or sess
	_reconcile_terminals(sess, ledger)
	save_ledger(sid, ledger)
	outcome = _outcome(ledger)
	sle1 = frappe.db.sql("select count(*) c, max(modified) m from `tabStock Ledger Entry`")[0]
	bin1 = frappe.db.sql("select count(*) c, max(modified) m from `tabBin`")[0]
	gl1 = frappe.db.sql("select count(*) c, max(modified) m from `tabGL Entry`")[0]
	hrc16b = frappe.db.get_value(
		"Historical Rebuild Campaign",
		"HRC-00016",
		["status", "modified", "pending"],
		as_dict=True,
	)

	rpm = (outcome["completed"] / (elapsed / 60.0)) if elapsed > 0 else 0.0
	result = {
		"ok": stop_reason in (None, "completed") and outcome["deadlock_1020"] == 0 and outcome["failed"] == 0,
		"label": label,
		"session_id": sid,
		"max_concurrent_config": max_concurrent,
		"identity_offset": identity_offset,
		"max_identities": max_identities,
		"dispatch_interval_s": dispatch_interval_s,
		"stage_a": a,
		"queued_after_stage_a": queued_after_a,
		"stage_b_elapsed_s": round(elapsed, 3),
		"ticks": ticks,
		"max_active_riv_seen": max_active_seen,
		"stop_reason": stop_reason,
		"outcome": outcome,
		"rivs_per_minute": round(rpm, 3),
		"workers_before": workers_before,
		"workers_after": _rq_stats(),
		"depth_samples_n": len(depth_samples),
		"depth_samples_tail": depth_samples[-5:],
		"db_samples_tail": db_samples[-5:],
		"baselines": {"sle": sle0, "bin": bin0, "gl": gl0},
		"after": {"sle": sle1, "bin": bin1, "gl": gl1},
		"hrc16_unchanged": hrc16 == hrc16b,
		"at": str(now_datetime()),
	}
	path = os.path.join(_session_dir(), f"benchmark_w8_{label}_{sid}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(result, fh, indent=2, default=str)
	result["report_path"] = path
	save_metrics(sid, {"benchmark": result})
	return result
