# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 validation runbook — master plan + zero rate small SAFE cluster (dev only)."""

from __future__ import annotations

import json
import os
from datetime import datetime

import frappe

COMPANY = "اسپاد فارمد دارو"
ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5215"
)


def _dump(name, data):
	path = os.path.join(ARTIFACT, name)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def run():
	from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_wizard
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate_campaign import (
		classify_zero_clusters,
		preview_zero_campaign,
		run_zero_safe_cluster_campaign,
	)
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_campaign import classify_wrong_clusters
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv_campaign import classify_failed_riv_campaign
	from erpnext_extensions.iran_accounting.historical_stock.gl_campaign import classify_gl_campaign

	print("=== Master Repair Plan ===")
	plan = build_master_repair_plan(company=COMPANY)
	_dump("master_plan.json", plan)
	for c in plan.get("classes") or []:
		print(
			f"  {c['repair_class']}: count={c['current_count']} READY={c['READY']} "
			f"WAIT={c['WAITING']} PZ={c['patient_zero_count']} risk={c['risk']} promo={c['promotion_status']}"
		)

	print("=== Campaign Wizard ===")
	wiz = campaign_wizard(company=COMPANY)
	_dump("wizard_snapshot.json", wiz)

	print("=== Zero Rate classify + preview ===")
	cls = classify_zero_clusters(company=COMPANY, max_cluster=12)
	prev = preview_zero_campaign(company=COMPANY, max_roots=12)
	print(f"  exact_ready={cls.get('exact_ready')} independent={cls['clusters'].get('independent_root_count')}")
	print(f"  preview ok={prev.get('ok')} n={prev.get('n_roots')} blocked={len(prev.get('blocked') or [])}")

	print("=== Wrong / RIV / GL classify (scaffold) ===")
	wr = classify_wrong_clusters(company=COMPANY)
	riv = classify_failed_riv_campaign(company=COMPANY)
	gl = classify_gl_campaign(company=COMPANY)
	print(f"  WR exact_ready={wr.get('exact_ready')} RIV safe={riv.get('safe_to_retry')} GL repairable={gl.get('repairable_after_sle_healthy')}")

	# Only apply if preview has a SAFE group with dry rows and no blocked
	apply = bool(prev.get("ok") and (prev.get("n_roots") or 0) >= 1 and not prev.get("blocked"))
	print(f"=== Zero Rate SAFE cluster apply={apply} ===")
	result = run_zero_safe_cluster_campaign(company=COMPANY, max_roots=12, apply=apply)
	_dump("zero_rate_campaign_final.json", result)
	print(
		f"  ok={result.get('ok')} verified={len(result.get('verified') or [])} "
		f"failed={len(result.get('failed') or [])} promo={result.get('promotion_status')} "
		f"stop={result.get('stop_reason')}"
	)
	if result.get("kpi_delta"):
		for k, v in result["kpi_delta"].items():
			print(f"  KPI {k}: {v}")

	report = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"version": "5.2.15",
		"company": COMPANY,
		"master_plan_summary": [
			{k: c.get(k) for k in ("repair_class", "current_count", "READY", "WAITING", "MANUAL", "AMBIGUOUS", "patient_zero_count", "risk", "promotion_status", "priority")}
			for c in plan.get("classes") or []
		],
		"dependency_graph": plan.get("dependency_graph"),
		"zero_rate_campaign": {
			"apply": apply,
			"ok": result.get("ok"),
			"verified": len(result.get("verified") or []),
			"failed": len(result.get("failed") or []),
			"promotion_status": result.get("promotion_status"),
			"kpi_delta": result.get("kpi_delta"),
			"dashboard_validation": result.get("dashboard_validation"),
			"stop_reason": result.get("stop_reason"),
			"elapsed_seconds": result.get("elapsed_seconds"),
		},
		"scaffolds": {
			"wrong_rate_exact_ready": wr.get("exact_ready"),
			"riv_safe_to_retry": riv.get("safe_to_retry"),
			"riv_by_bucket": riv.get("by_bucket"),
			"gl_repairable": gl.get("repairable_after_sle_healthy"),
			"gl_by_bucket": gl.get("by_bucket"),
		},
		"recommendation": (
			"Promote Zero Rate to PRODUCTION_PROVEN and continue Wrong Rate"
			if result.get("promotion_status") == "PRODUCTION_PROVEN"
			else "Stop — improve Zero Rate engine / restore / repeat before Wrong Rate"
		),
	}
	_dump("CAMPAIGN_REPORT_v5215.json", report)
	print("DONE", report["recommendation"])
	return report


if __name__ == "__main__":
	# bench execute path
	pass
