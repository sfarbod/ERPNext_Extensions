# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 Wrong Rate campaign scaffolding — own Patient Zero graph (no Zero Rate reuse)."""

from __future__ import annotations

import json
import os
from datetime import datetime

from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
	SAFE_GROUP,
	cluster_independent_roots,
	identity_key,
)
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5215/wrong_rate"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name, data):
	os.makedirs(ARTIFACT, exist_ok=True)
	path = os.path.join(ARTIFACT, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	return path


def classify_wrong_clusters(company=None, max_cluster=15) -> dict:
	"""Classify Wrong Rate into independent SAFE clusters. Never uses Zero Rate logic."""
	company = company or COMPANY
	scan = scan_wrong_rates(company=company, limit=2000)
	rows = scan.get("rows") or []
	exact_ready = [
		r
		for r in rows
		if (r.get("confidence") == "EXACT")
		and (
			r.get("eligible")
			or str(r.get("planner_status") or "").startswith("READY")
		)
	]
	# Tag topic for cluster filter
	for r in exact_ready:
		r.setdefault("topic", "WRONG_RATE")
	clusters = cluster_independent_roots(exact_ready, max_cluster=max_cluster)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"total_wrong_rows": scan.get("count"),
		"exact_ready": len(exact_ready),
		"clusters": clusters,
		"by_flag": scan.get("by_flag"),
		"promotion_status": "NOT_PROVEN",
		"message": "Campaign 2 scaffold — dry-run/apply only after Zero Rate PRODUCTION_PROVEN",
	}
	_dump("clusters.json", out)
	return out


def preview_wrong_campaign(company=None, max_roots=12) -> dict:
	cls = classify_wrong_clusters(company=company, max_cluster=max_roots)
	group = cls["clusters"].get("recommended_first_group")
	if not group or group.get("group_class") != SAFE_GROUP:
		return {"ok": False, "reason": "no SAFE_GROUP", "clusters": cls["clusters"], "promotion_status": "NOT_PROVEN"}
	return {
		"ok": True,
		"dry_run": True,
		"apply_allowed": False,
		"group_class": SAFE_GROUP,
		"n_roots": group.get("n_roots"),
		"repair_order": group.get("repair_order"),
		"affected_identities": group.get("affected_identities"),
		"estimated_sql_updates": group.get("estimated_sql_updates"),
		"estimated_sle_replay": group.get("estimated_sle_replay"),
		"promotion_status": "NOT_PROVEN",
		"message": "Preview only — Wrong Rate engine not production-proven",
	}
