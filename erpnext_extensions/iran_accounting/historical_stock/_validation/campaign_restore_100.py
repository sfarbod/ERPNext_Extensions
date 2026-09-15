# Copyright (c) 2026 — restored-DB campaign runner (dev only).
"""Phase 1–6: baseline → master plan → top-100 READY queue → controlled apply."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from time import perf_counter

import frappe
from frappe.utils import flt, nowdate

BASE = "/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/restore_20260915_133438"
COMPANY = "اسپاد فارمد دارو"
FROM_DATE = "2026-03-21"  # 1405-01-01 ≈ Gregorian start of Farvardin 1405
MAX_SUCCESS = 100
CHECKPOINT_EVERY = 10


def _dump(name, data):
	os.makedirs(BASE, exist_ok=True)
	path = os.path.join(BASE, name)
	parent = os.path.dirname(path)
	if parent:
		os.makedirs(parent, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def _counts():
	def c(sql, *args):
		return int(frappe.db.sql(sql, args)[0][0] or 0)

	return {
		"sle": c("SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE IFNULL(is_cancelled,0)=0"),
		"stock_entry": c("SELECT COUNT(*) FROM `tabStock Entry` WHERE docstatus=1"),
		"bin": c("SELECT COUNT(*) FROM `tabBin`"),
		"gl": c("SELECT COUNT(*) FROM `tabGL Entry` WHERE IFNULL(is_cancelled,0)=0"),
		"sabb": c("SELECT COUNT(*) FROM `tabSerial and Batch Bundle`") if frappe.db.table_exists("Serial and Batch Bundle") else 0,
		"sbe": c("SELECT COUNT(*) FROM `tabSerial and Batch Entry`") if frappe.db.table_exists("Serial and Batch Entry") else 0,
		"riv": c("SELECT COUNT(*) FROM `tabRepost Item Valuation` WHERE docstatus=1"),
		"failed_riv": c(
			"SELECT COUNT(*) FROM `tabRepost Item Valuation` WHERE status='Failed' AND docstatus=1"
		),
		"allow_negative_stock": frappe.db.get_single_value("Stock Settings", "allow_negative_stock"),
	}


def collect_baseline():
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	t0 = perf_counter()
	counts = _counts()
	dash = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
	i4 = scan_i4_leftover(company=COMPANY, from_date=FROM_DATE, to_date=nowdate(), limit=5000)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": COMPANY,
		"from_date": FROM_DATE,
		"to_date": nowdate(),
		"counts": counts,
		"dashboard": dash.get("dashboard") or dash.get("kpis") or {},
		"timing": dash.get("timing"),
		"i4_scan": {
			"count": i4.get("count"),
			"by_status": i4.get("by_status"),
			"ready": sum(1 for r in (i4.get("rows") or []) if r.get("eligible") or r.get("i4_status") == "READY_I4"),
			"manual": sum(1 for r in (i4.get("rows") or []) if (r.get("i4_status") or "") == "MANUAL"),
			"waiting": sum(1 for r in (i4.get("rows") or []) if "WAITING" in str(r.get("i4_status") or "")),
		},
		"elapsed_seconds": round(perf_counter() - t0, 2),
	}
	_dump("baseline/baseline.json", out)
	print(json.dumps({"counts": counts, "dashboard": out["dashboard"], "i4": out["i4_scan"]}, indent=2, default=str, ensure_ascii=False))
	return out


def build_top100():
	"""Rank READY roots: posting-order first, then gated READY_I4, then exact zero-rate."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover, preview_i4_replay
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row, READY_STATUSES
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	queue = []

	# 1) Posting Order READY
	posting = run_full_history_scan(company=COMPANY)
	for r in posting.get("rows") or []:
		ps = r.get("planner_status") or ""
		if ps not in READY_STATUSES and not r.get("eligible"):
			continue
		if (r.get("sql_updates") or 0) <= 0 and not r.get("eligible"):
			continue
		queue.append(
			_entry(
				priority_class=1,
				repair_class="POSTING_ORDER",
				row=r,
				reason="READY posting-order inversion with independent safety",
			)
		)

	# 2) READY_I4 with healthy opening + sim clears
	i4 = scan_i4_leftover(company=COMPANY, from_date=FROM_DATE, to_date=nowdate(), limit=5000)
	for r in i4.get("rows") or []:
		if not (r.get("eligible") or r.get("i4_status") == "READY_I4"):
			continue
		if r.get("previous_healthy") is False or r.get("pz_residual_clears_in_sim") is False:
			continue
		dec = evaluate_row({**r, "topic": "I4", "repair_class": "I4_LEFTOVER_REPAIR"})
		if not dec.get("eligible"):
			continue
		sim = preview_i4_replay(r["item"], r["warehouse"], from_dt=r.get("posting_datetime"))
		queue.append(
			_entry(
				priority_class=2,
				repair_class="I4_LEFTOVER_REPAIR",
				row={**r, **sim, "planner_status": dec.get("planner_status"), "sql_updates": sim.get("sql_updates")},
				reason="READY_I4 — healthy previous + simulation clears residual",
			)
		)

	# 3) Exact zero-rate READY
	zr = scan_zero_rate_rows(company=COMPANY)
	for r in zr.get("rows") or []:
		if (r.get("confidence") or "") != "EXACT":
			continue
		ps = r.get("planner_status") or ""
		if ps not in READY_STATUSES and not r.get("eligible"):
			continue
		queue.append(
			_entry(
				priority_class=3,
				repair_class="ZERO_RATE",
				row=r,
				reason="EXACT zero/lost rate patient zero",
			)
		)

	# Sort: priority_class, posting_datetime, estimated sql
	def sort_key(e):
		return (
			e["priority_class"],
			str(e.get("posting_datetime") or e.get("posting_date") or ""),
			int(e.get("estimated_sql_updates") or 0),
		)

	queue.sort(key=sort_key)
	# Deduplicate by (repair_class, voucher, item, warehouse)
	seen = set()
	unique = []
	for e in queue:
		key = (e["repair_class"], e.get("voucher"), e.get("item"), e.get("warehouse"))
		if key in seen:
			continue
		seen.add(key)
		unique.append(e)

	# Assign priorities 1..N
	for i, e in enumerate(unique[:200], start=1):
		e["priority"] = i
		e["can_repair_now"] = True
		e["groupability"] = "SAFE_GROUP" if e["repair_class"] == "I4_LEFTOVER_REPAIR" else "SAFE_SEQUENTIAL_GROUP"

	top100 = unique[:100]
	groups = _build_groups(top100)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"total_candidates": len(unique),
		"top100": top100,
		"groups": groups,
		"by_class": _count_by(unique[:100], "repair_class"),
	}
	_dump("plan/top100.json", out)
	_dump("plan/groups.json", groups)
	print(
		json.dumps(
			{
				"total_candidates": len(unique),
				"top100_by_class": out["by_class"],
				"groups": len(groups),
				"first10": [
					{
						"p": x["priority"],
						"class": x["repair_class"],
						"voucher": x.get("voucher"),
						"item": x.get("item"),
						"sql": x.get("estimated_sql_updates"),
						"status": x.get("planner_status"),
					}
					for x in top100[:10]
				],
			},
			indent=2,
			default=str,
			ensure_ascii=False,
		)
	)
	return out


