# Copyright (c) 2026 — count READY after engine unlock
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	company = "اسپاد فارمد دارو"
	scan = scan_wrong_rates(company=company, limit=4000)
	rows = scan.get("rows") or []
	ps = Counter(str(r.get("planner_status") or "") for r in rows)
	ready = [
		r
		for r in rows
		if str(r.get("planner_status") or "") in READY_STATUSES
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
	]
	out = {
		"n_rows": len(rows),
		"repairable": scan.get("repairable"),
		"by_planner": dict(ps.most_common()),
		"n_ready": len(ready),
		"ready_sample": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"source": r.get("source") or r.get("source_of_truth"),
				"expected": r.get("expected") or r.get("proposed_rate"),
				"ps": r.get("planner_status"),
				"sql": r.get("sql_updates"),
			}
			for r in ready[:25]
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
