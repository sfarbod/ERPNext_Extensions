# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B — diagnose PO READY dry vs apply mismatch."""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


def run():
	import frappe
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, assert_ready
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import dry_run_selected, apply_repairs

	po = run_full_history_scan(company=COMPANY)
	ready = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		ps = str(r.get("planner_status") or "")
		if ps.startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			ready.append(r)
	out = {"ready_n": len(ready), "cases": []}
	for r in ready[:5]:
		case = {
			"out": r.get("outbound_document"),
			"in": r.get("inbound_document"),
			"item": r.get("item"),
			"ps": r.get("planner_status"),
			"conf": r.get("confidence"),
			"opt": r.get("optimizer_status") or r.get("status"),
			"has_sig": bool(r.get("dependency_signature")),
			"has_proposed": bool(r.get("proposed_outbound_time")),
			"has_cur_out": bool(r.get("current_outbound_time")),
			"has_cur_in": bool(r.get("current_inbound_time")),
			"sql": r.get("sql_updates"),
			"keys": sorted([k for k, v in r.items() if v not in (None, "", [], {})])[:40],
		}
		# assert_ready
		try:
			assert_ready(dict(r))
			case["assert_ready"] = "OK"
		except Exception as exc:
			case["assert_ready"] = str(exc)[:300]
		dry = dry_run_selected([dict(r)])
		case["dry"] = {
			"aborted": dry.get("aborted"),
			"applied_n": len(dry.get("applied") or []),
			"blocked_n": len(dry.get("blocked") or []),
			"blocked": [
				{
					"reason": b.get("reason") or b.get("blocker") or b.get("message"),
					"ps": b.get("planner_status"),
				}
				for b in (dry.get("blocked") or [])[:2]
			],
			"applied_sample_keys": sorted((dry.get("applied") or [{}])[0].keys())[:20]
			if dry.get("applied")
			else [],
		}
		# apply dry_run=False path but we only want to see first blocker — use dry_run via apply_repairs(True) vs False carefully
		# Call apply with dry_run False inside a savepoint? Better: inspect apply_repairs blocked reasons by monkey-patching
		# Instead re-run apply_repairs with dry_run=True which uses dry_run_selected — need apply False path.
		# Use a transaction rollback:
		frappe.db.begin()
		try:
			applied = apply_repairs([dict(r)], dry_run=False)
			case["apply"] = {
				"aborted": applied.get("aborted"),
				"applied_n": len(applied.get("applied") or []),
				"blocked_n": len(applied.get("blocked") or []),
				"blocked": [
					{
						"reason": b.get("reason") or b.get("blocker") or b.get("message") or b.get("skip_reason"),
						"ps": b.get("planner_status"),
						"keys": sorted([k for k in (b or {}).keys()])[:25],
					}
					for b in (applied.get("blocked") or [])[:3]
				],
			}
		finally:
			frappe.db.rollback()
		out["cases"].append(case)
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
