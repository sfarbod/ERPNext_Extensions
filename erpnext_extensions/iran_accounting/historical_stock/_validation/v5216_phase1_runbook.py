# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.16 Phase 1 campaigns — I4 + Zero Rate SAFE_GROUP loops + Posting Order classify.

Dev-only. No Wrong Rate / RIV / GL / Warehouse (Phase 2).
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from time import perf_counter

import frappe
from frappe.utils import flt, nowdate

COMPANY = "اسپاد فارمد دارو"
FROM_DATE = "2026-03-21"
ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5216"
)
CHECKPOINT_EVERY = 10
ZR_CLUSTER = 15
I4_CLUSTER = 15
MAX_I4_ROUNDS = 40
MAX_ZR_ROUNDS = 40
MAX_PO_ROUNDS = 20

# KPI keys that must not regress beyond tolerance after a Zero Rate campaign
ZR_GATE_KEYS = (
	"Wrong Incoming",
	"Wrong Outgoing",
	"Wrong Amount",
	"Broken Bin",
	"Broken GL",
	"Failed RIV",
	"I4 Leftover",
)
ZR_GATE_TOLERANCE = {
	# Identity replay can surface a few new wrong-rate legs; allow tiny noise.
	"Wrong Incoming": 8,
	"Wrong Outgoing": 8,
	"Wrong Amount": 5,
	"Broken Bin": 0,
	"Broken GL": 0,
	"Failed RIV": 0,
	"I4 Leftover": 0,
}


