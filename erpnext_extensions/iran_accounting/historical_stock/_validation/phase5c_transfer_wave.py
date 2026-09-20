# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5C Transfer drain — chunked apply to avoid TooManyWritesError.

Hard caps:
- max 100 roots requested per wave
- apply in chunks of ``chunk_size`` (default 25) with commit between chunks
"""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"
TRANSFER = {
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
}


def _neg():
	import frappe

	return {
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"neg_incoming": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND incoming_rate < -0.0001"
		)[0][0],
		"neg_fg": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
			JOIN `tabStock Entry` se ON se.name=sle.voucher_no
			WHERE sle.is_cancelled=0 AND se.purpose='Manufacture'
			  AND sle.actual_qty>0 AND sle.incoming_rate < -0.0001
			"""
		)[0][0],
	}


def _select_roots(n_roots: int):
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		RECONSTRUCTABLE,
		reconstruct_transfer_valuation,
		apply_transfer_reconstruction_to_row,
	)

	w = scan_wrong_rates(company=COMPANY, limit=6000)
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	cands = []
	for r in wrows:
		if (r.get("purpose") or "") not in TRANSFER:
			continue
		vn = str(r.get("voucher") or "")
		if not vn.startswith("MAT-STE-") and not vn.startswith("STE-"):
			continue
		r = apply_transfer_reconstruction_to_row(dict(r))
		tr = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
		r["transfer_reconstruction"] = tr
		cls = tr.get("classification")
		if cls not in (EXACT, RECONSTRUCTABLE):
			continue
		prop = float(r.get("proposed_rate") or r.get("expected_rate") or tr.get("expected_rate") or 0)
		if abs(prop) < 0.0001:
			continue
		# Skip near-equal (already healthy within 1 IRR)
		cur = float(r.get("current_rate") or 0)
		if abs(cur - prop) <= 1.0:
			continue
		r["proposed_rate"] = prop
		r["expected_rate"] = prop
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		root = tr.get("root_voucher") or r.get("voucher")
		self_exact = (
			cls == EXACT
			and tr.get("authoritative_source") in ("outgoing_sle_svd", "outgoing_sle_rate")
			and (not root or root == r.get("voucher"))
			and tr.get("upstream_health") in ("healthy", "missing", None)
		)
		if self_exact:
			r["eligible"] = True
			r["confidence"] = "EXACT"
			r["status"] = "RECONSTRUCTABLE"
			r["planner_status"] = "READY_WRONG_RATE"
			r["patient_zero"] = {"voucher_no": r.get("voucher")}
			r["sql_updates"] = max(int(r.get("sql_updates") or 0), 1)
			cands.append(r)
			continue
		if pz_v and pz_v != r.get("voucher") and pz_v != root:
			continue
		if root and root != r.get("voucher") and cls != EXACT:
			continue
		if cls == RECONSTRUCTABLE and tr.get("upstream_health") not in ("healthy", None):
			continue
		if str(r.get("status") or "") == "DEPENDENCY_REPAIR_REQUIRED":
			continue
		if str(r.get("planner_status") or "").startswith("WAITING"):
			continue
		cands.append(r)

	cands.sort(
		key=lambda r: (
			0 if (r.get("transfer_reconstruction") or {}).get("classification") == EXACT else 1,
			str(r.get("posting_date") or "9999"),
			str(r.get("voucher") or ""),
		)
	)
	by_root = {}
	for r in cands:
		tr = r.get("transfer_reconstruction") or {}
		root = tr.get("root_voucher") or r.get("voucher")
		if root and root not in by_root:
			by_root[root] = r
	picked = list(by_root.keys())[: int(n_roots)]
	return cands, picked, by_root


