# Copyright (c) 2026 — apply remaining READY_WRONG_RATE roots explicitly
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)

	company = "اسپاد فارمد دارو"
	scan = scan_wrong_rates(company=company, limit=4000)
	ready = [
		r
		for r in scan.get("rows") or []
		if str(r.get("planner_status") or "") in READY_STATUSES
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
	]
	results = []
	for r in ready:
		res = apply_wrong_rate_root(r, dry_run=False)
		results.append(
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"source": r.get("source"),
				"expected": r.get("expected") or r.get("proposed_rate"),
				"dependency": r.get("dependency") or r.get("blocked_because"),
				"ok": res.get("ok"),
				"reason": res.get("reason") or res.get("error"),
				"after_rate": res.get("after_rate"),
				"inner_reason": ((res.get("out") or {}).get("reason")),
				"inner_blocked": [
					b.get("error") for b in ((res.get("out") or {}).get("blocked") or [])[:2]
				],
			}
		)
	# re-count
	scan2 = scan_wrong_rates(company=company, limit=4000)
	ready2 = [
		r
		for r in scan2.get("rows") or []
		if str(r.get("planner_status") or "") in READY_STATUSES
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
	]
	out = {
		"attempted": len(results),
		"repaired": sum(1 for x in results if x.get("ok")),
		"failed": sum(1 for x in results if not x.get("ok")),
		"results": results,
		"ready_after": len(ready2),
		"ready_after_sample": [
			{"voucher": r.get("voucher"), "item": r.get("item"), "source": r.get("source")}
			for r in ready2[:10]
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
