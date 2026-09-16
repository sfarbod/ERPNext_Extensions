# Copyright (c) 2026 — inventory all PO candidates on Quarantine SFG identity
from __future__ import annotations

import json
from collections import Counter


COMPANY = "اسپاد فارمد دارو"
ITEM = "30300014"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	rows = []
	for raw in scan.get("rows") or []:
		if (raw.get("item") or raw.get("item_code")) != ITEM:
			continue
		if raw.get("warehouse") != WH:
			continue
		r = attach_plan(dict(raw), cache=cache)
		rows.append(r)

	out = {
		"n": len(rows),
		"by_opt": dict(Counter(str(r.get("optimizer_status") or r.get("status")) for r in rows)),
		"by_ps": dict(Counter(str(r.get("planner_status")) for r in rows)),
		"rows": [
			{
				"opt": r.get("optimizer_status") or r.get("status"),
				"ps": r.get("planner_status"),
				"conf": r.get("confidence"),
				"in": r.get("inbound_document"),
				"out": r.get("outbound_document"),
				"batch": r.get("batch") or r.get("batch_no"),
				"min_b": r.get("min_qty_before"),
				"min_a": r.get("min_qty_after"),
				"reason": (r.get("reason") or "")[:120],
				"n_moves": len(r.get("moves") or []),
			}
			for r in sorted(rows, key=lambda x: str(x.get("current_outbound_time") or ""))
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:12000])
	return out
