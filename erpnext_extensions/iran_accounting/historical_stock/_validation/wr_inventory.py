# Copyright (c) 2026 — inventory wrong-rate READY roots
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	scan = scan_wrong_rates(company="اسپاد فارمد دارو", limit=2000)
	rows = scan.get("rows") or []
	exact = [r for r in rows if r.get("confidence") == "EXACT"]
	ready = [
		r
		for r in exact
		if r.get("eligible") or r.get("planner_status") in READY_STATUSES or str(r.get("planner_status") or "").startswith("READY")
	]
	# Prefer non-zero current (true wrong rate) vs zero (overlap with zero-rate)
	nonzero = [r for r in ready if abs(float(r.get("current") or r.get("current_rate") or 0)) > 0.0001]
	zeroish = [r for r in ready if abs(float(r.get("current") or r.get("current_rate") or 0)) <= 0.0001]
	out = {
		"total": scan.get("count"),
		"by_confidence": scan.get("by_confidence"),
		"by_flag": scan.get("by_flag"),
		"by_planner": dict(Counter(str(r.get("planner_status")) for r in rows)),
		"exact": len(exact),
		"exact_ready": len(ready),
		"exact_ready_nonzero": len(nonzero),
		"exact_ready_zeroish": len(zeroish),
		"sample_ready": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"warehouse": r.get("warehouse"),
				"flags": r.get("flags"),
				"current": r.get("current") or r.get("current_rate"),
				"expected": r.get("expected") or r.get("proposed_rate"),
				"source": r.get("source") or r.get("source_of_truth"),
				"planner_status": r.get("planner_status"),
				"sql_updates": r.get("sql_updates"),
				"surface": r.get("surface"),
				"purpose": r.get("purpose"),
			}
			for r in (nonzero or ready)[:15]
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
