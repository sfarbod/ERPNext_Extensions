# Copyright (c) 2026 — dump one warehouse escalation row fully
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.scope import evaluate_minimal_scope

	scan = run_full_history_scan(company="اسپاد فارمد دارو")
	# Prefer smallest: 30300020 with 1 other-batch
	cand = None
	for r in scan["rows"]:
		if (r.get("item_code") or r.get("item")) == "30300020" and "WAREHOUSE" in str(r.get("planner_status") or ""):
			cand = r
			break
	if not cand:
		for r in scan["rows"]:
			if "WAREHOUSE" in str(r.get("planner_status") or ""):
				cand = r
				break
	scope = evaluate_minimal_scope(cand)
	# keep serializable subset of cand
	keys = [
		"item_code",
		"item",
		"warehouse",
		"batch",
		"batch_no",
		"status",
		"planner_status",
		"inbound_document",
		"outbound_document",
		"negative_voucher",
		"proposed_times",
		"times",
		"minimum_seconds_required",
		"sql_updates",
		"reason",
		"message",
		"work_order",
		"confidence",
		"eligible",
		"row_simulation",
		"moves",
	]
	slim = {k: cand.get(k) for k in keys if k in cand}
	# also any *document* keys
	for k, v in cand.items():
		if "document" in k or "voucher" in k or "time" in k.lower():
			if k not in slim:
				slim[k] = v
	print(json.dumps({"candidate": slim, "scope": scope}, ensure_ascii=False, indent=2, default=str)[:8000])
	return {"candidate": slim, "scope": {k: scope.get(k) for k in scope if k != "other_batch_changes"}}
