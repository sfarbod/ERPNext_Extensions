# Copyright (c) 2026
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES, evaluate_row

	scan = scan_wrong_rates(company="اسپاد فارمد دارو", limit=500)
	rows = scan.get("rows") or []
	ps = Counter(str(r.get("planner_status")) for r in rows)
	# Rows that look ready-ish
	cand = [r for r in rows if str(r.get("planner_status") or "").startswith("READY") or r.get("status") == "RECONSTRUCTABLE" and r.get("confidence") == "EXACT"]
	sample = []
	for r in cand[:5]:
		d = evaluate_row(r)
		sample.append(
			{
				"voucher": r.get("voucher"),
				"ps": r.get("planner_status"),
				"sql": r.get("sql_updates"),
				"eligible": r.get("eligible"),
				"status": r.get("status"),
				"surface": r.get("surface"),
				"fresh_ps": d.get("planner_status"),
				"fresh_sql": d.get("sql_updates"),
				"fresh_reason": d.get("reason"),
			}
		)
	# Count startswith READY
	startswith_ready = [r for r in rows if str(r.get("planner_status") or "").startswith("READY")]
	out = {
		"by_planner": dict(ps),
		"startswith_ready": len(startswith_ready),
		"in_READY_STATUSES": sum(1 for r in rows if r.get("planner_status") in READY_STATUSES),
		"sample": sample,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:5000])
	return out