def _entry(*, priority_class, repair_class, row, reason):
	voucher = row.get("voucher") or row.get("voucher_no") or row.get("outbound_document") or row.get("document")
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	sql = int(row.get("sql_updates") or row.get("sql_updates_estimate") or row.get("replay_count") or 0)
	replay = int(row.get("replay_count") or row.get("rows") or sql)
	return {
		"priority_class": priority_class,
		"repair_class": repair_class,
		"voucher": voucher,
		"item": item,
		"warehouse": warehouse,
		"batch": row.get("batch") or row.get("batch_no"),
		"sabb": row.get("serial_and_batch_bundle"),
		"work_order": row.get("work_order"),
		"posting_datetime": row.get("posting_datetime") or row.get("posting_date"),
		"patient_zero": (row.get("patient_zero") or {}).get("voucher_no")
		if isinstance(row.get("patient_zero"), dict)
		else row.get("patient_zero") or voucher,
		"previous_healthy": row.get("previous_healthy"),
		"residual_anomaly": row.get("residual_value")
		if row.get("residual_value") is not None
		else row.get("current_value") or row.get("anomaly") or row.get("reason"),
		"planner_status": row.get("planner_status") or row.get("i4_status") or row.get("status"),
		"confidence": row.get("confidence") or "EXACT",
		"required_scope": row.get("required_scope") or "identity",
		"replay_count": replay,
		"estimated_sql_updates": sql or max(1, replay),
		"estimated_runtime_seconds": max(0.05, (replay or sql or 1) * 0.05),
		"dependencies": row.get("required_prerequisite") or row.get("dependency"),
		"downstream_count": len(row.get("touched_vouchers") or []) if isinstance(row.get("touched_vouchers"), list) else None,
		"gl_impact": row.get("affected_gl"),
		"riv_impact": 0,
		"reason": reason,
		"raw": {
			k: row.get(k)
			for k in (
				"sle",
				"posting_datetime",
				"i4_status",
				"stop_before_voucher",
				"stop_reason",
				"inbound_document",
				"outbound_document",
				"proposed_inbound_time",
				"proposed_outbound_time",
			)
			if row.get(k) is not None
		},
	}


