# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B Transfer canary — earliest EXACT/RECONSTRUCTABLE transfer roots.

  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.phase5b_transfer_canary.run \\
    --kwargs "{'n_roots': 1, 'apply': 1}"
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
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import count_wrong_rate_buckets
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_item_warehouse,
	)
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		RECONSTRUCTABLE,
		collapse_transfer_roots,
	)

	out = {
		"phase": "PHASE_5B_TRANSFER_CANARY",
		"n_roots": int(n_roots),
		"apply": int(apply),
		"before_neg": _neg(),
	}
	if out["before_neg"]["neg_valuation"] or out["before_neg"]["neg_incoming"] or out["before_neg"]["neg_fg"]:
		out["verdict"] = "PHASE_5B_BLOCKED_NEGATIVE_REGRESSION"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	w = scan_wrong_rates(company=COMPANY, limit=6000)
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	out["wrong_before"] = count_wrong_rate_buckets(wrows)

	TRANSFER = {
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	}
	cands = []
	for r in wrows:
		if (r.get("purpose") or "") not in TRANSFER:
			continue
		vn = str(r.get("voucher") or "")
		if not vn.startswith("MAT-STE-") and not vn.startswith("STE-"):
			continue
		tr = r.get("transfer_reconstruction") or {}
		cls = tr.get("classification")
		# Prefer EXACT (outgoing SVD). RECONSTRUCTABLE only when no upstream PZ.
		if cls not in (EXACT, RECONSTRUCTABLE):
			continue
		if not r.get("eligible") and abs(float(r.get("proposed_rate") or 0)) < 0.0001:
			continue
		if abs(float(r.get("proposed_rate") or r.get("expected_rate") or 0)) < 0.0001:
			continue
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		root = tr.get("root_voucher") or r.get("voucher")
		# Skip downstream of a different patient-zero / root.
		if pz_v and pz_v != r.get("voucher") and pz_v != root:
			continue
		if root and root != r.get("voucher") and cls != EXACT:
			continue
		# Prefer EXACT first; defer RECONSTRUCTABLE with upstream health unknown.
		if cls == RECONSTRUCTABLE and tr.get("upstream_health") not in ("healthy", None):
			continue
		if str(r.get("status") or "") == "DEPENDENCY_REPAIR_REQUIRED":
			continue
		if str(r.get("planner_status") or "").startswith("WAITING"):
			continue
		cands.append(r)

	# Sort EXACT before RECONSTRUCTABLE, then chronological.
	cands.sort(
		key=lambda r: (
			0 if (r.get("transfer_reconstruction") or {}).get("classification") == EXACT else 1,
			str(r.get("posting_date") or "9999"),
			str(r.get("voucher") or ""),
		)
	)
	# Collapse to one row per root voucher (earliest).
	by_root = {}
	for r in cands:
		tr = r.get("transfer_reconstruction") or {}
		root = tr.get("root_voucher") or r.get("voucher")
		if root and root not in by_root:
			by_root[root] = r
	picked_roots = list(by_root.keys())[: int(n_roots)]
	roots = [by_root[k] for k in picked_roots]
	# Expand: all eligible findings on those root vouchers.
	picked_set = set(picked_roots)
	expanded = [
		r
		for r in cands
		if (r.get("transfer_reconstruction") or {}).get("root_voucher") in picked_set
		or r.get("voucher") in picked_set
	]
	if not expanded:
		expanded = roots

	out["selected_roots"] = picked_roots
	out["selected_n"] = len(expanded)
	out["collapse_preview"] = collapse_transfer_roots(cands)
	out["selected_sample"] = [
		{
			"voucher": r.get("voucher"),
			"purpose": r.get("purpose"),
			"item": r.get("item"),
			"cls": (r.get("transfer_reconstruction") or {}).get("classification"),
			"expected": r.get("proposed_rate") or r.get("expected_rate"),
			"current": r.get("current_rate"),
			"source": (r.get("transfer_reconstruction") or {}).get("authoritative_source"),
		}
		for r in expanded[:20]
	]
	if not expanded:
		out["verdict"] = "PHASE_5B_NO_SAFE_TRANSFER_ROOT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	dry = hr_api.repair_wrong_rates_selected(rows=expanded, dry_run=True)
	out["dry_run"] = {
		"aborted": dry.get("aborted"),
		"applied_n": len(dry.get("applied") or []),
		"blocked_n": len(dry.get("blocked") or []),
		"reason": dry.get("reason") or dry.get("skip_reason"),
		"blocked_sample": [
			{"error": b.get("error") or b.get("reason"), "voucher": (b.get("row") or {}).get("voucher")}
			for b in (dry.get("blocked") or [])[:5]
		],
	}
	if dry.get("aborted") or not (dry.get("applied") or []):
		out["verdict"] = "PHASE_5B_NEEDS_FURTHER_TOOL_DEVELOPMENT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	if int(apply) != 1:
		out["verdict"] = "PHASE_5B_TRANSFER_CANARY_DRY_OK"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	applied = hr_api.repair_wrong_rates_selected(rows=expanded, dry_run=False)
	frappe.db.commit()
	out["apply_result"] = {
		"aborted": applied.get("aborted"),
		"applied_n": len(applied.get("applied") or []),
		"blocked_n": len(applied.get("blocked") or []),
		"sql_updates": applied.get("sql_updates_executed"),
		"repair_run_id": applied.get("repair_run_id"),
		"reason": applied.get("reason") or applied.get("skip_reason"),
	}
	for r in expanded:
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse") or r.get("t_warehouse") or r.get("s_warehouse")
		if item and wh:
			try:
				rescan_item_warehouse(item_code=item, warehouse=wh, company=COMPANY)
			except Exception:
				pass

	w2 = scan_wrong_rates(company=COMPANY, limit=6000)
	w2rows = [attach_plan(dict(r)) for r in (w2.get("rows") or [])]
	out["wrong_after"] = count_wrong_rate_buckets(w2rows)
	xfer_after = Counter()
	for r in w2rows:
		if (r.get("purpose") or "") in TRANSFER:
			cls = (r.get("transfer_reconstruction") or {}).get("classification") or "?"
			xfer_after[cls] += 1
	out["transfer_after"] = dict(xfer_after)
	z = scan_zero_rate_rows(company=COMPANY, limit=8000)
	out["zero_after"] = {
		"raw": z.get("raw_count") or z.get("count"),
		"recon": z.get("reconstructable_count"),
		"waiting": z.get("waiting_upstream_count"),
	}
	out["after_neg"] = _neg()
	out["DIRECT_ROOT_REPAIRED"] = out["apply_result"]["applied_n"]
	out["HEALED_BY_UPSTREAM_TRANSFER"] = max(
		0, (out["wrong_before"].get("ACTIONABLE") or 0) - (out["wrong_after"].get("ACTIONABLE") or 0)
	)
	if (
		out["after_neg"]["neg_valuation"] > out["before_neg"]["neg_valuation"]
		or out["after_neg"]["neg_incoming"] > out["before_neg"]["neg_incoming"]
		or out["after_neg"]["neg_fg"] > out["before_neg"]["neg_fg"]
	):
		out["verdict"] = "RIV_INCIDENT_STOPPED_CAMPAIGN"
	elif out["apply_result"].get("aborted"):
		out["verdict"] = "PHASE_5B_NEEDS_FURTHER_TOOL_DEVELOPMENT"
	else:
		out["verdict"] = "PHASE_5B_TRANSFER_CANARY_APPLY_OK"
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
