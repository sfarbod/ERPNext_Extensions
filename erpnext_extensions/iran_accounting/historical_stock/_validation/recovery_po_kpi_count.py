from __future__ import annotations
import json
from collections import Counter
def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result
	scan = stamp_scan_result(run_full_history_scan(company="اسپاد فارمد دارو"))
	rows = scan.get("rows") or []
	opt = Counter((r.get("optimizer_status") or r.get("status") or "") for r in rows)
	ps = Counter((r.get("planner_status") or "") for r in rows)
	actionable = [
		r for r in rows
		if (r.get("optimizer_status") or r.get("status")) not in ("NO_REPAIR_NEEDED",)
		and (r.get("planner_status") or "") not in ("NO_REPAIR_PATH",)
	]
	print(json.dumps({
		"raw": len(rows),
		"actionable": len(actionable),
		"by_opt": dict(opt),
		"by_ps": dict(ps),
		"actionable_by_opt": dict(Counter((r.get("optimizer_status") or r.get("status") or "") for r in actionable)),
	}, ensure_ascii=False, indent=2))