def run(*, n_roots: int = 50, chunk_size: int = 25, apply: int = 1):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_item_warehouse,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate

	n_roots = min(int(n_roots), 100)
	chunk_size = max(1, min(int(chunk_size), 25))  # physical apply chunk hard-cap 25
	out = {
		"phase": "PHASE_5C_TRANSFER_WAVE",
		"n_roots": n_roots,
		"chunk_size": chunk_size,
		"apply": int(apply),
		"before_neg": _neg(),
		"chunks": [],
	}
	if out["before_neg"]["neg_valuation"] or out["before_neg"]["neg_incoming"] or out["before_neg"]["neg_fg"]:
		out["verdict"] = "STOCK_REPAIR_INCIDENT_STOPPED_CAMPAIGN"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	cands, picked, by_root = _select_roots(n_roots)
	out["selected_roots"] = picked
	out["selected_root_n"] = len(picked)
	if not picked:
		out["verdict"] = "PHASE_5C_NO_SAFE_TRANSFER_ROOT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	total_applied = 0
	total_blocked = 0
	stopped = None
	# Wide candidate pool so multi-item vouchers expand fully at apply time.
	expand_pool, _, _ = _select_roots(max(n_roots * 4, 400))
	for i in range(0, len(picked), chunk_size):
		chunk_roots = picked[i : i + chunk_size]
		chunk_set = set(chunk_roots)
		expanded = [
			r
			for r in expand_pool
			if (r.get("transfer_reconstruction") or {}).get("root_voucher") in chunk_set
			or r.get("voucher") in chunk_set
		]
		# Deduplicate by (voucher, item, warehouse, voucher_detail)
		seen = set()
		uniq = []
		for r in expanded:
			key = (
				r.get("voucher"),
				r.get("item") or r.get("item_code"),
				r.get("warehouse") or r.get("t_warehouse"),
				r.get("voucher_detail") or r.get("voucher_detail_no"),
			)
			if key in seen:
				continue
			seen.add(key)
			uniq.append(r)
		expanded = uniq
		chunk_info = {"chunk": i // chunk_size + 1, "roots": chunk_roots, "findings": len(expanded)}
		if int(apply) != 1:
			dry = hr_api.repair_wrong_rates_selected(rows=expanded, dry_run=True)
			chunk_info["dry"] = {
				"applied_n": len(dry.get("applied") or []),
				"blocked_n": len(dry.get("blocked") or []),
				"aborted": dry.get("aborted"),
			}
			out["chunks"].append(chunk_info)
			continue
		try:
			dry = hr_api.repair_wrong_rates_selected(rows=expanded, dry_run=True)
			if dry.get("aborted") or not (dry.get("applied") or []):
				chunk_info["dry_fail"] = dry.get("reason") or dry.get("skip_reason") or "no applied"
				out["chunks"].append(chunk_info)
				stopped = "DRY_FAIL"
				break
			applied = hr_api.repair_wrong_rates_selected(rows=expanded, dry_run=False)
			frappe.db.commit()
			chunk_info["apply"] = {
				"applied_n": len(applied.get("applied") or []),
				"blocked_n": len(applied.get("blocked") or []),
				"aborted": applied.get("aborted"),
				"sql_updates": applied.get("sql_updates_executed"),
				"repair_run_id": applied.get("repair_run_id"),
			}
			total_applied += len(applied.get("applied") or [])
			total_blocked += len(applied.get("blocked") or [])
			for r in expanded:
				item = r.get("item") or r.get("item_code")
				wh = r.get("warehouse") or r.get("t_warehouse") or r.get("s_warehouse")
				if item and wh:
					try:
						rescan_item_warehouse(item_code=item, warehouse=wh, company=COMPANY)
					except Exception:
						pass
			neg = _neg()
			chunk_info["neg"] = neg
			i1 = (scan_i1_negative_rate(company=COMPANY, limit=200) or {}).get("count")
			chunk_info["i1"] = i1
			out["chunks"].append(chunk_info)
			if neg["neg_valuation"] or neg["neg_incoming"] or neg["neg_fg"] or i1:
				stopped = "SAFETY"
				out["verdict"] = "STOCK_REPAIR_INCIDENT_STOPPED_CAMPAIGN"
				break
		except Exception as exc:
			frappe.db.rollback()
			chunk_info["error"] = f"{type(exc).__name__}: {exc}"
			out["chunks"].append(chunk_info)
			stopped = type(exc).__name__
			out["verdict"] = "STOCK_REPAIR_INCIDENT_STOPPED_CAMPAIGN"
			break

	out["total_applied"] = total_applied
	out["total_blocked"] = total_blocked
	out["after_neg"] = _neg()
	out["i1"] = (scan_i1_negative_rate(company=COMPANY, limit=500) or {}).get("count")
	if stopped:
		out["stopped"] = stopped
	elif int(apply) != 1:
		out["verdict"] = "PHASE_5C_TRANSFER_WAVE_DRY_OK"
	else:
		out["verdict"] = "PHASE_5C_TRANSFER_WAVE_APPLY_OK"
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