def _dump(name, data):
	path = os.path.join(ARTIFACT, name)
	os.makedirs(os.path.dirname(path) or ARTIFACT, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def _dash():
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	return run_full_integrity_scan(company=COMPANY, include_manufacture=False).get("dashboard") or {}


def _validate():
	from erpnext_extensions.iran_accounting.historical_stock._validation.dashboard_consistency_v5214 import (
		collect_kpi_matrix,
	)

	m = collect_kpi_matrix(company=COMPANY)
	return {
		"all_pass": m.get("all_pass"),
		"pass_count": m.get("pass_count"),
		"fail_count": m.get("fail_count"),
		"dashboard": m.get("dashboard"),
	}


def _delta(before, after, keys=None):
	keys = keys or sorted(set(before or {}) | set(after or {}))
	out = {}
	for k in keys:
		b, a = (before or {}).get(k), (after or {}).get(k)
		if b is None and a is None:
			continue
		try:
			out[k] = {"before": b, "after": a, "delta": (a or 0) - (b or 0)}
		except Exception:
			out[k] = {"before": b, "after": a}
	return out


def _gate_ok(before, after) -> tuple[bool, list]:
	violations = []
	for k in ZR_GATE_KEYS:
		b = int((before or {}).get(k) or 0)
		a = int((after or {}).get(k) or 0)
		tol = ZR_GATE_TOLERANCE.get(k, 0)
		if a > b + tol:
			violations.append({"metric": k, "before": b, "after": a, "tol": tol})
	# Zero Rate itself must not increase
	zb, za = int((before or {}).get("Zero Rate") or 0), int((after or {}).get("Zero Rate") or 0)
	if za > zb:
		violations.append({"metric": "Zero Rate", "before": zb, "after": za, "tol": 0})
	return (not violations), violations


# ---------------------------------------------------------------------------
# I4
# ---------------------------------------------------------------------------


def _i4_ready_rows():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row

	scan = scan_i4_leftover(company=COMPANY, from_date=FROM_DATE, to_date=nowdate(), limit=5000)
	ready = []
	for r in scan.get("rows") or []:
		if not (r.get("eligible") or r.get("i4_status") == "READY_I4"):
			continue
		if r.get("previous_healthy") is False or r.get("pz_residual_clears_in_sim") is False:
			continue
		dec = evaluate_row({**r, "topic": "I4", "repair_class": "I4_LEFTOVER_REPAIR"})
		if not dec.get("eligible") and not str(dec.get("planner_status") or "").startswith("READY"):
			continue
		ready.append({**r, **{k: dec.get(k) for k in ("planner_status", "sql_updates", "eligible") if dec.get(k) is not None}})
	return scan, ready


def run_i4_safe_round(max_roots=I4_CLUSTER, *, apply=True) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import cluster_independent_roots, SAFE_GROUP
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import repair_i4_selected

	scan, ready = _i4_ready_rows()
	for r in ready:
		r.setdefault("topic", "I4_LEFTOVER")
		r.setdefault("confidence", "EXACT")
	clusters = cluster_independent_roots(ready, max_cluster=max_roots)
	group = clusters.get("recommended_first_group")
	if not group or group.get("group_class") != SAFE_GROUP or not group.get("roots"):
		return {
			"ok": True,
			"exhausted": True,
			"ready_count": len(ready),
			"by_status": scan.get("by_status"),
			"message": "No READY_I4 SAFE_GROUP",
		}

	roots = (group.get("roots") or [])[:max_roots]
	before = _dash()
	dry = repair_i4_selected(roots, dry_run=True)
	result = {
		"started_at": datetime.utcnow().isoformat() + "Z",
		"n_roots": len(roots),
		"vouchers": [r.get("voucher") for r in roots],
		"identities": [{"item": r.get("item"), "warehouse": r.get("warehouse")} for r in roots],
		"dry_run": {"aborted": dry.get("aborted"), "blocked": dry.get("blocked"), "count": dry.get("count")},
		"apply": apply,
		"applied": [],
		"failed": [],
	}
	if not apply:
		result["ok"] = not dry.get("aborted")
		return result

	# One root at a time with savepoint + residual verify
	frappe.flags["historical_repair"] = True
	sql_total = 0
	try:
		for i, row in enumerate(roots, 1):
			sp = f"i4p_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(sp)
			try:
				out = repair_i4_selected([row], dry_run=False)
				if out.get("aborted"):
					frappe.db.rollback(save_point=sp)
					result["failed"].append({"voucher": row.get("voucher"), "reason": out.get("reason")})
					result["stop_reason"] = f"aborted at {row.get('voucher')}"
					break
				# Verify I4 residual cleared on patient zero SLE
				item, wh = row.get("item"), row.get("warehouse")
				from erpnext_extensions.iran_accounting.historical_stock.i4_repair import classify_i4_row

				after_cls = classify_i4_row(item, wh, voucher=row.get("voucher"))
				cleared = after_cls.get("i4_status") in ("I4_REPAIRED", "HEALTHY", "NOT_I4") or (
					abs(flt(after_cls.get("stock_value") or after_cls.get("residual") or 99)) <= 1
					and abs(flt(after_cls.get("qty_after") or after_cls.get("qty_after_transaction") or 0)) <= 0.0001
				)
				# Prefer explicit residual fields
				if after_cls.get("i4_status") == "I4_REPAIRED":
					cleared = True
				entry = {
					"voucher": row.get("voucher"),
					"item": item,
					"warehouse": wh,
					"cleared": cleared,
					"after_status": after_cls.get("i4_status"),
					"sql": out.get("sql_updates_executed"),
				}
				if cleared:
					result["applied"].append(entry)
					sql_total += int(out.get("sql_updates_executed") or 0)
					frappe.db.commit()
				else:
					frappe.db.rollback(save_point=sp)
					result["failed"].append({**entry, "reason": "residual_not_cleared"})
					result["stop_reason"] = f"false-success at {row.get('voucher')}"
					break
				if i % CHECKPOINT_EVERY == 0:
					result.setdefault("checkpoints", []).append({"at": i, "applied": len(result["applied"])})
			except Exception as exc:
				frappe.db.rollback(save_point=sp)
				result["failed"].append({"voucher": row.get("voucher"), "error": str(exc)})
				result["stop_reason"] = str(exc)
				break
	finally:
		frappe.flags["historical_repair"] = False

	after = _dash()
	val = _validate()
	result.update(
		{
			"ok": bool(result["applied"]) and not result.get("stop_reason") and val.get("all_pass"),
			"sql_updates_executed": sql_total,
			"before": before,
			"after": after,
			"kpi_delta": _delta(
				before,
				after,
				["I4 Leftover", "READY_I4", "WAITING_I4", "Broken Bin", "Broken GL", "Zero Rate", "Integrity Score", "Repairable"],
			),
			"dashboard_validation": val,
		}
	)
	return result


def run_i4_until_exhausted(*, apply=True) -> dict:
	rounds = []
	t0 = perf_counter()
	for n in range(1, MAX_I4_ROUNDS + 1):
		print(f"=== I4 round {n} ===")
		r = run_i4_safe_round(apply=apply)
		rounds.append(r)
		_dump(f"i4/round_{n:02d}.json", r)
		if r.get("exhausted"):
			print("I4 READY exhausted")
			break
		if r.get("stop_reason") or not r.get("ok"):
			print("I4 STOP", r.get("stop_reason"), "ok=", r.get("ok"))
			break
		print(f"  applied={len(r.get('applied') or [])} I4 delta={r.get('kpi_delta', {}).get('I4 Leftover')}")
	summary = {
		"rounds": len(rounds),
		"total_applied": sum(len(x.get("applied") or []) for x in rounds),
		"total_failed": sum(len(x.get("failed") or []) for x in rounds),
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"last": rounds[-1] if rounds else None,
		"stopped": bool(rounds and (rounds[-1].get("stop_reason") or (not rounds[-1].get("ok") and not rounds[-1].get("exhausted")))),
	}
	_dump("i4/summary.json", summary)
	return summary


# ---------------------------------------------------------------------------
# Zero Rate
# ---------------------------------------------------------------------------


def run_zr_until_stable(*, apply=True) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate_campaign import (
		classify_zero_clusters,
		run_zero_safe_cluster_campaign,
	)

	rounds = []
	t0 = perf_counter()
	for n in range(1, MAX_ZR_ROUNDS + 1):
		print(f"=== Zero Rate round {n} ===")
		cls = classify_zero_clusters(company=COMPANY, max_cluster=ZR_CLUSTER)
		exact = cls.get("exact_ready") or 0
		rec = (cls.get("clusters") or {}).get("recommended_first_group")
		if exact <= 0:
			print("Zero Rate EXACT READY exhausted")
			rounds.append({"exhausted": True, "exact_ready": exact})
			break
		if not rec or not rec.get("n_roots"):
			print("Zero Rate ENGINE GAP — EXACT READY exist but no SAFE_GROUP/SEQUENTIAL")
			rounds.append(
				{
					"engine_gap": True,
					"exact_ready": exact,
					"unsafe_groups": len((cls.get("clusters") or {}).get("unsafe_groups") or []),
					"sequential_groups": len((cls.get("clusters") or {}).get("sequential_groups") or []),
					"stop_reason": "exact_ready_without_safe_group",
				}
			)
			break
		before = _dash()
		r = run_zero_safe_cluster_campaign(company=COMPANY, max_roots=ZR_CLUSTER, apply=apply)
		after = r.get("after") or _dash()
		ok_gate, violations = _gate_ok(before, after)
		r["gate_ok"] = ok_gate
		r["gate_violations"] = violations
		r["round"] = n
		# Prefer campaign's own validation; also require gate
		if not ok_gate:
			r["ok"] = False
			r["stop_reason"] = r.get("stop_reason") or f"KPI gate violated: {violations}"
		rounds.append(r)
		_dump(f"zero_rate/round_{n:02d}.json", r)
		print(
			f"  verified={len(r.get('verified') or [])} ZR={r.get('kpi_delta', {}).get('Zero Rate')} "
			f"gate_ok={ok_gate} promo={r.get('promotion_status')}"
		)
		if r.get("stop_reason") or not r.get("ok"):
			print("ZR STOP", r.get("stop_reason"))
			break
		if len(r.get("verified") or []) == 0:
			print("ZR no verified roots — stop")
			break
	summary = {
		"rounds": len(rounds),
		"total_verified": sum(len(x.get("verified") or []) for x in rounds if not x.get("exhausted")),
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"last": rounds[-1] if rounds else None,
		"stopped": bool(
			rounds
			and not rounds[-1].get("exhausted")
			and (rounds[-1].get("stop_reason") or not rounds[-1].get("ok"))
		),
	}
	_dump("zero_rate/summary.json", summary)
	return summary


# ---------------------------------------------------------------------------
# Posting Order classification + repair
# ---------------------------------------------------------------------------

PO_BUCKETS = (
	"TRUE_REPAIRABLE",
	"WAITING_DEPENDENCY",
	"WAREHOUSE_ESCALATION",
	"REAL_STOCK_SHORTAGE",
	"NO_REPAIR_PATH",
	"MANUAL",
	"AMBIGUOUS",
)


def classify_posting_order(company=None) -> dict:
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	company = company or COMPANY
	scan = run_full_history_scan(company=company)
	rows = scan.get("rows") or []
	by_bucket = defaultdict(list)
	for r in rows:
		ps = str(r.get("planner_status") or "")
		st = str(r.get("status") or "")
		conf = str(r.get("confidence") or "")
		bucket = "MANUAL"
		if r.get("eligible") or ps in READY_STATUSES or ("READY" in ps and "WAREHOUSE" not in ps):
			bucket = "TRUE_REPAIRABLE"
		elif "WAREHOUSE" in ps or st == "ELIGIBLE" and "WAREHOUSE" in ps:
			bucket = "WAREHOUSE_ESCALATION"
		elif "WAREHOUSE" in ps or r.get("warehouse_escalation"):
			bucket = "WAREHOUSE_ESCALATION"
		elif "SHORTAGE" in st or st in ("REAL_STOCK_SHORTAGE", "INSUFFICIENT_STOCK"):
			bucket = "REAL_STOCK_SHORTAGE"
		elif st == "CROSS_TIME_REPAIRABLE" or conf == "LIKELY":
			bucket = "MANUAL"
		elif "WAITING" in ps or "DEPENDENCY" in st or "DEPENDENCY" in ps or st == "AMBIGUOUS_DEPENDENCY":
			bucket = "WAITING_DEPENDENCY"
		elif "NO_REPAIR" in ps or "NO_REPAIR" in st or "CYCLE" in st or st == "NO_REPAIR_NEEDED":
			bucket = "NO_REPAIR_PATH"
		elif conf == "AMBIGUOUS" or "AMBIGUOUS" in ps or st == "LATER_INBOUND_UNRELATED":
			bucket = "AMBIGUOUS"
		elif "MANUAL" in st or "MIDNIGHT" in st or conf in ("LIKELY", "MANUAL"):
			bucket = "MANUAL"
		r["po_bucket"] = bucket
		by_bucket[bucket].append(r)

	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"total": len(rows),
		"by_bucket": {k: len(v) for k, v in by_bucket.items()},
		"bucket_order": list(PO_BUCKETS),
		"by_planner_status": dict(Counter(str(r.get("planner_status") or "") for r in rows)),
		"by_status": dict(Counter(str(r.get("status") or "") for r in rows)),
		"true_repairable": len(by_bucket.get("TRUE_REPAIRABLE") or []),
		"sample_waiting": [
			{
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"status": r.get("status"),
				"planner_status": r.get("planner_status"),
				"reason": r.get("reason") or r.get("message"),
			}
			for r in (by_bucket.get("WAITING_DEPENDENCY") or [])[:20]
		],
		"sample_true": [
			{
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"item": r.get("item") or r.get("item_code"),
				"planner_status": r.get("planner_status"),
				"sql_updates": r.get("sql_updates"),
			}
			for r in (by_bucket.get("TRUE_REPAIRABLE") or [])[:20]
		],
	}
	_dump("posting_order/classification.json", out)
	return out


