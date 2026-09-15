# Copyright (c) 2026 — GL root inventory + controlled proofs (honest residual check)
from __future__ import annotations

import json


def run(*, apply=0, max_proofs=2, voucher=None):
	import frappe
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
		classify_stock_entry_gl,
		rebuild_gl_for_voucher,
		scan_gl_integrity,
	)

	scan = scan_gl_integrity(company="اسپاد فارمد دارو", limit=300)
	rows = scan.get("rows") or []
	by = scan.get("by_class") or {}
	ready = [
		r
		for r in rows
		if r.get("eligible")
		and str(r.get("planner_status") or "").startswith("READY")
		and int(r.get("sql_updates") or 0) > 0
		and not r.get("sle_poisoned")
		and str(r.get("gl_class") or "")
		in (
			"G1_BALANCED_BUT_ECONOMICALLY_WRONG",
			"G3_UNBALANCED",
		)
	]
	viable = []
	for r in ready:
		se = frappe.get_doc("Stock Entry", r["voucher"])
		expected = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
		if expected:
			viable.append(r)
	if voucher:
		viable = [classify_stock_entry_gl(voucher)] + [x for x in viable if x.get("voucher") != voucher]
	viable = sorted(viable, key=lambda r: abs(float(r.get("difference") or 0)))
	out = {
		"count": scan.get("count"),
		"by_class": by,
		"ready_g1_g3": len(ready),
		"viable_with_gl_map": len(viable),
		"sample": [
			{"voucher": r.get("voucher"), "gl_class": r.get("gl_class"), "diff": r.get("difference")}
			for r in viable[:10]
		],
	}
	proofs = []
	if int(apply) and viable:
		for r in viable[: int(max_proofs)]:
			v = r.get("voucher")
			dry = rebuild_gl_for_voucher(v, dry_run=True)
			if dry.get("blocked"):
				proofs.append(
					{
						"voucher": v,
						"ok": False,
						"stage": "dry",
						"reason": dry.get("reason") or dry.get("planner_status"),
					}
				)
				continue
			applied = rebuild_gl_for_voucher(v, dry_run=False)
			ok = (
				bool(applied.get("written"))
				and not applied.get("blocked")
				and applied.get("gl_class") in ("G0_HEALTHY", "G1_BALANCED_BUT_ECONOMICALLY_WRONG")
				and abs(float(applied.get("stored_debit") or 0) - float(applied.get("stored_credit") or 0)) < 0.5
				and float(applied.get("stored_debit") or 0) > 0
			)
			proofs.append(
				{
					"voucher": v,
					"ok": ok,
					"before_class": (applied.get("before") or {}).get("gl_class"),
					"after_class": applied.get("gl_class"),
					"debit": applied.get("stored_debit"),
					"credit": applied.get("stored_credit"),
					"dims_ok": applied.get("dimension_cost_centers_preserved"),
					"no_sa": applied.get("no_unexpected_stock_adjustment"),
					"no_ro": applied.get("no_unexpected_round_off"),
					"reason": applied.get("reason"),
				}
			)
			if ok:
				frappe.db.commit()
	out["proofs"] = proofs
	out["promotion"] = (
		"LIMITED_PROVEN"
		if sum(1 for p in proofs if p.get("ok")) >= 2
		else ("PARTIAL" if any(p.get("ok") for p in proofs) else "NOT_PROVEN")
	)
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:7000])
	return out
