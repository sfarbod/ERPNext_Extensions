# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B Manufacture canary — EXACT/RECONSTRUCTABLE manufacture roots.

  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.phase5b_mfg_canary.run \\
    --kwargs "{'n_roots': 1, 'apply': 1}"
"""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


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


def run(*, n_roots: int = 1, apply: int = 0):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
		preview_manufacture_voucher,
		scan_manufacture_anomalies,
	)
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import count_wrong_rate_buckets
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_item_warehouse,
	)

	out = {
		"phase": "PHASE_5B_MFG_CANARY",
		"n_roots": int(n_roots),
		"apply": int(apply),
		"before_neg": _neg(),
	}
	if out["before_neg"]["neg_valuation"] or out["before_neg"]["neg_fg"]:
		out["verdict"] = "PHASE_5B_BLOCKED_NEGATIVE_REGRESSION"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	scan = scan_manufacture_anomalies(company=COMPANY, limit=800)
	rows = scan.get("rows") or []
	# Prefer EXACT eligible with healthy inputs.
	cands = []
	for r in rows:
		if not r.get("eligible"):
			continue
		ih = r.get("input_health") or {}
		if ih.get("status") == "poisoned":
			continue
		if r.get("confidence") != "EXACT" and not r.get("after_rates_healthy"):
			continue
		cands.append(r)
	cands.sort(key=lambda r: (str(r.get("voucher") or "")))
	by_v = {}
	for r in cands:
		v = r.get("voucher")
		if v and v not in by_v:
			# Refresh preview for structured fields.
			by_v[v] = preview_manufacture_voucher(v)
	picked = list(by_v.keys())[: int(n_roots)]
	roots = [by_v[k] for k in picked]
	out["selected_roots"] = picked
	out["selected_n"] = len(roots)
	out["selected_sample"] = [
		{
			"voucher": r.get("voucher"),
			"status": r.get("status"),
			"confidence": r.get("confidence"),
			"expected_rate": r.get("expected_target_rate"),
			"current_rate": r.get("current_target_rate"),
			"matched_but_corrupt": r.get("matched_but_corrupt"),
			"input_health": (r.get("input_health") or {}).get("status"),
			"zero_rm": r.get("zero_rm_present"),
			"zero_rm_cause": r.get("zero_rm_cause"),
		}
		for r in roots
	]
	if not roots:
		out["verdict"] = "PHASE_5B_NO_SAFE_MFG_ROOT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	dry = hr_api.repair_manufacture_selected_api(rows=roots, dry_run=True)
	out["dry_run"] = {
		"aborted": dry.get("aborted"),
		"applied_n": len(dry.get("applied") or []),
		"blocked_n": len(dry.get("blocked") or []),
		"reason": dry.get("reason") or dry.get("skip_reason"),
	}
	if dry.get("aborted") or not (dry.get("applied") or []):
		out["verdict"] = "PHASE_5B_NEEDS_FURTHER_TOOL_DEVELOPMENT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	if int(apply) != 1:
		out["verdict"] = "PHASE_5B_MFG_CANARY_DRY_OK"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	applied = hr_api.repair_manufacture_selected_api(rows=roots, dry_run=False)
	frappe.db.commit()
	out["apply_result"] = {
		"aborted": applied.get("aborted"),
		"applied_n": len(applied.get("applied") or []),
		"blocked_n": len(applied.get("blocked") or []),
		"reason": applied.get("reason") or applied.get("skip_reason"),
	}
	for r in roots:
		for ir in (r.get("input_rows") or [])[:5]:
			item = ir.get("item")
			wh = ir.get("warehouse")
			if item and wh:
				try:
					rescan_item_warehouse(item_code=item, warehouse=wh, company=COMPANY)
				except Exception:
					pass

	w2 = scan_wrong_rates(company=COMPANY, limit=6000)
	w2rows = [attach_plan(dict(r)) for r in (w2.get("rows") or [])]
	out["wrong_after"] = count_wrong_rate_buckets(w2rows)
	out["after_neg"] = _neg()
	out["HEALED_BY_MANUFACTURE_REBUILD"] = out["apply_result"]["applied_n"]
	if out["after_neg"]["neg_valuation"] or out["after_neg"]["neg_fg"]:
		out["verdict"] = "RIV_INCIDENT_STOPPED_CAMPAIGN"
	elif out["apply_result"].get("aborted"):
		out["verdict"] = "PHASE_5B_NEEDS_FURTHER_TOOL_DEVELOPMENT"
	else:
		out["verdict"] = "PHASE_5B_MFG_CANARY_APPLY_OK"
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
