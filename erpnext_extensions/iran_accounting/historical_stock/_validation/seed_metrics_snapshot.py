"""Seed metrics snapshot from current blocker counts + last campaign KPI without full Scan All."""
from __future__ import annotations
import json
from time import perf_counter

def run(company=None):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		get_dashboard_summary,
		save_metrics_snapshot,
		worker_queue_status,
	)
	t0 = perf_counter()
	company = company or "اسپاد فارمد دارو"
	# Prefer a lightweight dashboard: known campaign KPIs + live blocker counts.
	# Full Scan All remains the authoritative refresher when workers are up.
	dash = {
		"Integrity Score": 31,
		"Integrity Score Version": "5.3.0",
		"Posting Order": 443,
		"Wrong Rate": 3094,
		"Zero Rate": 463,
		"I1 Negative Rate": 3,
		"I4 Leftover": 63,
		"Broken Bin": 40,
		"Broken GL": 88,
		"Failed RIV": 1716,
		"Patient Zero": 192,
	}
	if frappe.db.exists("DocType", "Historical Repair Blocker"):
		dash["User Action Required"] = frappe.db.count(
			"Historical Repair Blocker",
			{"company": company, "lane": "USER_ACTION_REQUIRED", "status": ("!=", "RESOLVED")},
		)
		dash["Tool Limit"] = frappe.db.count(
			"Historical Repair Blocker",
			{"company": company, "lane": "TOOL_LIMIT", "status": ("!=", "RESOLVED")},
		)
	snap = save_metrics_snapshot(
		company=company,
		dashboard=dash,
		timing={"seed_s": round(perf_counter() - t0, 3)},
		source="campaign_seed_v530",
		extra={"note": "Seeded after Phase 2; refresh via Scan All when long workers available"},
	)
	summary = get_dashboard_summary(company)
	workers = worker_queue_status("long")
	out = {
		"seeded": True,
		"snapshot_name": snap.get("name"),
		"summary_elapsed_ms": summary.get("elapsed_ms"),
		"freshness": summary.get("freshness"),
		"worker_available": workers.get("available"),
		"user_action": dash.get("User Action Required"),
		"tool_limit": dash.get("Tool Limit"),
		"dashboard_keys": sorted(summary.get("dashboard") or {}),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2))
	return out
