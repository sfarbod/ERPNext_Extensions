# Copyright (c) 2026 — Long-run Historical Repair campaign (30+ iterations)
from __future__ import annotations

import json
import os
from datetime import datetime
from time import perf_counter

COMPANY = "اسپاد فارمد دارو"
ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/campaign_longrun"
)


def _dump(name, data):
	os.makedirs(ARTIFACT, exist_ok=True)
	path = os.path.join(ARTIFACT, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	return path


def _snap() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from collections import Counter

	scan = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
	po = scan.get("posting_order") or {}
	po_rows = po.get("rows") if isinstance(po, dict) else []
	# Prefer integrity scan nested rows; fall back to count
	if not po_rows and isinstance(po, dict):
		# scan stores count only in posting_order payload sometimes
		pass
	dash = {
		"posting_order": po.get("count") if isinstance(po, dict) else po,
		"wrong_rate": (scan.get("wrong_rate") or {}).get("count"),
		"zero_rate": (scan.get("zero_rate") or {}).get("count"),
		"i4_leftover": (scan.get("i4") or {}).get("count"),
		"broken_gl": (scan.get("gl") or {}).get("count"),
		"failed_riv": (scan.get("failed_riv") or {}).get("count"),
		"riv_by": (scan.get("failed_riv") or {}).get("by_status"),
	}
	# PO optimizer breakdown via dedicated scan (cheap enough vs full)
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	po_scan = run_full_history_scan(company=COMPANY)
	by_opt = Counter()
	ready_po = []
	cache = {}
	for raw in po_scan.get("rows") or []:
		opt = str(raw.get("optimizer_status") or raw.get("status") or "")
		if opt == "NO_REPAIR_NEEDED":
			continue
		by_opt[opt] += 1
		r = attach_plan(dict(raw), cache=cache)
		ps = str(r.get("planner_status") or "")
		if ps in READY_STATUSES and int(r.get("sql_updates") or 0) > 0 and r.get("eligible"):
			ready_po.append(r)
	dash["po_by_opt"] = dict(by_opt)
	dash["po_ready"] = len(ready_po)
	dash["po_actionable"] = sum(by_opt.values())
	return {"dashboard": dash, "ready_po": ready_po, "scan": scan, "po_scan": po_scan}


def _master_plan(scan) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan

	try:
		# Prefer fresh rebuild from DB — do not reuse a stale scan payload shape.
		plan = build_master_repair_plan(company=COMPANY)
		return plan
	except TypeError:
		try:
			return build_master_repair_plan()
		except Exception as e:
			return {"error": str(e), "entries": []}
	except Exception as e:
		return {"error": str(e), "entries": []}


def _apply_gl(max_n=20) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
		scan_gl_integrity,
		rebuild_gl_for_voucher,
	)
	import frappe

	scan = scan_gl_integrity(company=COMPANY, limit=300)
	ready = []
	for r in scan.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if (
			ps in READY_STATUSES
			and int(r.get("sql_updates") or 0) > 0
			and r.get("eligible")
			and not r.get("sle_poisoned")
			and str(r.get("gl_class") or "") in ("G1_BALANCED_BUT_ECONOMICALLY_WRONG", "G2_MISSING", "G3_UNBALANCED")
		):
			ready.append(r)
	applied, failed, noop = [], [], []
	for r in ready[: int(max_n)]:
		vn = r.get("voucher")
		try:
			res = rebuild_gl_for_voucher(vn, dry_run=False)
			changed = bool(res.get("written")) and not res.get("blocked")
			if not changed:
				noop.append(
					{
						"voucher": vn,
						"gl_class": r.get("gl_class"),
						"reason": res.get("reason") or res.get("blocked_because") or "not_written",
					}
				)
				continue
			frappe.db.commit()
			applied.append({"voucher": vn, "ok": True, "gl_class": r.get("gl_class")})
		except Exception as e:
			failed.append({"voucher": vn, "ok": False, "reason": f"{type(e).__name__}: {e}"})
	return {
		"n_ready": len(ready),
		"n_repaired": len(applied),
		"n_failed": len(failed),
		"n_noop": len(noop),
		"applied": applied[:10],
		"failed": failed[:10],
	}


