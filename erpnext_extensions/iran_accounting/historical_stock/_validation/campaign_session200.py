# Copyright (c) 2026 — session-200 campaign (dev only).
"""Next-200 Historical Repair roots on restored development DB."""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from time import perf_counter

import frappe
from frappe.utils import flt, nowdate

# Reuse helpers from the first campaign module.
from erpnext_extensions.iran_accounting.historical_stock._validation import campaign_restore_100 as c100

BASE = "/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/restore_20260915_133438/session200"
COMPANY = c100.COMPANY
FROM_DATE = c100.FROM_DATE
MAX_SUCCESS = 200
CHECKPOINT_EVERY = 10


def _dump(name, data):
	path = os.path.join(BASE, name)
	os.makedirs(os.path.dirname(path) or BASE, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def collect_pre_baseline():
	# Point c100 dumps into session200 for this call
	old = c100.BASE
	c100.BASE = BASE
	try:
		out = c100.collect_baseline()
		# also store under explicit name
		_dump("baseline/pre200_baseline.json", out)
		return out
	finally:
		c100.BASE = old


def build_next200():
	"""Dynamic queue: PO batch-scoped → READY_I4 → EXACT zero → EXACT wrong-rate."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		preview_i4_replay,
		scan_i4_leftover,
	)
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES, evaluate_row
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	queue = []

	posting = run_full_history_scan(company=COMPANY)
	for r in posting.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if "READY_BATCH_SCOPED" not in ps and ps not in READY_STATUSES and not r.get("eligible"):
			continue
		if "READY" not in ps and not r.get("eligible"):
			continue
		queue.append(
			c100._entry(
				priority_class=1,
				repair_class="POSTING_ORDER",
				row=r,
				reason="READY posting-order / batch-scoped",
			)
		)

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
			c100._entry(
				priority_class=2,
				repair_class="I4_LEFTOVER_REPAIR",
				row={**r, **sim, "planner_status": dec.get("planner_status"), "sql_updates": sim.get("sql_updates")},
				reason="READY_I4 — healthy previous + simulation clears residual",
			)
		)

	zr = scan_zero_rate_rows(company=COMPANY)
	for r in zr.get("rows") or []:
		if (r.get("confidence") or "") != "EXACT":
			continue
		ps = str(r.get("planner_status") or "")
		if "READY" not in ps and not r.get("eligible"):
			continue
		queue.append(
			{
				**c100._entry(
					priority_class=3,
					repair_class="ZERO_RATE",
					row=r,
					reason="EXACT zero/lost rate patient zero",
				),
				"idx": r.get("idx"),
				"voucher_detail": r.get("voucher_detail") or r.get("name"),
				"s_warehouse": r.get("s_warehouse"),
				"t_warehouse": r.get("t_warehouse"),
			}
		)

	wr = scan_wrong_rates(company=COMPANY, limit=2000)
	for r in wr.get("rows") or []:
		if (r.get("confidence") or "") != "EXACT":
			continue
		ps = str(r.get("planner_status") or "")
		if "READY" not in ps and not r.get("eligible"):
			continue
		queue.append(
			{
				**c100._entry(
					priority_class=4,
					repair_class="WRONG_RATE",
					row=r,
					reason="EXACT wrong-rate patient zero",
				),
				"idx": r.get("idx"),
				"voucher_detail": r.get("voucher_detail") or r.get("name"),
			}
		)

	queue.sort(
		key=lambda e: (
			e["priority_class"],
			str(e.get("posting_datetime") or e.get("posting_date") or ""),
			int(e.get("estimated_sql_updates") or 0),
		)
	)
	seen = set()
	unique = []
	for e in queue:
		key = (e["repair_class"], e.get("voucher"), e.get("item"), e.get("warehouse"))
		if key in seen:
			continue
		seen.add(key)
		unique.append(e)

	for i, e in enumerate(unique[:250], start=1):
		e["priority"] = i
		e["can_repair_now"] = True
		e["groupability"] = (
			"SAFE_GROUP" if e["repair_class"] == "I4_LEFTOVER_REPAIR" else "SAFE_SEQUENTIAL_GROUP"
		)

	top200 = unique[:200]
	groups = c100._build_groups(top200)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"total_candidates": len(unique),
		"next200": top200,
		"groups": groups,
		"by_class": dict(Counter(e["repair_class"] for e in top200)),
	}
	_dump("plan/next200.json", out)
	_dump("plan/groups.json", groups)
	print(
		json.dumps(
			{
				"total_candidates": len(unique),
				"next200_by_class": out["by_class"],
				"groups": len(groups),
				"first15": [
					{
						"p": x["priority"],
						"class": x["repair_class"],
						"voucher": x.get("voucher"),
						"item": x.get("item"),
						"sql": x.get("estimated_sql_updates"),
						"status": x.get("planner_status"),
						"residual": x.get("residual_anomaly"),
					}
					for x in top200[:15]
				],
			},
			indent=2,
			default=str,
			ensure_ascii=False,
		)
	)
	return out


def execute_next200(max_success=MAX_SUCCESS):
	"""Up to 200 successful roots; I4 + proven PO; optional single-proof new classes."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	results = {
		"started_at": datetime.utcnow().isoformat() + "Z",
		"successful": [],
		"failed": [],
		"deferred": [],
		"checkpoints": [],
		"success_count": 0,
		"tool_changes": [],
		"class_proofs": {},
	}
	baseline_path = os.path.join(BASE, "baseline/pre200_baseline.json")
	if not os.path.exists(baseline_path):
		baseline_path = os.path.join(BASE, "baseline/baseline.json")
	baseline_dash = (json.load(open(baseline_path)).get("dashboard") if os.path.exists(baseline_path) else {}) or {}

	seen_keys = set()
	execution_order = 0

	progress_path = os.path.join(BASE, "repairs/progress.json")
	if os.path.exists(progress_path):
		prev = json.load(open(progress_path))
		results["successful"] = [x for x in (prev.get("successful") or []) if x.get("ok")]
		results["failed"] = list(prev.get("failed") or [])
		results["deferred"] = list(prev.get("deferred") or [])
		results["checkpoints"] = list(prev.get("checkpoints") or [])
		results["success_count"] = len(results["successful"])
		results["class_proofs"] = prev.get("class_proofs") or {}
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

	# --- Posting Order from next200 plan ---
	plan_path = os.path.join(BASE, "plan/next200.json")
	if os.path.exists(plan_path):
		plan = json.load(open(plan_path))
		for e in plan.get("next200") or []:
			if results["success_count"] >= max_success:
				break
			if e.get("repair_class") != "POSTING_ORDER":
				continue
			key = ("PO", e.get("voucher"), e.get("item"), e.get("warehouse"))
			if key in seen_keys:
				continue
			execution_order += 1
			entry = c100._repair_one_posting(e, execution_order)
			entry["initial_priority"] = e.get("priority")
			seen_keys.add(key)
			_record(results, entry)
			_dump("repairs/progress.json", results)

	# --- I4 dynamic loop ---
	rounds = 0
	while results["success_count"] < max_success and rounds < 60:
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
			break

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
			entry = c100._repair_one_i4(r, execution_order)
			entry["initial_priority"] = None
			_record(results, entry)
			if entry.get("structural") and "residual not cleared" in str(entry.get("blocker") or ""):
				results["stop_reason"] = f"false-success/residual at {entry.get('voucher')}"
				_dump("repairs/progress.json", results)
				return _finish(results, baseline_dash)

			if results["success_count"] and results["success_count"] % CHECKPOINT_EVERY == 0:
				_checkpoint(results, baseline_dash)
				# material worsen: I4 up by >5 vs session baseline
				b_i4 = int(baseline_dash.get("I4 Leftover") or 0)
				last = results["checkpoints"][-1]
				n_i4 = int((last.get("dashboard") or {}).get("I4 Leftover") or 0)
				if n_i4 > b_i4 + 5:
					results["stop_reason"] = f"I4 worsened {b_i4}→{n_i4}"
					_dump("repairs/progress.json", results)
					return _finish(results, baseline_dash)

		_dump("repairs/progress.json", results)

	# If I4 exhausted and still under max — optional single-proof ZERO_RATE then continue limited
	if results["success_count"] < max_success and os.path.exists(plan_path):
		plan = json.load(open(plan_path))
		zero_candidates = [e for e in (plan.get("next200") or []) if e.get("repair_class") == "ZERO_RATE"]
		if zero_candidates and not results["class_proofs"].get("ZERO_RATE"):
			try:
				proof = _proof_zero_rate(zero_candidates[0], execution_order + 1)
			except Exception as exc:
				proof = {
					"ok": False,
					"deferred": True,
					"repair_class": "ZERO_RATE",
					"voucher": zero_candidates[0].get("voucher"),
					"blocker": f"proof crashed: {exc}",
					"execution_order": execution_order + 1,
					"proof": True,
				}
			execution_order += 1
			results["class_proofs"]["ZERO_RATE"] = proof
			_record(results, proof)
			_dump("repairs/progress.json", results)
			if proof.get("ok"):
				# continue a few more exact zero rates (cap 20) after proof
				for e in zero_candidates[1:21]:
					if results["success_count"] >= max_success:
						break
					key = (e.get("item"), e.get("warehouse"), e.get("voucher"))
					if key in seen_keys:
						continue
					seen_keys.add(key)
					execution_order += 1
					try:
						entry = _repair_one_zero(e, execution_order)
					except Exception as exc:
						entry = {
							"ok": False,
							"deferred": True,
							"voucher": e.get("voucher"),
							"item": e.get("item"),
							"warehouse": e.get("warehouse"),
							"repair_class": "ZERO_RATE",
							"blocker": str(exc),
							"execution_order": execution_order,
						}
					entry["initial_priority"] = e.get("priority")
					_record(results, entry)
					if results["success_count"] and results["success_count"] % CHECKPOINT_EVERY == 0:
						_checkpoint(results, baseline_dash)
					_dump("repairs/progress.json", results)

	if results["success_count"] < max_success and not results.get("stop_reason"):
		results["stop_reason"] = "no more READY roots in proven classes"

	return _finish(results, baseline_dash)


