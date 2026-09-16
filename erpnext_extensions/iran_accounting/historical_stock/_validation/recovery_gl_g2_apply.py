# Copyright (c) 2026 — Apply READY G2_MISSING GL rebuilds (SLE-healthy only)
from __future__ import annotations

import json


def run(*, max_n=10, dry_run=0):
	import frappe
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
		rebuild_gl_for_voucher,
		scan_gl_integrity,
	)
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	scan = scan_gl_integrity(company="اسپاد فارمد دارو", limit=500)
	ready = [
		r
		for r in (scan.get("rows") or [])
		if (
			r.get("planner_status") in READY_STATUSES
			or str(r.get("planner_status") or "").startswith("READY")
		)
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
		and not r.get("sle_poisoned")
		and str(r.get("gl_class") or "") == "G2_MISSING"
	]
	results = []
	for r in ready[: int(max_n)]:
		v = r.get("voucher")
		se = frappe.get_doc("Stock Entry", v)
		expected = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
		if not expected:
			results.append({"voucher": v, "ok": False, "reason": "NO_EXPECTED_GL"})
			continue
		dry = rebuild_gl_for_voucher(v, dry_run=True)
		if dry.get("blocked"):
			results.append(
				{
					"voucher": v,
					"ok": False,
					"reason": dry.get("reason") or dry.get("planner_status"),
					"phase": "dry",
				}
			)
			continue
		if int(dry_run):
			results.append({"voucher": v, "ok": True, "dry_run": True, "before": r.get("gl_class")})
			continue
		applied = rebuild_gl_for_voucher(v, dry_run=False)
		ok = bool(applied.get("written")) and not applied.get("blocked")
		if ok:
			frappe.db.commit()
		results.append(
			{
				"voucher": v,
				"ok": ok,
				"before": r.get("gl_class"),
				"after": applied.get("gl_class"),
				"written": applied.get("written"),
				"reason": applied.get("reason"),
			}
		)
	out = {
		"n_ready": len(ready),
		"n_attempted": len(results),
		"n_repaired": sum(1 for x in results if x.get("ok") and not x.get("dry_run")),
		"n_failed": sum(1 for x in results if not x.get("ok")),
		"results": results,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