def _apply_svd_residue(max_n=40) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.svd_residue import (
		scan_matched_rate_svd_residue,
		apply_svd_residue,
	)

	rows = scan_matched_rate_svd_residue(COMPANY, limit=500)
	applied, failed = [], []
	for r in rows[: int(max_n)]:
		res = apply_svd_residue(r, dry_run=False)
		entry = {
			"voucher": r.get("voucher"),
			"item": r.get("item"),
			"ok": bool(res.get("ok")) and int(res.get("changed") or 0) > 0,
			"changed": res.get("changed"),
			"reason": res.get("reason"),
		}
		(applied if entry["ok"] else failed).append(entry)
	return {"n_candidates": len(rows), "n_repaired": len(applied), "n_failed": len(failed)}


def _apply_wr_ready(max_n=20) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)
	import frappe

	scan = scan_wrong_rates(company=COMPANY, limit=2000)
	ready = []
	for r in scan.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if (ps in READY_STATUSES or ps.startswith("READY")) and int(r.get("sql_updates") or 0) > 0 and r.get("eligible"):
			ready.append(r)
	applied, failed = [], []
	for r in ready[: int(max_n)]:
		res = apply_wrong_rate_root(r, dry_run=False)
		entry = {
			"voucher": r.get("voucher"),
			"item": r.get("item"),
			"ok": bool(res.get("ok")),
			"reason": res.get("reason") or res.get("error"),
		}
		(applied if entry["ok"] else failed).append(entry)
		if entry["ok"]:
			frappe.db.commit()
	return {"n_ready": len(ready), "n_repaired": len(applied), "n_failed": len(failed)}


def _apply_po(ready, max_n=40) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.recovery_po_apply_one import run as apply_one

	# Apply all unique outbounds among ready (capped)
	seen = []
	for r in ready:
		vn = r.get("outbound_document")
		if vn and vn not in seen:
			seen.append(vn)
		if len(seen) >= int(max_n):
			break
	results = []
	for vn in seen:
		try:
			res = apply_one(outbound=vn, dry_run=0)
			results.append(res)
		except Exception as e:
			results.append({"outbound": vn, "exc": f"{type(e).__name__}: {e}"})
	ok = sum(1 for r in results for x in (r.get("results") or []) if x.get("ok"))
	fail = sum(1 for r in results for x in (r.get("results") or []) if x.get("ok") is False)
	return {"n_outbounds": len(seen), "n_ok": ok, "n_fail": fail, "results": results}


def _apply_assisted(max_n=40) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.recovery_assisted_apply_auto import (
		run as assisted,
	)

	return assisted(max_n=max_n, dry_run=0)


def _apply_warehouse(max_n=10) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.recovery_posting_order_apply import (
		run as po_apply,
	)

	# Uses SAFE_GROUP including READY_WAREHOUSE_REPLAY
	return po_apply(max_n=max_n, dry_run=0)


