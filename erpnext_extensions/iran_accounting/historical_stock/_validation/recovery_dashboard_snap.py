# Copyright (c) 2026 — full dashboard snapshot for recovery report
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan

	company = "اسپاد فارمد دارو"
	scan = run_full_integrity_scan(company=company, include_manufacture=False)
	dash = scan.get("dashboard") or scan
	# Prefer explicit dashboard keys used by Desk
	metrics = {
		"integrity_score": dash.get("integrity_score") or scan.get("integrity_score"),
		"posting_order": dash.get("posting_order") or (scan.get("posting_order") or {}).get("count"),
		"wrong_rate": dash.get("wrong_rate") or (scan.get("wrong_rate") or {}).get("count"),
		"wrong_rate_by_status": dict(
			Counter(str(r.get("planner_status") or "") for r in ((scan.get("wrong_rate") or {}).get("rows") or []))
		),
		"zero_rate": dash.get("zero_rate") or (scan.get("zero_rate") or {}).get("count"),
		"i4_leftover": dash.get("i4_leftover") or (scan.get("i4") or {}).get("count"),
		"i4_by_status": (scan.get("i4") or {}).get("by_status"),
		"broken_bin": dash.get("broken_bin") or dash.get("sle_bin"),
		"broken_gl": dash.get("broken_gl") or (scan.get("gl") or {}).get("count"),
		"broken_sabb": dash.get("broken_sabb"),
		"failed_riv": dash.get("failed_riv") or (scan.get("failed_riv") or {}).get("count"),
		"riv_by_status": (scan.get("failed_riv") or {}).get("by_status"),
		"patient_zero": dash.get("patient_zero"),
		"zero_rate_patient_zero": dash.get("zero_rate_patient_zero"),
		"repairable": dash.get("repairable") or scan.get("repairable"),
		"manual": dash.get("manual"),
		"ambiguous": dash.get("ambiguous"),
		"dashboard_keys": sorted((dash.keys() if isinstance(dash, dict) else [])),
	}
	wr_rows = (scan.get("wrong_rate") or {}).get("rows") or []
	ready = [
		r
		for r in wr_rows
		if str(r.get("planner_status") or "") in READY_STATUSES
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
	]
	metrics["wrong_rate_ready"] = len(ready)
	metrics["wrong_rate_ready_sample"] = [
		{
			"voucher": r.get("voucher"),
			"item": r.get("item"),
			"source": r.get("source") or r.get("source_of_truth"),
			"expected": r.get("expected") or r.get("proposed_rate"),
			"dependency": r.get("dependency") or r.get("blocked_because"),
		}
		for r in ready[:20]
	]
	# Master plan top 100
	try:
		plan = build_master_repair_plan(scan)
		entries = plan.get("entries") or plan.get("rows") or plan.get("plan") or []
		metrics["master_plan_n"] = len(entries)
		metrics["master_plan_top"] = entries[:100]
		metrics["master_plan_keys"] = list(plan.keys())[:30]
	except Exception as e:
		metrics["master_plan_error"] = str(e)
	print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str)[:14000])
	return {"metrics": metrics, "scan_summary": {k: (len(v) if isinstance(v, list) else (v.get("count") if isinstance(v, dict) else v)) for k, v in scan.items() if k != "dashboard"}}
