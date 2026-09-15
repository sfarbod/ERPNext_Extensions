# Copyright (c) 2026 — diagnose oscillating warehouse campaigns
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)
	from frappe.utils import get_datetime
	import frappe

	disc = discover_warehouse_campaigns(company="اسپاد فارمد دارو")
	out = []
	for c in disc.get("ready_campaigns") or []:
		same = 0
		diff = []
		for m in c.get("moves") or []:
			doc = m.get("document")
			new = get_datetime(m.get("new")) if m.get("new") else None
			cur = frappe.db.sql(
				"""
				SELECT posting_datetime FROM `tabStock Ledger Entry`
				WHERE voucher_no=%s AND is_cancelled=0
				ORDER BY posting_datetime LIMIT 1
				""",
				(doc,),
			)
			cur_dt = get_datetime(cur[0][0]) if cur else None
			if cur_dt and new and abs((cur_dt - new).total_seconds()) < 0.5:
				same += 1
			else:
				diff.append({"doc": doc, "cur": str(cur_dt), "new": str(new)})
		out.append(
			{
				"item": c.get("item"),
				"warehouse": (c.get("warehouse") or "")[:40],
				"n_moves": len(c.get("moves") or []),
				"already_applied": same,
				"need_change": len(diff),
				"diffs": diff[:6],
				"status": c.get("planner_status"),
			}
		)
	print(json.dumps({"n": len(out), "rows": out}, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