def _build_groups(top100):
	# SAFE_GROUP: independent I4 identities (item, warehouse) unique
	i4 = [e for e in top100 if e["repair_class"] == "I4_LEFTOVER_REPAIR"]
	identities = defaultdict(list)
	for e in i4:
		identities[(e["item"], e["warehouse"])].append(e)
	independent = [v[0] for v in identities.values() if len(v) == 1]
	groups = []
	chunk = 5
	for i in range(0, len(independent), chunk):
		part = independent[i : i + chunk]
		gid = f"SAFE-I4-{i // chunk + 1:02d}"
		groups.append(
			{
				"group_id": gid,
				"class": "SAFE_GROUP",
				"repair_roots": [x["voucher"] for x in part],
				"repair_order": [x["voucher"] for x in part],
				"affected_identities": [{"item": x["item"], "warehouse": x["warehouse"]} for x in part],
				"estimated_sle_replay": sum(int(x.get("replay_count") or 0) for x in part),
				"estimated_sql_updates": sum(int(x.get("estimated_sql_updates") or 0) for x in part),
				"estimated_runtime_seconds": round(sum(float(x.get("estimated_runtime_seconds") or 0) for x in part), 2),
				"risk": "LOW",
				"expected_reductions": {"I4 Leftover": len(part), "Patient Zero": len(part)},
				"entries": part,
			}
		)
	# Posting order as sequential singles
	po = [e for e in top100 if e["repair_class"] == "POSTING_ORDER"]
	for i, e in enumerate(po[:20], start=1):
		groups.append(
			{
				"group_id": f"SEQ-PO-{i:02d}",
				"class": "SAFE_SEQUENTIAL_GROUP",
				"repair_roots": [e["voucher"]],
				"repair_order": [e["voucher"]],
				"affected_identities": [{"item": e["item"], "warehouse": e["warehouse"]}],
				"estimated_sle_replay": e.get("replay_count"),
				"estimated_sql_updates": e.get("estimated_sql_updates"),
				"estimated_runtime_seconds": e.get("estimated_runtime_seconds"),
				"risk": "MEDIUM",
				"expected_reductions": {"Posting Order": 1},
				"entries": [e],
			}
		)
	return groups


def _count_by(rows, key):
	out = defaultdict(int)
	for r in rows:
		out[r.get(key) or "?"] += 1
	return dict(out)


