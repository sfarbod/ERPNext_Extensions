# Copyright (c) 2026 — extract Desk dashboard KPI numbers
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan

	company = "اسپاد فارمد دارو"
	scan = run_full_integrity_scan(company=company, include_manufacture=False)
	dash = scan.get("dashboard") or {}
	# Dashboard values may be ints or dicts
	def num(key):
		v = dash.get(key)
		if isinstance(v, dict):
			return v.get("value") or v.get("count") or v.get("n") or 0
		return v

	metrics = {k: num(k) for k in dash.keys()}
	wr = scan.get("wrong_rate") or {}
	wr_rows = wr.get("rows") or []
	ps = Counter(str(r.get("planner_status") or "") for r in wr_rows)
	ready = [
		r
		for r in wr_rows
		if str(r.get("planner_status") or "") in READY_STATUSES
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
	]
	waiting = [r for r in wr_rows if "WAITING" in str(r.get("planner_status") or "")]
	manual = [
		r
		for r in wr_rows
		if str(r.get("planner_status") or "") in ("RATE_MANUAL", "MANUAL", "RATE_AMBIGUOUS", "AMBIGUOUS", "RATE_REPAIR_COMPLETE")
	]
	# Waiting class breakdown
	wait_why = Counter()
	for r in waiting:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		src = r.get("source") or r.get("source_of_truth") or ""
		wait_why[f"{r.get('planner_status')}|src={src}|pz={bool(pz_v)}"] += 1
	manual_why = Counter()
	for r in manual:
		manual_why[
			f"{r.get('planner_status')}|src={r.get('source') or r.get('source_of_truth')}|flags={','.join(r.get('flags') or [])}"
		] += 1

	# Master plan
	plan_top = []
	try:
		plan = build_master_repair_plan(company=company, scan=scan)
		entries = plan.get("entries") or plan.get("plan") or plan.get("rows") or []
		if isinstance(entries, dict):
			entries = list(entries.values())
		plan_top = entries[:100]
		plan_meta = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in plan.items()}
	except TypeError:
		try:
			plan = build_master_repair_plan(scan)
			entries = plan.get("entries") or plan.get("plan") or plan.get("rows") or []
			plan_top = (entries or [])[:100]
			plan_meta = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in plan.items()}
		except Exception as e:
			plan_meta = {"error": str(e)}
	except Exception as e:
		plan_meta = {"error": str(e)}

	out = {
		"dashboard": metrics,
		"wrong_rate_by_planner": dict(ps.most_common()),
		"n_ready": len(ready),
		"n_waiting": len(waiting),
		"n_manualish": len(manual),
		"waiting_classes": dict(wait_why.most_common(20)),
		"manual_classes": dict(manual_why.most_common(25)),
		"ready_sample": [
			{"voucher": r.get("voucher"), "item": r.get("item"), "source": r.get("source"), "exp": r.get("expected")}
			for r in ready[:15]
		],
		"master_plan_meta": plan_meta,
		"master_plan_top_n": len(plan_top),
		"master_plan_top": [
			{
				"voucher": e.get("voucher") or e.get("root") or e.get("name"),
				"topic": e.get("topic") or e.get("repair_class"),
				"impact": e.get("impact") or e.get("fan_out") or e.get("score"),
				"status": e.get("planner_status") or e.get("status"),
				"reason": e.get("reason") or e.get("required_action"),
			}
			for e in plan_top[:40]
			if isinstance(e, dict)
		],
		"patient_zero_count": scan.get("patient_zero_count"),
		"zero_rate_count": (scan.get("zero_rate") or {}).get("count"),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:16000])
	return out
