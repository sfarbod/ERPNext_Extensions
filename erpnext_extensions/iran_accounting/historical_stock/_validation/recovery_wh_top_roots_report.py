# Copyright (c) 2026 — top remaining roots by dependency for warehouse report
from __future__ import annotations

import json
from collections import Counter


COMPANY = "اسپاد فارمد دارو"


def run(limit=100):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	rows = []
	for raw in scan.get("rows") or []:
		r = attach_plan(dict(raw), cache=cache)
		ps = str(r.get("planner_status") or "")
		if ps in ("READY", "READY_LOCAL_REPAIR", "READY_BATCH_SCOPED_REPAIR", "READY_IDENTITY_REPAIR", "READY_WAREHOUSE_REPLAY", "READY_WAREHOUSE_CAMPAIGN"):
			continue
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		dep = str(r.get("dependency") or "")
		# dependency score
		score = 0
		if "WAREHOUSE" in ps or "WAREHOUSE" in dep:
			score += 50
		if opt == "CROSS_TIME_REPAIRABLE":
			score += 40
		if opt == "CROSS_ITEM_CONFLICT":
			score += 35
		if opt == "MIDNIGHT_REVIEW":
			score += 30
		if opt == "REAL_STOCK_SHORTAGE":
			score += 10
		if "AMBIGUOUS" in ps or opt == "AMBIGUOUS_DEPENDENCY":
			score += 5
		min_b = r.get("min_qty_before")
		try:
			score += min(40, int(abs(float(min_b or 0)) / 50))
		except Exception:
			pass
		# classify blocker
		if opt == "REAL_STOCK_SHORTAGE" or dep == "WAREHOUSE_REAL_SHORTAGE":
			klass = "Real stock shortage"
		elif "AMBIGUOUS" in ps or opt == "AMBIGUOUS_DEPENDENCY":
			klass = "Mathematical ambiguity"
		elif opt == "MIDNIGHT_REVIEW":
			klass = "Operator decision"
		elif opt in ("CROSS_ITEM_CONFLICT", "CROSS_TIME_REPAIRABLE") or "WAREHOUSE" in ps:
			klass = "Engine limitation"
		elif opt == "LATER_INBOUND_UNRELATED":
			klass = "Missing evidence"
		elif "MANUAL" in ps or opt == "NO_REPAIR_NEEDED":
			klass = "Policy decision" if opt == "NO_REPAIR_NEEDED" else "Operator decision"
		else:
			klass = "Operator decision"
		rows.append(
			{
				"score": score,
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"out": r.get("outbound_document") or r.get("negative_voucher"),
				"in": r.get("inbound_document"),
				"opt": opt,
				"ps": ps,
				"dep": dep,
				"conf": r.get("confidence"),
				"min_b": r.get("min_qty_before"),
				"min_a": r.get("min_qty_after"),
				"class": klass,
			}
		)
	rows.sort(key=lambda x: (-x["score"], str(x.get("item") or ""), str(x.get("out") or "")))
	top = rows[: int(limit)]
	wh = discover_warehouse_campaigns(company=COMPANY)
	out = {
		"n_remaining_po": len(rows),
		"by_class": dict(Counter(r["class"] for r in rows)),
		"top100": [
			{
				**{k: (v[:50] if k == "warehouse" and isinstance(v, str) else v) for k, v in r.items()},
			}
			for r in top
		],
		"warehouse_ready": wh.get("n_ready_campaigns"),
		"warehouse_ready_items": [c.get("item") for c in (wh.get("ready_campaigns") or [])],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
