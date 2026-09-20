# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5 Posting Order canary — READY_WAREHOUSE_REPLAY only."""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


def run(*, apply: int = 0, n: int = 2):
	import frappe
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api

	po = run_full_history_scan(company=COMPANY)
	rows = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		ps = str(r.get("planner_status") or "")
		if ps.startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			rows.append(r)
	rows = rows[: int(n)]
	out = {
		"selected": [
			{
				"out": r.get("outbound_document"),
				"in": r.get("inbound_document"),
				"item": r.get("item"),
				"ps": r.get("planner_status"),
				"conf": r.get("confidence"),
				"opt": r.get("optimizer_status") or r.get("status"),
			}
			for r in rows
		]
	}
	if not rows:
		out["verdict"] = "NO_PO_READY"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	from erpnext_extensions.iran_accounting.stock_posting_order.api import repair_posting_order_selected

	dry = repair_posting_order_selected(rows=rows, dry_run=True)
	out["dry"] = {
		"aborted": (dry or {}).get("aborted"),
		"applied_n": len((dry or {}).get("applied") or []),
		"blocked_n": len((dry or {}).get("blocked") or []),
		"reason": (dry or {}).get("reason"),
	}
	if int(apply) != 1:
		out["verdict"] = "PO_CANARY_DRY"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	applied = repair_posting_order_selected(rows=rows, dry_run=False)
	frappe.db.commit()
	out["apply"] = {
		"aborted": applied.get("aborted"),
		"applied_n": len(applied.get("applied") or []),
		"blocked_n": len(applied.get("blocked") or []),
	}
	neg = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
	)[0][0]
	out["neg_valuation"] = neg
	out["verdict"] = "PO_CANARY_OK" if not applied.get("aborted") and neg == 0 else "PO_CANARY_STOP"
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
