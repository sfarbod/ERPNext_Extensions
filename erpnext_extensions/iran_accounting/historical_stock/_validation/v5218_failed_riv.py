# Copyright (c) 2026 — Failed RIV inventory + optional single SAFE retry
from __future__ import annotations

import json


def run(*, try_retry=0):
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import (
		retry_failed_riv,
		scan_failed_riv,
	)

	scan = scan_failed_riv(company="اسپاد فارمد دارو", limit=500)
	by = scan.get("by_status") or {}
	safe = [r for r in (scan.get("rows") or []) if r.get("riv_status") == "SAFE_TO_RETRY" and r.get("eligible")]
	waiting = {
		k: by.get(k, 0)
		for k in (
			"WAITING_RATE",
			"WAITING_SLE",
			"WAITING_GL",
			"WAITING_PATIENT_ZERO",
			"WAITING_REPLAY",
		)
	}
	unsafe = {
		k: by.get(k, 0)
		for k in (
			"NEGATIVE_STOCK",
			"RAW_MATERIAL_COST",
			"VALUATION_INTEGRITY",
			"PERMANENTLY_UNSAFE",
			"UNKNOWN",
		)
	}
	out = {
		"count": scan.get("count"),
		"by_status": by,
		"safe_to_retry": len(safe),
		"waiting": waiting,
		"unsafe": unsafe,
		"sample_safe": [
			{"riv": r.get("riv_name"), "item": r.get("item"), "warehouse": r.get("warehouse"), "voucher": r.get("voucher")}
			for r in safe[:5]
		],
		"upstream_required": [k for k, v in waiting.items() if v] if not safe else [],
	}
	proof = None
	if int(try_retry) and safe:
		one = safe[0]
		dry = retry_failed_riv(one["riv_name"], dry_run=True)
		applied = None
		if dry.get("dry_run") and not dry.get("blocked"):
			applied = retry_failed_riv(one["riv_name"], dry_run=False)
		proof = {"riv": one["riv_name"], "dry": {k: dry.get(k) for k in ("dry_run", "blocked", "planner_status", "written")}, "applied": applied}
		out["proof"] = proof
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
