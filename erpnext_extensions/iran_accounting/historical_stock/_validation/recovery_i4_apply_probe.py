# Copyright (c) 2026 — compare I4 classify vs sle READY_I4 for one identity
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		repair_i4_selected,
		scan_i4_leftover,
	)
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from frappe.utils import nowdate

	company = "اسپاد فارمد دارو"
	sle = scan_sle_bin(company=company, limit=500)
	ready = sorted(
		[
			r
			for r in (sle.get("rows") or [])
			if str(r.get("planner_status") or "") == "READY_I4" and int(r.get("sql_updates") or 0) > 0
		],
		key=lambda r: int(r.get("sql_updates") or 9999),
	)
	one = ready[0] if ready else None
	out = {"sle_ready_i4": len(ready)}
	if not one:
		print(json.dumps(out))
		return out
	item = one.get("item") or one.get("item_code")
	wh = one.get("warehouse")
	out["one"] = {
		"voucher": one.get("voucher"),
		"item": item,
		"warehouse": wh,
		"sql": one.get("sql_updates"),
		"planner_status": one.get("planner_status"),
		"status": one.get("status"),
	}
	cls = classify_i4_row(item, wh, voucher=one.get("voucher"))
	planned = attach_plan(cls)
	out["classify"] = {
		k: planned.get(k)
		for k in (
			"i4_status",
			"planner_status",
			"eligible",
			"sql_updates",
			"reason",
			"patient_zero",
			"voucher",
			"status",
		)
	}
	i4 = scan_i4_leftover(
		company=company,
		item_code=item,
		warehouse=wh,
		from_date="2026-03-21",
		to_date=str(nowdate()),
		limit=50,
	)
	out["i4_scan"] = [
		{
			k: r.get(k)
			for k in ("voucher", "i4_status", "planner_status", "eligible", "sql_updates", "reason")
		}
		for r in (i4.get("rows") or [])[:5]
	]

	if planned.get("planner_status") == "READY_I4" and planned.get("eligible"):
		dry = repair_i4_selected([planned], dry_run=True)
		out["dry"] = {
			"aborted": dry.get("aborted"),
			"n_applied": len(dry.get("applied") or []),
			"n_blocked": len(dry.get("blocked") or []),
			"blocked0": (dry.get("blocked") or [None])[0],
		}
		if not dry.get("aborted") and not dry.get("blocked"):
			applied = repair_i4_selected([planned], dry_run=False)
			out["apply"] = {
				"aborted": applied.get("aborted"),
				"n_applied": len(applied.get("applied") or []),
				"n_blocked": len(applied.get("blocked") or []),
			}
	else:
		out["skip_apply"] = (
			f"dedicated classify is {planned.get('planner_status')} "
			f"eligible={planned.get('eligible')} reason={planned.get('reason')}"
		)

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
