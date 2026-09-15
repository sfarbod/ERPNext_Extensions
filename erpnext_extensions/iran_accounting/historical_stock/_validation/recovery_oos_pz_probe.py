# Copyright (c) 2026 — probe OUT_OF_SCAN patient zeros for engine unlock
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	import frappe

	company = "اسپاد فارمد دارو"
	targets = ["MAT-PRE-2026-00793-1", "MAT-RECO-2026-02784", "MAT-STE-2026-31064"]
	wrong = scan_wrong_rates(company=company, limit=5000)
	rows = wrong.get("rows") or []

	def _pz(r):
		p = r.get("patient_zero")
		return p.get("voucher_no") if isinstance(p, dict) else p

	out = {"targets": {}}
	for pz in targets:
		deps = [r for r in rows if _pz(r) == pz]
		# SLE summary
		sles = frappe.db.sql(
			"""
			SELECT item_code, warehouse, actual_qty, incoming_rate, valuation_rate,
			       stock_value_difference, qty_after_transaction, voucher_type
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND is_cancelled=0
			""",
			pz,
			as_dict=True,
		)
		# Does voucher exist and what type?
		vt = None
		for dt in ("Stock Entry", "Purchase Receipt", "Stock Reconciliation", "Purchase Invoice"):
			if frappe.db.exists(dt, pz):
				vt = dt
				break
		# For STE: classify if present
		classified = None
		if vt == "Stock Entry":
			# find matching wrong-rate style row for each dep item
			pass
		dep_items = Counter((r.get("item"), r.get("warehouse")) for r in deps)
		# Check if any dep item appears on the PZ voucher SLE with healthy rate
		sle_by_item = {}
		for s in sles:
			sle_by_item.setdefault(s.item_code, []).append(s)
		resolvable = []
		for r in deps[:20]:
			item = r.get("item")
			psles = sle_by_item.get(item) or []
			healthy = [s for s in psles if float(s.incoming_rate or 0) > 0 or float(s.valuation_rate or 0) > 0]
			resolvable.append(
				{
					"dep": r.get("voucher"),
					"item": item,
					"dep_ps": r.get("planner_status"),
					"dep_conf": r.get("confidence"),
					"dep_expected": r.get("expected") or r.get("proposed_rate"),
					"dep_source": r.get("source"),
					"pz_sle_n": len(psles),
					"pz_healthy_rate_n": len(healthy),
					"pz_rates": [
						{"ir": s.incoming_rate, "vr": s.valuation_rate, "qty": s.actual_qty}
						for s in healthy[:3]
					],
					"reason": (r.get("reason") or "")[:120],
				}
			)
		out["targets"][pz] = {
			"voucher_type": vt,
			"n_sle": len(sles),
			"n_deps_in_wr_scan": len(deps),
			"dep_items": [{"item": i, "wh": w, "n": n} for (i, w), n in dep_items.most_common(20)],
			"resolvable_sample": resolvable,
		}

	# CROSS_ITEM PO rows
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	po = run_full_history_scan(company=company)
	cross = [
		r
		for r in (po.get("rows") or [])
		if str(r.get("optimizer_status") or r.get("status") or "") == "CROSS_ITEM_CONFLICT"
	]
	out["cross_item"] = [
		{
			"in": r.get("inbound_voucher") or r.get("in_voucher"),
			"out": r.get("outbound_voucher") or r.get("out_voucher") or r.get("voucher"),
			"item": r.get("item"),
			"wh": r.get("warehouse"),
			"ps": r.get("planner_status"),
			"conf": r.get("confidence"),
			"reason": (r.get("reason") or "")[:160],
			"opt": r.get("optimizer_status"),
		}
		for r in cross
	]

	# G3
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity

	gl = scan_gl_integrity(company=company, limit=200)
	g3 = [r for r in (gl.get("rows") or []) if r.get("gl_class") == "G3_UNBALANCED"]
	g4 = [r for r in (gl.get("rows") or []) if r.get("gl_class") == "G4_BUILT_FROM_POISONED_SLE"]
	out["g3"] = [
		{
			"voucher": r.get("voucher"),
			"ps": r.get("planner_status"),
			"eligible": r.get("eligible"),
			"sql": r.get("sql_updates"),
			"diff": r.get("difference"),
			"poison": r.get("sle_poisoned"),
			"reason": (r.get("reason") or "")[:160],
		}
		for r in g3
	]
	out["g4"] = [
		{
			"voucher": r.get("voucher"),
			"ps": r.get("planner_status"),
			"eligible": r.get("eligible"),
			"reason": (r.get("reason") or "")[:160],
		}
		for r in g4
	]
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
