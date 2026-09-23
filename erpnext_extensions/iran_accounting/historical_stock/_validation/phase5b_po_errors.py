# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B — print apply error strings for READY PO rows."""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


def run():
	import frappe
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs

	po = run_full_history_scan(company=COMPANY)
	ready = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		ps = str(r.get("planner_status") or "")
		if ps.startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			ready.append(r)
	out = []
	for r in ready:
		frappe.db.begin()
		try:
			res = apply_repairs([dict(r)], dry_run=False)
			out.append(
				{
					"out": r.get("outbound_document"),
					"in": r.get("inbound_document"),
					"item": r.get("item"),
					"ps": r.get("planner_status"),
					"min_before": r.get("min_qty_before"),
					"min_after": r.get("min_qty_after"),
					"reason": res.get("reason"),
					"blocked_errors": [b.get("error") for b in (res.get("blocked") or [])],
				}
			)
		finally:
			frappe.db.rollback()
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
