from __future__ import annotations
import json

def run():
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.engine import run_assisted_campaign
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import apply_wrong_rate_root

	camp = run_assisted_campaign(threshold=0.95)
	auto = list(camp.get("ready_rows") or [])[:5]
	out = []
	for r in auto:
		vn, item = r.get("voucher"), r.get("item")
		sles = frappe.db.sql(
			"""
			SELECT name, incoming_rate, valuation_rate, stock_value_difference, actual_qty, warehouse
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND is_cancelled=0
			""",
			(vn, item),
			as_dict=True,
		)
		dry = apply_wrong_rate_root(r, dry_run=True)
		out.append(
			{
				"voucher": vn,
				"item": item,
				"expected": r.get("expected") or r.get("proposed_rate"),
				"observed": r.get("observed") or r.get("current_rate") or r.get("rate"),
				"source": r.get("source") or r.get("source_of_truth"),
				"planner": r.get("planner_status"),
				"confidence": r.get("confidence") or r.get("assisted_confidence"),
				"sles": sles,
				"dry_apply": {k: dry.get(k) for k in ("ok", "after_rate", "expected", "reason", "changed", "would_write")},
			}
		)
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:10000])
	return out
