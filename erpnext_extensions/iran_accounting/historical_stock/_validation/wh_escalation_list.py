# Copyright (c) 2026 — list warehouse escalation candidates
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	scan = run_full_history_scan(company="اسپاد فارمد دارو")
	rows = [r for r in scan["rows"] if "WAREHOUSE" in str(r.get("planner_status") or "")]
	out = []
	for r in rows:
		out.append(
			{
				"item": r.get("item_code") or r.get("item"),
				"warehouse": r.get("warehouse"),
				"status": r.get("status"),
				"planner_status": r.get("planner_status"),
				"sql_updates": r.get("sql_updates"),
				"reason": str(r.get("reason") or r.get("message") or "")[:200],
				"batch": r.get("batch_no") or r.get("batch"),
				"other_batch_rewrite_count": r.get("other_batch_rewrite_count"),
				"posting_datetime": str(r.get("posting_datetime") or ""),
				"inbound": r.get("inbound_voucher") or r.get("inbound"),
				"outbound": r.get("outbound_voucher") or r.get("outbound"),
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"keys": sorted([k for k in r.keys() if "batch" in k.lower() or "voucher" in k.lower() or "sql" in k.lower() or "second" in k.lower()])[:30],
			}
		)
	# sort by other_batch_rewrite_count ascending (smallest first)
	out.sort(key=lambda x: (int(x.get("other_batch_rewrite_count") or 999), str(x.get("item"))))
	print(json.dumps({"count": len(out), "rows": out}, ensure_ascii=False, indent=2, default=str))
	return {"count": len(out), "rows": out}
