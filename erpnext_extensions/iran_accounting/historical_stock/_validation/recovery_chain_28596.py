# Copyright (c) 2026 — debug chain 28596→28696 clearance
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		_rate_patient_cleared,
		evaluate_row,
	)

	company = "اسپاد فارمد دارو"
	scan = scan_wrong_rates(company=company, limit=4000)
	rows = scan.get("rows") or []
	by_v = {}
	for r in rows:
		v = r.get("voucher")
		if v and v not in by_v:
			by_v[v] = r
	targets = ["MAT-STE-2026-28596", "MAT-STE-2026-28696", "MAT-STE-2026-28735"]
	out = {"present": {}, "cleared": {}}
	for v in targets:
		r = by_v.get(v)
		if not r:
			out["present"][v] = None
			continue
		out["present"][v] = {
			"ps": r.get("planner_status"),
			"status": r.get("status"),
			"source": r.get("source"),
			"eligible": r.get("eligible"),
			"current_rate": r.get("current_rate"),
			"current": r.get("current"),
			"expected": r.get("expected"),
			"patient_zero": r.get("patient_zero"),
			"sql": r.get("sql_updates"),
		}
	cache = {"rows_by_voucher": by_v}
	out["cleared"]["28596"] = _rate_patient_cleared("MAT-STE-2026-28596", cache)
	if by_v.get("MAT-STE-2026-28696"):
		d = evaluate_row(dict(by_v["MAT-STE-2026-28696"]), cache=cache)
		out["fresh_28696"] = {
			"ps": d.get("planner_status"),
			"reason": d.get("reason"),
			"eligible": d.get("eligible"),
			"sql": d.get("sql_updates"),
		}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
