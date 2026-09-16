# Copyright (c) 2026
from __future__ import annotations
import json

def run():
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	scan = scan_gl_integrity(company="اسپاد فارمد دارو", limit=500)
	ready = [
		{
			"voucher": r.get("voucher"),
			"gl_class": r.get("gl_class"),
			"diff": r.get("difference"),
			"planner_status": r.get("planner_status"),
			"sql": r.get("sql_updates"),
			"eligible": r.get("eligible"),
			"sle_poisoned": r.get("sle_poisoned"),
		}
		for r in (scan.get("rows") or [])
		if (
			r.get("planner_status") in READY_STATUSES
			or str(r.get("planner_status") or "").startswith("READY")
		)
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
	]
	print(json.dumps({"by_class": scan.get("by_class"), "ready": ready, "n_ready": len(ready)}, ensure_ascii=False, indent=2, default=str)[:4000])
	return ready