def execute_repairs(max_success=MAX_SUCCESS, limit_candidates=100):
	"""Controlled apply loop: READY posting-order first, then gated READY_I4."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		repair_i4_selected,
		scan_i4_leftover,
	)
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	results = {
		"started_at": datetime.utcnow().isoformat() + "Z",
		"successful": [],
		"failed": [],
		"deferred": [],
		"checkpoints": [],
		"success_count": 0,
	}
	baseline_dash = (json.load(open(os.path.join(BASE, "baseline/baseline.json"))) if os.path.exists(os.path.join(BASE, "baseline/baseline.json")) else {}).get("dashboard") or {}

	seen_keys = set()
	execution_order = 0

	# Resume prior progress (idempotent restart)
	progress_path = os.path.join(BASE, "repairs/progress.json")
	if os.path.exists(progress_path):
		prev = json.load(open(progress_path))
		# Drop stop_reason so we can continue after tool fixes
		results["successful"] = [x for x in (prev.get("successful") or []) if x.get("ok")]
		results["failed"] = list(prev.get("failed") or [])
		results["deferred"] = list(prev.get("deferred") or [])
		results["checkpoints"] = list(prev.get("checkpoints") or [])
		results["success_count"] = len(results["successful"])
		execution_order = max(
			[0]
			+ [
				int(x.get("execution_order") or 0)
				for x in results["successful"] + results["failed"] + results["deferred"]
			]
		)
		for s in results["successful"] + results["failed"] + results["deferred"]:
			seen_keys.add((s.get("item"), s.get("warehouse"), s.get("voucher")))
			if s.get("repair_class") == "POSTING_ORDER":
				seen_keys.add(("PO", s.get("voucher"), s.get("item"), s.get("warehouse")))

	# --- Posting Order READY from initial top100 plan ---
	plan_path = os.path.join(BASE, "plan/top100.json")
	if os.path.exists(plan_path):
		plan = json.load(open(plan_path))
		for e in plan.get("top100") or []:
			if results["success_count"] >= max_success:
				break
			if e.get("repair_class") != "POSTING_ORDER":
				continue
			key = ("PO", e.get("voucher"), e.get("item"), e.get("warehouse"))
			if key in seen_keys:
				continue
			execution_order += 1
			entry = _repair_one_posting(e, execution_order)
			seen_keys.add(key)
			if entry.get("ok"):
				results["successful"].append(entry)
				results["success_count"] += 1
			elif entry.get("deferred"):
				results["deferred"].append(entry)
			else:
				results["failed"].append(entry)
			_dump("repairs/progress.json", results)

	# Prefer I4 READY roots — proven class with gate
	rounds = 0
	while results["success_count"] < max_success and rounds < 40:
		rounds += 1
		i4 = scan_i4_leftover(company=COMPANY, from_date=FROM_DATE, to_date=nowdate(), limit=3000)
		ready = []
		for r in i4.get("rows") or []:
			if not (r.get("eligible") or r.get("i4_status") == "READY_I4"):
				continue
			if r.get("previous_healthy") is False or r.get("pz_residual_clears_in_sim") is False:
				continue
			dec = evaluate_row({**r, "topic": "I4", "repair_class": "I4_LEFTOVER_REPAIR"})
			if not dec.get("eligible"):
				continue
			key = (r.get("item"), r.get("warehouse"), r.get("voucher"))
			if key in seen_keys:
				continue
			ready.append(r)
		ready.sort(key=lambda x: str(x.get("posting_datetime") or ""))
		if not ready:
			results["stop_reason"] = "no more READY_I4 roots"
			break

		# Take next independent identity only (avoid same identity twice in parallel)
		batch = []
		used_id = set()
		for r in ready:
			ik = (r.get("item"), r.get("warehouse"))
			if ik in used_id:
				continue
			used_id.add(ik)
			batch.append(r)
			if len(batch) >= 5:
				break

		for r in batch:
			if results["success_count"] >= max_success:
				break
			key = (r.get("item"), r.get("warehouse"), r.get("voucher"))
			seen_keys.add(key)
			execution_order += 1
			entry = _repair_one_i4(r, execution_order)
			if entry.get("ok"):
				results["successful"].append(entry)
				results["success_count"] += 1
			elif entry.get("deferred"):
				results["deferred"].append(entry)
			else:
				results["failed"].append(entry)
				# Hard stop only when apply claimed write but residual remained (false success class)
				if entry.get("structural") and "residual not cleared" in str(entry.get("blocker") or ""):
					results["stop_reason"] = f"structural failure at {entry.get('voucher')}"
					_dump("repairs/progress.json", results)
					return results
				# Other structural/tool errors: defer and continue
				if entry.get("structural"):
					entry["deferred"] = True
					results["deferred"].append(entry)
					results["failed"].pop()

			if results["success_count"] and results["success_count"] % CHECKPOINT_EVERY == 0:
				dash = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
				cp = {
					"after_success": results["success_count"],
					"dashboard": dash.get("dashboard"),
					"delta_vs_baseline": _delta(baseline_dash, dash.get("dashboard") or {}),
					"at": datetime.utcnow().isoformat() + "Z",
				}
				results["checkpoints"].append(cp)
				_dump(f"checkpoints/after_{results['success_count']}.json", cp)
				# Pause if I4 worsened
				b_i4 = int(baseline_dash.get("I4 Leftover") or 0)
				n_i4 = int((dash.get("dashboard") or {}).get("I4 Leftover") or 0)
				if n_i4 > b_i4 + 5:
					results["stop_reason"] = f"I4 worsened {b_i4}→{n_i4}"
					_dump("repairs/progress.json", results)
					return results

		_dump("repairs/progress.json", results)

	results["finished_at"] = datetime.utcnow().isoformat() + "Z"
	final_dash = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
	results["final_dashboard"] = final_dash.get("dashboard")
	results["final_delta"] = _delta(baseline_dash, final_dash.get("dashboard") or {})
	_dump("repairs/final.json", results)
	print(
		json.dumps(
			{
				"success": results["success_count"],
				"failed": len(results["failed"]),
				"deferred": len(results["deferred"]),
				"stop": results.get("stop_reason"),
				"final_delta": results.get("final_delta"),
			},
			indent=2,
			default=str,
		)
	)
	return results


def _repair_one_posting(entry, execution_order):
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs

	voucher = entry.get("voucher")
	item = entry.get("item")
	warehouse = entry.get("warehouse")
	t0 = perf_counter()
	raw = entry.get("raw") or {}
	row = {
		"voucher": voucher,
		"voucher_no": voucher,
		"item": item,
		"item_code": item,
		"warehouse": warehouse,
		"planner_status": entry.get("planner_status"),
		"inbound_document": raw.get("inbound_document"),
		"outbound_document": raw.get("outbound_document"),
		"proposed_inbound_time": raw.get("proposed_inbound_time"),
		"proposed_outbound_time": raw.get("proposed_outbound_time"),
		**{k: v for k, v in (entry.get("raw") or {}).items() if v is not None},
	}
	# Merge full candidate from scan if available via entry fields stored in top100 — use plan raw + entry
	# Re-scan single identity by running full scan filter is expensive; apply with stamped row from plan.
	plan_row = None
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

		scan = run_full_history_scan(company=COMPANY)
		for r in scan.get("rows") or []:
			if (r.get("voucher") or r.get("outbound_document") or r.get("voucher_no")) == voucher and (
				r.get("item") or r.get("item_code")
			) == item:
				plan_row = r
				break
	except Exception as exc:
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "POSTING_ORDER",
			"blocker": f"rescan failed: {exc}",
			"execution_order": execution_order,
		}
	if not plan_row:
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "POSTING_ORDER",
			"blocker": "voucher no longer in posting-order scan",
			"execution_order": execution_order,
		}
	ps = plan_row.get("planner_status") or ""
	if "READY" not in ps:
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "POSTING_ORDER",
			"blocker": f"no longer READY ({ps})",
			"execution_order": execution_order,
		}
	try:
		dry = apply_repairs([plan_row], dry_run=True)
		applied = apply_repairs([plan_row], dry_run=False)
	except Exception as exc:
		frappe.db.rollback()
		return {
			"ok": False,
			"structural": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "POSTING_ORDER",
			"error": str(exc),
			"execution_order": execution_order,
		}
	# Success heuristic: apply did not abort and wrote something
	ok = bool(applied) and not applied.get("aborted") and (
		applied.get("applied") or applied.get("count") or applied.get("status") or applied.get("rows")
	)
	# Prefer explicit repaired markers
	if isinstance(applied, dict):
		if applied.get("error") or applied.get("aborted"):
			ok = False
		applied_rows = applied.get("applied") or applied.get("rows") or []
		if applied_rows and all(
			(r.get("status") in ("BLOCKED", "FAILED", "ERROR") if isinstance(r, dict) else False) for r in applied_rows
		):
			ok = False
		if applied.get("written") or applied.get("sql_updates_executed") or any(
			isinstance(r, dict) and r.get("written") for r in applied_rows
		):
			ok = True
	return {
		"ok": bool(ok),
		"deferred": not ok,
		"voucher": voucher,
		"item": item,
		"warehouse": warehouse,
		"repair_class": "POSTING_ORDER",
		"execution_order": execution_order,
		"runtime_seconds": round(perf_counter() - t0, 3),
		"dry_run": {k: dry.get(k) for k in ("status", "count", "dry_run") if isinstance(dry, dict)},
		"apply": {
			k: applied.get(k)
			for k in ("status", "count", "aborted", "sql_updates_executed", "repair_run_id", "written")
			if isinstance(applied, dict) and k in applied
		},
		"planner_status_before": ps,
		"blocker": None if ok else (applied.get("error") or applied.get("reason") or "posting-order apply inconclusive"),
	}


def _repair_one_i4(row, execution_order):
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		repair_i4_selected,
	)

	item = row.get("item")
	warehouse = row.get("warehouse")
	voucher = row.get("voucher")
	t0 = perf_counter()
	before = classify_i4_row(item, warehouse, voucher=voucher, sle_name=row.get("sle"))
	residual_before = flt(before.get("residual_value") or before.get("current_value") or 0)
	# Dry run
	dry = repair_i4_selected([before], dry_run=True)
	# Confirm still READY
	if before.get("i4_status") != "READY_I4" and not before.get("eligible"):
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"blocker": before.get("message") or before.get("i4_status"),
			"execution_order": execution_order,
		}
	# Verify sim clears
	if before.get("pz_residual_clears_in_sim") is False:
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"blocker": "simulation does not clear residual",
			"execution_order": execution_order,
		}
	try:
		applied = repair_i4_selected([before], dry_run=False)
	except Exception as exc:
		frappe.db.rollback()
		return {
			"ok": False,
			"structural": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"error": str(exc),
			"execution_order": execution_order,
		}
	if applied.get("aborted"):
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"blocker": applied.get("reason"),
			"blocked": applied.get("blocked"),
			"execution_order": execution_order,
		}
	after = classify_i4_row(item, warehouse, voucher=voucher, sle_name=row.get("sle"))
	residual_after = flt(after.get("residual_value") or after.get("current_value") or 0)
	cleared = abs(residual_after) <= 1 and (
		after.get("i4_status") == "I4_REPAIRED" or abs(flt(after.get("qty_after") or 0)) <= 0.0001
	)
	# Also accept if no longer I4 leftover on this SLE
	if not cleared and abs(residual_before) > 1 and abs(residual_after) <= 1:
		cleared = True
	a0 = (applied.get("applied") or [{}])[0]
	entry = {
		"ok": bool(cleared),
		"voucher": voucher,
		"item": item,
		"warehouse": warehouse,
		"repair_class": "I4_LEFTOVER_REPAIR",
		"execution_order": execution_order,
		"residual_before": residual_before,
		"residual_after": residual_after,
		"i4_before": before.get("i4_status"),
		"i4_after": after.get("i4_status"),
		"sql_updates": applied.get("sql_updates_executed"),
		"replay_rows": a0.get("rows"),
		"runtime_seconds": round(perf_counter() - t0, 3),
		"repair_run_id": applied.get("repair_run_id"),
		"gl": a0.get("gl"),
		"bin_final_qty": a0.get("final_qty"),
		"bin_final_value": a0.get("final_value"),
		"dry_run_sql": (dry.get("rows") or [{}])[0].get("sql_updates") if isinstance(dry, dict) else None,
	}
	if not cleared:
		entry["deferred"] = False
		entry["structural"] = True
		entry["blocker"] = f"residual not cleared ({residual_before} → {residual_after})"
	return entry


def _delta(before, after):
	keys = [
		"I4 Leftover",
		"Patient Zero",
		"Broken Bin",
		"Waiting Downstream Bin",
		"Broken GL",
		"Wrong Rate",
		"Zero Rate",
		"Failed RIV",
		"Broken SABB",
		"Repairable",
		"Manual",
		"Ambiguous",
		"Integrity Score",
	]
	out = {}
	for k in keys:
		b = before.get(k)
		a = after.get(k)
		if b is None and a is None:
			continue
		try:
			out[k] = {"before": b, "after": a, "delta": (a or 0) - (b or 0)}
		except Exception:
			out[k] = {"before": b, "after": a}
	return out


def run_phase1():
	return collect_baseline()


def run_phase2():
	return build_top100()


def run_all():
	b = collect_baseline()
	p = build_top100()
	r = execute_repairs(max_success=MAX_SUCCESS)
	return {"baseline": b, "plan_summary": {"total": p.get("total_candidates"), "by_class": p.get("by_class")}, "repairs": {
		"success": r.get("success_count"),
		"failed": len(r.get("failed") or []),
		"deferred": len(r.get("deferred") or []),
		"stop": r.get("stop_reason"),
		"final_delta": r.get("final_delta"),
	}}