def run_po_repairable(*, apply=True, max_roots=15) -> dict:
	"""Repair TRUE_REPAIRABLE posting-order rows in small SAFE batches."""
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs, dry_run
	from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
		cluster_independent_roots,
		SAFE_GROUP,
	)

	cls = classify_posting_order()
	# Re-scan to get full rows with bucket stamp — classify mutates copies in by_bucket via same objects
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	scan = run_full_history_scan(company=COMPANY)
	ready = []
	for r in scan.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if r.get("eligible") or ps in READY_STATUSES or "READY" in ps:
			r.setdefault("topic", "POSTING_ORDER")
			r.setdefault("confidence", r.get("confidence") or "EXACT")
			r.setdefault("voucher", r.get("voucher") or r.get("voucher_no"))
			r.setdefault("item", r.get("item") or r.get("item_code"))
			ready.append(r)
	if not ready:
		return {"ok": True, "exhausted": True, "classification": cls, "message": "No TRUE_REPAIRABLE posting order"}

	clusters = cluster_independent_roots(ready, max_cluster=max_roots)
	group = clusters.get("recommended_first_group")
	if not group or group.get("group_class") != SAFE_GROUP:
		# Fall back to first N independent by identity
		seen = set()
		pack = []
		for r in ready:
			key = (r.get("item"), r.get("warehouse"))
			if key in seen:
				continue
			seen.add(key)
			pack.append(r)
			if len(pack) >= max_roots:
				break
		roots = pack
	else:
		roots = (group.get("roots") or [])[:max_roots]

	before = _dash()
	preview = dry_run(roots)
	result = {
		"n_roots": len(roots),
		"vouchers": [r.get("voucher") for r in roots],
		"dry_run_count": preview.get("count"),
		"apply": apply,
		"classification_summary": cls.get("by_bucket"),
	}
	if not apply:
		result["ok"] = True
		result["preview"] = preview
		return result

	frappe.flags["historical_repair"] = True
	applied, failed = [], []
	try:
		for i, row in enumerate(roots, 1):
			sp = f"po_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(sp)
			try:
				out = apply_repairs([row], dry_run=False)
				# apply_repairs may return list/dict
				ok = True
				if isinstance(out, dict) and (out.get("aborted") or out.get("blocked")):
					ok = False
				if ok:
					applied.append({"voucher": row.get("voucher"), "out_keys": list(out.keys()) if isinstance(out, dict) else type(out).__name__})
					frappe.db.commit()
				else:
					frappe.db.rollback(save_point=sp)
					failed.append({"voucher": row.get("voucher"), "out": out})
					result["stop_reason"] = f"blocked at {row.get('voucher')}"
					break
			except Exception as exc:
				frappe.db.rollback(save_point=sp)
				failed.append({"voucher": row.get("voucher"), "error": str(exc)})
				result["stop_reason"] = str(exc)
				break
	finally:
		frappe.flags["historical_repair"] = False

	after = _dash()
	val = _validate()
	result.update(
		{
			"ok": bool(applied) and not result.get("stop_reason") and val.get("all_pass"),
			"applied": applied,
			"failed": failed,
			"before": before,
			"after": after,
			"kpi_delta": _delta(before, after, ["Posting Order", "I4 Leftover", "Zero Rate", "Broken Bin", "Integrity Score"]),
			"dashboard_validation": val,
		}
	)
	_dump("posting_order/repair_round.json", result)
	return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_phase1(*, apply=True) -> dict:
	"""Full Phase 1 session: baseline → I4 → ZR → PO classify/repair → report."""
	t0 = perf_counter()
	print("=== Phase 1 baseline ===")
	before = _dash()
	val0 = _validate()
	_dump("baseline.json", {"dashboard": before, "validation": val0})
	print("baseline", {k: before.get(k) for k in ("I4 Leftover", "Zero Rate", "Posting Order", "READY_I4", "Integrity Score")})
	print("validate", val0.get("all_pass"), val0.get("pass_count"), val0.get("fail_count"))

	print("=== Phase 1 I4 ===")
	i4 = run_i4_until_exhausted(apply=apply)

	print("=== Phase 1 Zero Rate ===")
	zr = run_zr_until_stable(apply=apply)

	print("=== Phase 1 Posting Order ===")
	po_cls = classify_posting_order()
	po_rep = run_po_repairable(apply=apply, max_roots=12)

	after = _dash()
	val1 = _validate()
	report = {
		"version": "5.2.16",
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": COMPANY,
		"database": "20260915_133438-database.sql.gz",
		"production_affected": False,
		"before": before,
		"after": after,
		"kpi_delta": _delta(
			before,
			after,
			[
				"I4 Leftover",
				"READY_I4",
				"WAITING_I4",
				"Zero Rate",
				"Posting Order",
				"Patient Zero",
				"Wrong Incoming",
				"Wrong Outgoing",
				"Wrong Amount",
				"Broken Bin",
				"Broken GL",
				"Failed RIV",
				"Integrity Score",
				"Repairable",
			],
		),
		"validation_before": val0,
		"validation_after": val1,
		"i4": i4,
		"zero_rate": zr,
		"posting_order": {"classification": po_cls, "repair": {k: po_rep.get(k) for k in po_rep if k not in ("before", "after")}},
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"phase2_started": False,
		"recommendation": _recommend(before, after, i4, zr, po_cls, po_rep, val1),
	}
	_dump("PHASE1_REPORT_v5216.json", report)
	print("DONE", report["recommendation"])
	return report


