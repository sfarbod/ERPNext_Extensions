# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 Failed RIV campaign — classify then repair only SAFE_TO_RETRY clusters."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime

from erpnext_extensions.iran_accounting.historical_stock import RIV_SAFE_TO_RETRY
from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import SAFE_GROUP, cluster_independent_roots
from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv

ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5215/failed_riv"
)
COMPANY = "اسپاد فارمد دارو"

RIV_BUCKETS = (
	"SAFE_TO_RETRY",
	"WAITING_SLE",
	"WAITING_RATE",
	"WAITING_GL",
	"WAITING_PATIENT_ZERO",
	"DEADLOCK",
	"TIMEOUT",
	"RAW_MATERIAL_COST",
	"UNKNOWN",
	"UNSAFE",
)


def _dump(name, data):
	os.makedirs(ARTIFACT, exist_ok=True)
	path = os.path.join(ARTIFACT, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	return path


def _bucket(row) -> str:
	st = str(row.get("riv_status") or row.get("status") or "")
	err = str(row.get("error_head") or "").lower()
	if st == RIV_SAFE_TO_RETRY or row.get("eligible"):
		return "SAFE_TO_RETRY"
	if "WAITING_SLE" in st:
		return "WAITING_SLE"
	if "WAITING_RATE" in st:
		return "WAITING_RATE"
	if "WAITING_GL" in st:
		return "WAITING_GL"
	if "WAITING_PATIENT" in st or "PATIENT_ZERO" in st:
		return "WAITING_PATIENT_ZERO"
	if "deadlock" in err:
		return "DEADLOCK"
	if "timeout" in err or "lock wait" in err:
		return "TIMEOUT"
	if "raw material" in err or "consumption entry" in err:
		return "RAW_MATERIAL_COST"
	if "UNSAFE" in st:
		return "UNSAFE"
	return "UNKNOWN"


def classify_failed_riv_campaign(company=None, max_cluster=12) -> dict:
	company = company or COMPANY
	scan = scan_failed_riv(company=company, limit=2000)
	rows = scan.get("rows") or []
	by_bucket = defaultdict(list)
	for r in rows:
		b = _bucket(r)
		r["riv_bucket"] = b
		by_bucket[b].append(r)

	safe = by_bucket.get("SAFE_TO_RETRY") or []
	for r in safe:
		r.setdefault("topic", "FAILED_RIV")
		r.setdefault("confidence", "EXACT")
	clusters = cluster_independent_roots(safe, max_cluster=max_cluster)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"total": len(rows),
		"by_bucket": {k: len(v) for k, v in by_bucket.items()},
		"bucket_order": list(RIV_BUCKETS),
		"safe_to_retry": len(safe),
		"clusters": clusters,
		"promotion_status": "NOT_PROVEN",
		"message": "Only SAFE_TO_RETRY may enter a repair campaign. Never retry all Failed RIV.",
	}
	_dump("classification.json", out)
	return out
