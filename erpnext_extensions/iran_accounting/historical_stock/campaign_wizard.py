# Copyright (c) 2026, ERPNext Extensions contributors
"""Repair Campaign Wizard — preview / health / resume / metrics / export."""

from __future__ import annotations

from datetime import datetime

from erpnext_extensions.iran_accounting.historical_stock.failed_riv_campaign import classify_failed_riv_campaign
from erpnext_extensions.iran_accounting.historical_stock.gl_campaign import classify_gl_campaign
from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
from erpnext_extensions.iran_accounting.historical_stock.util import dump_artifact, resolve_company
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_campaign import (
	classify_wrong_clusters,
	preview_wrong_campaign,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_rate_campaign import (
	classify_zero_clusters,
	preview_zero_campaign,
)

_HISTORY: list[dict] = []


def campaign_wizard(company=None) -> dict:
	"""Operator entry: Master Plan + available campaign previews (read-only)."""
	company = resolve_company(company)
	plan = build_master_repair_plan(company=company)
	zr = classify_zero_clusters(company=company, max_cluster=15)
	wr = classify_wrong_clusters(company=company, max_cluster=15)
	riv = classify_failed_riv_campaign(company=company, max_cluster=12)
	gl = classify_gl_campaign(company=company)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"version": "5.2.20",
		"company": company,
		"master_plan": {
			"repair_order": plan.get("repair_order"),
			"classes": [
				{
					"repair_class": c["repair_class"],
					"READY": c.get("READY"),
					"WAITING": c.get("WAITING"),
					"current_count": c.get("current_count"),
					"risk": c.get("risk"),
					"promotion_status": c.get("promotion_status"),
					"priority": c.get("priority"),
				}
				for c in plan.get("classes") or []
			],
			"dependency_graph": plan.get("dependency_graph"),
		},
		"campaigns": {
			"ZERO_RATE": {
				"exact_ready": zr.get("exact_ready"),
				"independent_roots": zr["clusters"].get("independent_root_count"),
				"recommended": _summarize_group(zr["clusters"].get("recommended_first_group")),
				"promotion_status": zr.get("promotion_status"),
			},
			"WRONG_RATE": {
				"exact_ready": wr.get("exact_ready"),
				"independent_roots": wr["clusters"].get("independent_root_count"),
				"recommended": _summarize_group(wr["clusters"].get("recommended_first_group")),
				"promotion_status": wr.get("promotion_status"),
			},
			"FAILED_RIV": {
				"by_bucket": riv.get("by_bucket"),
				"safe_to_retry": riv.get("safe_to_retry"),
				"recommended": _summarize_group(riv["clusters"].get("recommended_first_group")),
				"promotion_status": riv.get("promotion_status"),
			},
			"GL": {
				"by_bucket": gl.get("by_bucket"),
				"repairable_after_sle_healthy": gl.get("repairable_after_sle_healthy"),
				"promotion_status": gl.get("promotion_status"),
			},
			"WAREHOUSE_WIDE": {
				"promotion_status": "ENGINE_SCAFFOLD",
				"message": "Use warehouse_dependency.analyze_warehouse_dependencies first",
			},
		},
		"forbidden": [
			"Global Replay",
			"Global Repost",
			"Global GL rebuild",
			"Global RIV retry",
			"Warehouse-wide replay without proof",
			"Different roots inside same dependency tree",
		],
	}
	_record("wizard", out)
	dump_artifact("campaigns", "wizard/latest.json", out)
	return out


def campaign_preview(topic: str, company=None) -> dict:
	company = resolve_company(company)
	topic = (topic or "ZERO_RATE").upper()
	if topic == "ZERO_RATE":
		out = preview_zero_campaign(company=company)
	elif topic == "WRONG_RATE":
		out = preview_wrong_campaign(company=company)
	elif topic == "FAILED_RIV":
		out = classify_failed_riv_campaign(company=company)
	elif topic == "GL":
		out = classify_gl_campaign(company=company)
	else:
		out = {"ok": False, "reason": f"unknown topic {topic}"}
	_record("preview", {"topic": topic, **{k: out.get(k) for k in ("ok", "n_roots", "promotion_status")}})
	return out


def campaign_health(company=None) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.dashboard_consistency_v5214 import (
		collect_kpi_matrix,
	)

	company = resolve_company(company)
	matrix = collect_kpi_matrix(company=company)
	return {
		"all_pass": matrix.get("all_pass"),
		"pass_count": matrix.get("pass_count"),
		"fail_count": matrix.get("fail_count"),
		"dashboard": matrix.get("dashboard"),
		"authorize_campaigns": bool(matrix.get("all_pass")),
		"history_len": len(_HISTORY),
	}


def campaign_history() -> dict:
	return {"count": len(_HISTORY), "entries": list(_HISTORY[-50:])}


def campaign_export(company=None) -> dict:
	wiz = campaign_wizard(company=company)
	path = dump_artifact(
		"campaigns",
		f"export/campaign_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json",
		wiz,
	)
	return {"path": path, "summary": wiz.get("campaigns")}


def campaign_comparison(before: dict, after: dict) -> dict:
	keys = set(before or {}) | set(after or {})
	diff = {}
	for k in sorted(keys):
		b, a = (before or {}).get(k), (after or {}).get(k)
		if b != a:
			try:
				diff[k] = {"before": b, "after": a, "delta": (a or 0) - (b or 0)}
			except Exception:
				diff[k] = {"before": b, "after": a}
	return {"diff": diff, "changed_keys": list(diff.keys())}


def _summarize_group(g):
	if not g:
		return None
	return {
		"group_class": g.get("group_class"),
		"n_roots": g.get("n_roots"),
		"repair_order": (g.get("repair_order") or [])[:20],
		"estimated_sql_updates": g.get("estimated_sql_updates"),
		"estimated_sle_replay": g.get("estimated_sle_replay"),
		"risk": g.get("risk"),
	}


def _record(kind, payload):
	_HISTORY.append({"ts": datetime.utcnow().isoformat() + "Z", "kind": kind, "payload": payload})