def _record(results, entry):
	if entry.get("ok"):
		results["successful"].append(entry)
		results["success_count"] = len(results["successful"])
	elif entry.get("deferred") or (entry.get("structural") and "residual not cleared" not in str(entry.get("blocker") or "")):
		entry["deferred"] = True
		results["deferred"].append(entry)
	else:
		results["failed"].append(entry)


def _checkpoint(results, baseline_dash):
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	dash = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
	cp = {
		"after_success": results["success_count"],
		"dashboard": dash.get("dashboard"),
		"delta_vs_session_baseline": c100._delta(baseline_dash, dash.get("dashboard") or {}),
		"at": datetime.utcnow().isoformat() + "Z",
	}
	results["checkpoints"].append(cp)
	_dump(f"checkpoints/after_{results['success_count']}.json", cp)


def _finish(results, baseline_dash):
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	results["finished_at"] = datetime.utcnow().isoformat() + "Z"
	final_dash = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
	results["final_dashboard"] = final_dash.get("dashboard")
	results["final_delta"] = c100._delta(baseline_dash, final_dash.get("dashboard") or {})
	_dump("repairs/final.json", results)
	slim = [
		{
			k: x.get(k)
			for k in (
				"execution_order",
				"initial_priority",
				"voucher",
				"item",
				"warehouse",
				"repair_class",
				"residual_before",
				"residual_after",
				"sql_updates",
				"replay_rows",
				"runtime_seconds",
				"repair_run_id",
				"i4_before",
				"i4_after",
			)
			if k in x
		}
		for x in results["successful"]
	]
	_dump("repairs/successful_slim.json", slim)
	print(
		json.dumps(
			{
				"success": results["success_count"],
				"failed": len(results["failed"]),
				"deferred": len(results["deferred"]),
				"stop": results.get("stop_reason"),
				"by_class": dict(Counter(x.get("repair_class") for x in results["successful"])),
				"final_delta": results.get("final_delta"),
			},
			indent=2,
			default=str,
			ensure_ascii=False,
		)
	)
	return results


