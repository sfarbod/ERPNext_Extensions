# Copyright (c) 2026 — inspect WR sql_updates distribution
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
		classify_wrong_rate_universe,
	)

	inv = classify_wrong_rate_universe(company="اسپاد فارمد دارو", limit=2000)
	ready = inv.get("ready_rows") or []
	sqls = Counter(int(r.get("sql_updates") or 0) for r in ready)
	sample = [
		{
			"voucher": r.get("voucher"),
			"sql": r.get("sql_updates"),
			"eligible": r.get("eligible"),
			"planner_status": r.get("planner_status"),
			"rate_status": r.get("rate_status"),
			"surface": r.get("surface"),
		}
		for r in ready[:12]
	]
	# Also count planner READY regardless of our filter
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	scan = scan_wrong_rates(company="اسپاد فارمد دارو", limit=2000)
	plan_ready = [
		r
		for r in (scan.get("rows") or [])
		if r.get("planner_status") in READY_STATUSES or str(r.get("planner_status") or "").startswith("READY")
	]
	out = {
		"classifier_ready": len(ready),
		"sql_dist": dict(sqls),
		"sample": sample,
		"planner_ready": len(plan_ready),
		"planner_sql_dist": dict(Counter(int(r.get("sql_updates") or 0) for r in plan_ready)),
		"by_rate_status": inv.get("by_rate_status"),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:5000])
	return out
