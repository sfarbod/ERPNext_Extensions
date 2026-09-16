# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 GL root repair classification — SLE truth; never rebuild from poisoned SLE."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime

from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity


GL_BUCKETS = (
	"G1",
	"G2",
	"G3",
	"G4",
	"ROOT_GL",
	"DOWNSTREAM_GL",
	"WAITING_SLE",
	"WAITING_REPLAY",
	"WAITING_RATE",
)

from erpnext_extensions.iran_accounting.historical_stock.util import dump_artifact, resolve_company




def classify_gl_campaign(company=None) -> dict:
	company = resolve_company(company)
	scan = scan_gl_integrity(company=company, limit=500)
	rows = scan.get("rows") or []
	by_bucket = defaultdict(list)
	for r in rows:
		gc = str(r.get("gl_class") or r.get("status") or "")
		bucket = "UNKNOWN"
		for b in ("G1", "G2", "G3", "G4"):
			if b in gc:
				bucket = b
				break
		ps = str(r.get("planner_status") or r.get("status") or "")
		if "WAITING_SLE" in ps or r.get("sle_poisoned"):
			bucket = "WAITING_SLE"
		elif "WAITING_REPLAY" in ps:
			bucket = "WAITING_REPLAY"
		elif "WAITING_RATE" in ps:
			bucket = "WAITING_RATE"
		elif r.get("is_root") or r.get("root_gl"):
			bucket = "ROOT_GL"
		elif r.get("downstream") or "DOWNSTREAM" in ps:
			bucket = "DOWNSTREAM_GL"
		r["gl_bucket"] = bucket
		by_bucket[bucket].append(r)

	repairable = []
	for b in ("G1", "G2", "G3", "G4", "ROOT_GL"):
		for r in by_bucket.get(b) or []:
			# Only after SLE healthy
			if r.get("sle_poisoned") or "WAITING" in str(r.get("planner_status") or ""):
				continue
			if r.get("eligible") or str(r.get("planner_status") or "").startswith("READY"):
				repairable.append(r)

	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"total": len(rows),
		"by_bucket": {k: len(v) for k, v in by_bucket.items()},
		"bucket_order": list(GL_BUCKETS),
		"repairable_after_sle_healthy": len(repairable),
		"by_class": scan.get("by_class"),
		"promotion_status": "NOT_PROVEN",
		"message": "Never rebuild GL from poisoned SLE. SLE remains source of truth.",
	}
	dump_artifact("gl", "classification.json", out)
	return out