def _proof_zero_rate(entry, execution_order):
	return _repair_one_zero(entry, execution_order, proof=True)


def _repair_one_zero(entry, execution_order, proof=False):
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row
	from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
		_load_detail,
		repair_zero_rate_selected,
	)

	voucher = entry.get("voucher")
	item = entry.get("item")
	warehouse = entry.get("warehouse")
	t0 = perf_counter()
	try:
		detail = _load_detail(
			{
				"voucher": voucher,
				"item": item,
				"warehouse": warehouse,
				"batch": entry.get("batch"),
				"serial_and_batch_bundle": entry.get("sabb"),
				"idx": entry.get("idx"),
				"voucher_detail": entry.get("voucher_detail"),
				"name": entry.get("voucher_detail"),
			}
		)
		before = classify_zero_row(detail)
	except Exception as exc:
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "ZERO_RATE",
			"blocker": f"classify failed: {exc}",
			"execution_order": execution_order,
			"proof": proof,
		}
	ps = str(before.get("planner_status") or "")
	if "READY" not in ps and not before.get("eligible"):
		return {
			"ok": False,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "ZERO_RATE",
			"blocker": f"not READY ({ps})",
			"execution_order": execution_order,
			"proof": proof,
		}
	try:
		dry = repair_zero_rate_selected([before], dry_run=True)
		applied = repair_zero_rate_selected([before], dry_run=False)
	except Exception as exc:
		frappe.db.rollback()
		return {
			"ok": False,
			"structural": True,
			"deferred": True,
			"voucher": voucher,
			"item": item,
			"warehouse": warehouse,
			"repair_class": "ZERO_RATE",
			"error": str(exc),
			"execution_order": execution_order,
			"proof": proof,
		}
	ok = bool(applied) and not applied.get("aborted")
	# Reclassify
	try:
		after = classify_zero_row(
			_load_detail(
				{
					"voucher": voucher,
					"item": item,
					"warehouse": warehouse,
					"idx": entry.get("idx"),
					"voucher_detail": entry.get("voucher_detail") or before.get("voucher_detail"),
					"name": entry.get("voucher_detail") or before.get("voucher_detail"),
				}
			)
		)
		# success if no longer EXACT zero-rate eligible / repaired
		if after.get("confidence") == "EXACT" and after.get("eligible"):
			ok = False
	except Exception:
		after = {}
	return {
		"ok": bool(ok),
		"deferred": not ok,
		"voucher": voucher,
		"item": item,
		"warehouse": warehouse,
		"repair_class": "ZERO_RATE",
		"execution_order": execution_order,
		"runtime_seconds": round(perf_counter() - t0, 3),
		"sql_updates": applied.get("sql_updates_executed") if isinstance(applied, dict) else None,
		"repair_run_id": applied.get("repair_run_id") if isinstance(applied, dict) else None,
		"before_status": before.get("planner_status") or before.get("status"),
		"after_status": after.get("planner_status") or after.get("status"),
		"proof": proof,
		"blocker": None if ok else "zero-rate defect may remain",
	}


def run_session200():
	b = collect_pre_baseline()
	p = build_next200()
	r = execute_next200(max_success=MAX_SUCCESS)
	return {
		"baseline_i4": (b.get("dashboard") or {}).get("I4 Leftover"),
		"plan_candidates": p.get("total_candidates"),
		"plan_by_class": p.get("by_class"),
		"success": r.get("success_count"),
		"stop": r.get("stop_reason"),
		"final_delta": r.get("final_delta"),
	}
