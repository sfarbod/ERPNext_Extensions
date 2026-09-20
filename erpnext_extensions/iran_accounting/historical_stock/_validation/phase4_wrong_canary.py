# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 4 Wrong Rate canary — earliest READY_WRONG_RATE independent roots.

  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.phase4_wrong_canary.run \\
    --kwargs "{'n_roots': 3, 'apply': 1}"
"""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"


def _neg():
	import frappe

	return {
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"i1": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
	}


def run(*, n_roots: int = 3, apply: int = 0):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import count_wrong_rate_buckets
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_item_warehouse,
	)

	out = {"phase": "PHASE_4_WRONG_CANARY", "n_roots": int(n_roots), "apply": int(apply), "before_neg": _neg()}
	if out["before_neg"]["neg_valuation"]:
		out["verdict"] = "PHASE_4_BLOCKED_NEGATIVE_REGRESSION"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	w = scan_wrong_rates(company=COMPANY, limit=4000)
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	out["wrong_before"] = count_wrong_rate_buckets(wrows)

	cands = []
	for r in wrows:
		ps = str(r.get("planner_status") or "")
		if not (r.get("eligible") or ps in READY_STATUSES or ps.startswith("READY")):
			continue
		if int(r.get("sql_updates") or 0) <= 0:
			continue
		# Prefer Material Issue / Transfer / Manufacture; skip Receipt invent.
		if (r.get("purpose") or "") == "Material Receipt":
			continue
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v and pz_v != r.get("voucher") and not ps.startswith("READY"):
			continue
		cands.append(r)
	cands.sort(key=lambda r: (str(r.get("posting_date") or "9999"), str(r.get("voucher") or "")))
	by_v = {}
	for r in cands:
		v = r.get("voucher")
		if v and v not in by_v:
			by_v[v] = r
	picked_vs = list(by_v.keys())[: int(n_roots)]
	roots = [r for r in cands if r.get("voucher") in set(picked_vs)]
	out["selected_vouchers"] = picked_vs
	out["selected_n"] = len(roots)
	out["selected_sample"] = [
		{
			"voucher": r.get("voucher"),
			"purpose": r.get("purpose"),
			"item": r.get("item") or r.get("item_code"),
			"planner": r.get("planner_status"),
			"flag": r.get("flag"),
			"confidence": r.get("confidence"),
			"expected": r.get("expected_rate") or r.get("proposed_rate"),
		}
		for r in roots[:20]
	]
	if not roots:
		out["verdict"] = "PHASE_4_NO_SAFE_WRONG_ROOT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	dry = hr_api.repair_wrong_rates_selected(rows=roots, dry_run=True)
	out["dry_run"] = {
		"aborted": dry.get("aborted"),
		"applied_n": len(dry.get("applied") or []),
		"blocked_n": len(dry.get("blocked") or []),
		"reason": dry.get("reason"),
		"blocked_sample": (dry.get("blocked") or [])[:3],
	}
	if dry.get("aborted") or not (dry.get("applied") or []):
		out["verdict"] = "PHASE_4_NEEDS_TOOL_DEVELOPMENT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	if int(apply) != 1:
		out["verdict"] = "PHASE_4_WRONG_CANARY_DRY_OK"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	applied = hr_api.repair_wrong_rates_selected(rows=roots, dry_run=False)
	frappe.db.commit()
	out["apply_result"] = {
		"aborted": applied.get("aborted"),
		"applied_n": len(applied.get("applied") or []),
		"blocked_n": len(applied.get("blocked") or []),
		"sql_updates": applied.get("sql_updates_executed"),
		"repair_run_id": applied.get("repair_run_id"),
	}
	for r in roots[:15]:
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse") or r.get("s_warehouse") or r.get("t_warehouse")
		if item and wh:
			try:
				rescan_item_warehouse(item_code=item, warehouse=wh, company=COMPANY)
			except Exception:
				pass

	w2 = scan_wrong_rates(company=COMPANY, limit=4000)
	w2rows = [attach_plan(dict(r)) for r in (w2.get("rows") or [])]
	out["wrong_after"] = count_wrong_rate_buckets(w2rows)
	z = scan_zero_rate_rows(company=COMPANY, limit=8000)
	out["zero_after"] = {
		"raw": z.get("raw_count"),
		"recon": z.get("reconstructable_count"),
		"waiting": z.get("waiting_upstream_count"),
	}
	out["after_neg"] = _neg()
	out["DIRECT_ROOT_REPAIRED"] = out["apply_result"]["applied_n"]
	if out["after_neg"]["neg_valuation"] > out["before_neg"]["neg_valuation"]:
		out["verdict"] = "RIV_INCIDENT_STOPPED_CAMPAIGN"
	elif out["apply_result"].get("aborted"):
		out["verdict"] = "PHASE_4_NEEDS_TOOL_DEVELOPMENT"
	else:
		out["verdict"] = "PHASE_4_WRONG_CANARY_APPLY_OK"
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