def run_iteration(n: int) -> dict:
	"""One full recovery iteration."""
	t0 = perf_counter()
	before = _snap()
	plan = _master_plan(before["scan"])
	repairs = {
		"po": _apply_po(before["ready_po"], max_n=40) if before["ready_po"] else {"n_outbounds": 0},
		"warehouse": _apply_warehouse(max_n=15),
		"wr": _apply_wr_ready(max_n=30),
		"svd": _apply_svd_residue(max_n=40),
		"gl": _apply_gl(max_n=20),
		"assisted": _apply_assisted(max_n=40),
	}
	after = _snap()
	bd, ad = before["dashboard"], after["dashboard"]
	delta = {
		k: (ad.get(k) if not isinstance(ad.get(k), dict) else None)
		and (None if bd.get(k) is None or ad.get(k) is None else (ad.get(k) - bd.get(k)))
		for k in ("posting_order", "wrong_rate", "zero_rate", "i4_leftover", "broken_gl", "failed_riv", "po_actionable", "po_ready")
	}
	# fix delta calc
	delta = {}
	for k in ("posting_order", "wrong_rate", "zero_rate", "i4_leftover", "broken_gl", "failed_riv", "po_actionable", "po_ready"):
		b, a = bd.get(k), ad.get(k)
		delta[k] = None if b is None or a is None else (a - b)

	out = {
		"iteration": n,
		"ts": datetime.now().isoformat(timespec="seconds"),
		"elapsed_s": round(perf_counter() - t0, 3),
		"before": bd,
		"after": ad,
		"delta": delta,
		"repairs": {
			"po": {k: repairs["po"].get(k) for k in ("n_outbounds", "n_ok", "n_fail")},
			"warehouse": {
				k: repairs["warehouse"].get(k)
				for k in ("n_ready_before", "n_repaired", "n_failed")
			},
			"wr": {k: repairs["wr"].get(k) for k in ("n_ready", "n_repaired", "n_failed")},
			"svd": {k: repairs["svd"].get(k) for k in ("n_candidates", "n_repaired", "n_failed")},
			"gl": {k: repairs["gl"].get(k) for k in ("n_ready", "n_repaired", "n_failed")},
			"assisted": {
				k: repairs["assisted"].get(k)
				for k in ("n_auto_ready", "n_repaired", "n_failed", "n_skipped_matched", "by_bucket")
			},
		},
		"master_plan_n": len(plan.get("classes") or plan.get("entries") or plan.get("rows") or plan.get("plan") or []),
		"master_plan_ready": {
			c.get("repair_class"): c.get("READY")
			for c in (plan.get("classes") or [])
			if isinstance(c, dict)
		},
		"po_by_opt_after": ad.get("po_by_opt"),
		"worthwhile": bool(
			(delta.get("po_actionable") or 0) < 0
			or (delta.get("wrong_rate") or 0) < 0
			or (delta.get("broken_gl") or 0) < 0
			or (delta.get("zero_rate") or 0) < 0
			or (delta.get("failed_riv") or 0) < 0
			or (delta.get("i4_leftover") or 0) < 0
			or (repairs["po"].get("n_ok") or 0) > 0
			or (repairs["svd"].get("n_repaired") or 0) > 0
			or (repairs["wr"].get("n_repaired") or 0) > 0
			or (repairs["assisted"].get("n_repaired") or 0) > 0
			# GL only counts when KPI moved or explicit write confirmed elsewhere
		),
	}
	_dump(f"iter_{n:03d}.json", out)
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out


def run(*, start=1, n_iters=30, stop_when_stuck=3):
	"""Run ``n_iters`` iterations; stop early only after ``stop_when_stuck`` empty ones
	AND no READY queues — but keep going to n_iters minimum unless truly exhausted.
	"""
	summary = []
	stuck = 0
	i = int(start)
	target = int(start) + int(n_iters) - 1
	while i <= target or (stuck < int(stop_when_stuck) and i <= target + 20):
		# Always do at least n_iters; after that stop when stuck
		if i > target and stuck >= int(stop_when_stuck):
			break
		print(f"\n===== CAMPAIGN ITERATION {i} =====", flush=True)
		try:
			out = run_iteration(i)
		except Exception as e:
			out = {"iteration": i, "error": f"{type(e).__name__}: {e}", "worthwhile": False}
			print(json.dumps(out, ensure_ascii=False))
			_dump(f"iter_{i:03d}_error.json", out)
		summary.append(out)
		if out.get("worthwhile"):
			stuck = 0
		else:
			stuck += 1
			# Before giving up mid-minimum, still continue until target
			if i >= target and stuck >= int(stop_when_stuck):
				break
		i += 1
		if i > target + 50:
			break

	final = {
		"n_iters_run": len(summary),
		"first": summary[0].get("before") if summary else None,
		"last": summary[-1].get("after") if summary else None,
		"worthwhile_n": sum(1 for s in summary if s.get("worthwhile")),
		"iters": [
			{
				"n": s.get("iteration"),
				"delta": s.get("delta"),
				"worthwhile": s.get("worthwhile"),
				"repairs": s.get("repairs"),
			}
			for s in summary
		],
	}
	_dump("campaign_summary.json", final)
	print(json.dumps(final, ensure_ascii=False, indent=2, default=str)[:8000])
	return final
