# Copyright (c) 2026 — Focused recovery drain for v5.2.18 (no version bump)
from __future__ import annotations

import json
from collections import Counter
from time import perf_counter

COMPANY = "اسپاد فارمد دارو"


def _print(obj):
	print(json.dumps(obj, ensure_ascii=False, default=str, indent=2))


def snap():
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES, attach_plan

	full = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
	dash = full.get("dashboard") or {}
	po = run_full_history_scan(company=COMPANY)
	by_opt = Counter()
	ready_po = []
	cache = {}
	for raw in po.get("rows") or []:
		opt = str(raw.get("optimizer_status") or raw.get("status") or "")
		if opt == "NO_REPAIR_NEEDED":
			continue
		by_opt[opt] += 1
		r = attach_plan(dict(raw), cache=cache)
		ps = str(r.get("planner_status") or "")
		if ps in READY_STATUSES and int(r.get("sql_updates") or 0) > 0 and r.get("eligible"):
			ready_po.append(r)
	return {
		"dashboard": {
			"Posting Order": dash.get("Posting Order"),
			"Wrong Rate": dash.get("Wrong Rate"),
			"Wrong Rate READY": dash.get("Wrong Rate READY"),
			"Zero Rate": dash.get("Zero Rate"),
			"I4 Leftover": dash.get("I4 Leftover"),
			"READY_I4": dash.get("READY_I4"),
			"Broken GL": dash.get("Broken GL"),
			"GL READY": dash.get("GL READY"),
			"Failed RIV": dash.get("Failed RIV"),
			"RIV SAFE": dash.get("RIV SAFE"),
			"Repairable": dash.get("Repairable"),
			"Patient Zero": dash.get("Patient Zero"),
			"Integrity Score": dash.get("Integrity Score"),
		},
		"po_by_opt": dict(by_opt),
		"po_ready": len(ready_po),
		"ready_po": ready_po,
		"full": full,
	}


def apply_gl(max_n=30):
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
		scan_gl_integrity,
		rebuild_gl_for_voucher,
	)
	import frappe

	scan = scan_gl_integrity(company=COMPANY, limit=500)
	ready = [
		r
		for r in (scan.get("rows") or [])
		if str(r.get("planner_status") or "") in READY_STATUSES
		and int(r.get("sql_updates") or 0) > 0
		and r.get("eligible")
		and not r.get("sle_poisoned")
	]
	out = {"n_ready": len(ready), "applied": [], "noop": [], "failed": []}
	for r in ready[:max_n]:
		vn = r.get("voucher")
		try:
			res = rebuild_gl_for_voucher(vn, dry_run=False)
			if res.get("written") and not res.get("blocked"):
				frappe.db.commit()
				from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
					classify_stock_entry_gl,
				)

				after = classify_stock_entry_gl(vn)
				out["applied"].append(
					{
						"voucher": vn,
						"before": r.get("gl_class"),
						"after": after.get("gl_class"),
						"after_ps": after.get("planner_status"),
					}
				)
			else:
				out["noop"].append({"voucher": vn, "reason": res.get("reason") or res.get("planner_status")})
		except Exception as e:
			frappe.db.rollback()
			out["failed"].append({"voucher": vn, "error": str(e)})
	return out


def apply_wr(max_n=20):
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)
	import frappe

	scan = scan_wrong_rates(company=COMPANY, limit=5000)
	ready = []
	for r in scan.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if (ps in READY_STATUSES or ps.startswith("READY")) and int(r.get("sql_updates") or 0) > 0 and r.get("eligible"):
			ready.append(r)
	out = {"n_ready": len(ready), "applied": [], "noop": [], "failed": []}
	for r in ready[:max_n]:
		try:
			res = apply_wrong_rate_root(r, dry_run=False)
			if res.get("ok") and res.get("written"):
				frappe.db.commit()
				out["applied"].append({"voucher": r.get("voucher"), "item": r.get("item"), "res": res.get("reason")})
			else:
				out["noop"].append({"voucher": r.get("voucher"), "item": r.get("item"), "res": res})
		except Exception as e:
			frappe.db.rollback()
			out["failed"].append({"voucher": r.get("voucher"), "error": str(e)})
	return out


def probe_po_unlocks(limit=40):
	"""Probe REAL_STOCK_SHORTAGE / CROSS_ITEM for unique inbound or joint coalesce."""
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.shortage import (
		search_hidden_inbound,
	)

	po = run_full_history_scan(company=COMPANY)
	by = Counter()
	unique = []
	cross = []
	for r in po.get("rows") or []:
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		if opt == "NO_REPAIR_NEEDED":
			continue
		by[opt] += 1
		if opt == "REAL_STOCK_SHORTAGE" and len(unique) < limit:
			sh = search_hidden_inbound(r)
			if sh.get("outcome") != "REAL_STOCK_SHORTAGE" or sh.get("unique_inbound"):
				unique.append(
					{
						"outbound": r.get("outbound_document") or r.get("voucher"),
						"item": r.get("item"),
						"wh": r.get("warehouse"),
						"outcome": sh.get("outcome"),
						"unique": bool(sh.get("unique_inbound")),
						"n_candidates": sh.get("n_candidates") or len(sh.get("candidates") or []),
						"reason": sh.get("reason"),
					}
				)
		if opt == "CROSS_ITEM_CONFLICT" and len(cross) < 20:
			cross.append(
				{
					"outbound": r.get("outbound_document") or r.get("voucher"),
					"inbound": r.get("inbound_document"),
					"item": r.get("item"),
					"wh": r.get("warehouse"),
					"docs": r.get("docs_changed") or r.get("moves"),
					"reason": r.get("dependency_reason"),
				}
			)
	return {"by_opt": dict(by), "unique_hits": unique, "n_unique_hits": len(unique), "cross_sample": cross}


def run():
	t0 = perf_counter()
	before = snap()
	gl = apply_gl(max_n=30)
	wr = apply_wr(max_n=20)
	probe = probe_po_unlocks(limit=60)
	after = snap()
	out = {
		"elapsed_s": round(perf_counter() - t0, 2),
		"before": before["dashboard"],
		"after": after["dashboard"],
		"po_by_opt_before": before["po_by_opt"],
		"po_by_opt_after": after["po_by_opt"],
		"gl": {k: gl[k] if k != "applied" else gl["applied"][:15] for k in gl},
		"wr": wr,
		"po_probe": {
			"n_unique_hits": probe["n_unique_hits"],
			"unique_sample": probe["unique_hits"][:15],
			"cross_sample": probe["cross_sample"][:10],
		},
	}
	_print(out)
	return out