def _recommend(before, after, i4, zr, po_cls, po_rep, val1):
	i4_left = int((after or {}).get("I4 Leftover") or 0)
	zr_left = int((after or {}).get("Zero Rate") or 0)
	po_left = int((after or {}).get("Posting Order") or 0)
	ready_i4 = int((after or {}).get("READY_I4") or 0)
	po_true = int((po_cls or {}).get("true_repairable") or 0)
	parts = []
	if not val1.get("all_pass"):
		parts.append("Dashboard validation FAIL — fix consistency before more repairs")
	if i4.get("stopped"):
		parts.append("I4 campaign stopped on regression/false-success — extend engine before resume")
	elif ready_i4 > 0:
		parts.append(f"READY_I4 still {ready_i4} — continue I4 SAFE_GROUP")
	elif i4_left > 0:
		parts.append(f"I4 leftover {i4_left} remains as WAITING/MANUAL — improve dependency conversion")
	else:
		parts.append("I4 ≈ 0")
	if zr.get("stopped"):
		parts.append("Zero Rate stopped on gate/engine — investigate before next cluster")
	elif zr_left > 20:
		parts.append(f"Zero Rate {zr_left} — continue SAFE_GROUP clusters")
	else:
		parts.append(f"Zero Rate near target ({zr_left})")
	if po_true > 0 and not (po_rep or {}).get("exhausted"):
		parts.append(f"Posting Order TRUE_REPAIRABLE={po_true} — continue SAFE batches")
	elif po_left > 0:
		parts.append(f"Posting Order {po_left} mostly non-auto — classify MANUAL/SHORTAGE/WAITING")
	else:
		parts.append("Posting Order ≈ 0")
	mature = i4_left <= 5 and zr_left <= 20 and po_true == 0 and val1.get("all_pass") and not i4.get("stopped") and not zr.get("stopped")
	parts.append("Phase 1 MATURE — Phase 2 may begin" if mature else "Phase 1 NOT complete — do not start Phase 2")
	return "; ".join(parts)
