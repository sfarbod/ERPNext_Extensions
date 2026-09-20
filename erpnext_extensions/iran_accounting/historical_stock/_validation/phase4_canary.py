# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 4 canary — repair earliest independent Zero Rate root chain(s).

  # dry-run one root
  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.phase4_canary.run \\
    --kwargs "{'n_roots': 1, 'apply': 0}"

  # apply 1 → 3 → 5
  ... --kwargs "{'n_roots': 1, 'apply': 1}"
"""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"

# Prefer Material Issue / Transfer roots (no Material Receipt invent risk).
PREFERRED_PURPOSES = {
	"Material Issue",
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Repack",
	"Manufacture",
}


def _neg_counts():
	import frappe

	return {
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"neg_incoming": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0 AND actual_qty > 0 AND incoming_rate < -0.0001
			"""
		)[0][0],
		"i1": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
	}


def _select_roots(zrows, *, n_roots: int) -> list[dict]:
	"""Earliest independent EXACT/RECONSTRUCTABLE zero rows, one chain per voucher."""
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	cands = []
	for r in zrows:
		purpose = r.get("purpose") or ""
		if purpose not in PREFERRED_PURPOSES:
			continue
		if purpose == "Material Receipt":
			continue
		st = str(r.get("status") or "")
		ps = str(r.get("planner_status") or "")
		if not (
			r.get("eligible")
			or st == "RECONSTRUCTABLE"
			or ps in READY_STATUSES
			or ps.startswith("READY")
		):
			continue
		if abs(float(r.get("proposed_rate") or 0)) <= 0:
			continue
		# Skip rows waiting on another patient.
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v and pz_v != r.get("voucher"):
			continue
		cands.append(r)

	cands.sort(
		key=lambda r: (
			str(r.get("posting_date") or "9999"),
			str(r.get("posting_time") or ""),
			str(r.get("voucher") or ""),
		)
	)
	# One representative row per voucher (root chain batch).
	by_v = {}
	for r in cands:
		v = r.get("voucher")
		if v and v not in by_v:
			by_v[v] = r
	return list(by_v.values())[: int(n_roots)]


def run(*, n_roots: int = 1, apply: int = 0, voucher: str | None = None):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_item_warehouse,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	out = {
		"phase": "PHASE_4_CANARY",
		"n_roots": int(n_roots),
		"apply": int(apply),
		"before_neg": _neg_counts(),
		"active_riv": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabRepost Item Valuation`
			WHERE docstatus=1 AND status IN ('Queued','In Progress')
			"""
		)[0][0],
	}
	if out["active_riv"]:
		out["verdict"] = "RIV_INCIDENT_STOPPED_CAMPAIGN"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	if out["before_neg"]["neg_valuation"] or out["before_neg"]["neg_incoming"]:
		out["verdict"] = "PHASE_4_BLOCKED_NEGATIVE_REGRESSION"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	zscan = scan_zero_rate_rows(company=COMPANY, limit=8000)
	zrows = [attach_plan(dict(r)) for r in (zscan.get("rows") or [])]
	out["zero_before"] = {
		"raw": zscan.get("raw_count"),
		"recon": zscan.get("reconstructable_count"),
		"waiting": zscan.get("waiting_upstream_count"),
		"by_status": dict(Counter(r.get("status") for r in zrows)),
	}

	if voucher:
		roots = [r for r in zrows if r.get("voucher") == voucher and r.get("eligible")]
		# include all eligible rows on that voucher
		if not roots:
			roots = [r for r in zrows if r.get("voucher") == voucher]
	else:
		picked = _select_roots(zrows, n_roots=n_roots)
		# Expand to all eligible rows on selected vouchers (root-chain batch).
		vouchers = {r.get("voucher") for r in picked}
		roots = [
			r
			for r in zrows
			if r.get("voucher") in vouchers
			and (r.get("eligible") or r.get("status") == "RECONSTRUCTABLE")
		]
		# Ensure at least the picked representatives.
		have = {r.get("voucher_detail") for r in roots}
		for r in picked:
			if r.get("voucher_detail") not in have:
				roots.append(r)

	out["selected"] = [
		{
			"voucher": r.get("voucher"),
			"detail": r.get("voucher_detail"),
			"purpose": r.get("purpose"),
			"item": r.get("item"),
			"warehouse": r.get("warehouse") or r.get("s_warehouse") or r.get("t_warehouse"),
			"proposed_rate": r.get("proposed_rate"),
			"source": r.get("source_of_truth"),
			"confidence": r.get("confidence"),
			"planner": r.get("planner_status"),
			"eligible": r.get("eligible"),
			"posting_date": r.get("posting_date"),
		}
		for r in roots
	]
	out["selected_vouchers"] = sorted({r.get("voucher") for r in roots if r.get("voucher")})

	if not roots:
		out["verdict"] = "PHASE_4_NO_SAFE_ROOT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	# Wrong Rate on same vouchers (before) — healing measure.
	w_before = scan_wrong_rates(company=COMPANY, voucher=None, limit=500)
	# Filter in python for selected vouchers (API may not take multi voucher).
	sel = set(out["selected_vouchers"])
	w_before_n = sum(1 for r in (w_before.get("rows") or []) if r.get("voucher") in sel)
	out["wrong_on_selected_before"] = w_before_n

	dry = hr_api.repair_zero_rate_selected(rows=roots, dry_run=True)
	out["dry_run"] = {
		"aborted": dry.get("aborted"),
		"applied_n": len(dry.get("applied") or []),
		"blocked_n": len(dry.get("blocked") or []),
		"sql_updates": dry.get("sql_updates_executed"),
		"reason": dry.get("reason"),
		"blocked_sample": (dry.get("blocked") or [])[:3],
	}
	if dry.get("aborted") or not (dry.get("applied") or []):
		out["verdict"] = "PHASE_4_NEEDS_TOOL_DEVELOPMENT"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	if int(apply) != 1:
		out["verdict"] = "PHASE_4_CANARY_DRY_OK"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	applied = hr_api.repair_zero_rate_selected(rows=roots, dry_run=False)
	frappe.db.commit()
	out["apply_result"] = {
		"aborted": applied.get("aborted"),
		"applied_n": len(applied.get("applied") or []),
		"blocked_n": len(applied.get("blocked") or []),
		"sql_updates": applied.get("sql_updates_executed"),
		"repair_run_id": applied.get("repair_run_id"),
		"reason": applied.get("reason"),
	}

	# Incremental rescan per identity
	rescans = []
	for r in roots:
		item = r.get("item")
		wh = r.get("warehouse") or r.get("s_warehouse") or r.get("t_warehouse")
		if not item or not wh:
			continue
		try:
			rescans.append(rescan_item_warehouse(item_code=item, warehouse=wh, company=COMPANY))
		except Exception as exc:
			rescans.append({"error": str(exc), "item": item, "warehouse": wh})
	out["rescans_n"] = len(rescans)

	z_after = scan_zero_rate_rows(company=COMPANY, limit=8000)
	out["zero_after"] = {
		"raw": z_after.get("raw_count"),
		"recon": z_after.get("reconstructable_count"),
		"waiting": z_after.get("waiting_upstream_count"),
		"by_status": dict(Counter(r.get("status") for r in (z_after.get("rows") or []))),
	}
	out["ZERO_HEALED"] = int(out["zero_before"]["raw"] or 0) - int(out["zero_after"]["raw"] or 0)

	w_after = scan_wrong_rates(company=COMPANY, limit=500)
	w_after_n = sum(1 for r in (w_after.get("rows") or []) if r.get("voucher") in sel)
	out["wrong_on_selected_after"] = w_after_n
	out["WRONG_HEALED_ON_SELECTED"] = w_before_n - w_after_n

	out["after_neg"] = _neg_counts()
	try:
		i4 = scan_i4_leftover(company=COMPANY, limit=200)
		out["i4_after_count"] = i4.get("count")
	except Exception as exc:
		out["i4_after_error"] = str(exc)

	worsened = (
		out["after_neg"]["neg_valuation"] > out["before_neg"]["neg_valuation"]
		or out["after_neg"]["neg_incoming"] > out["before_neg"]["neg_incoming"]
	)
	out["DIRECT_ROOT_REPAIRED"] = out["apply_result"]["applied_n"]
	if worsened:
		out["verdict"] = "RIV_INCIDENT_STOPPED_CAMPAIGN"
		out["stop_reason"] = "negative_rate_worsened"
	elif out["apply_result"]["aborted"]:
		out["verdict"] = "PHASE_4_NEEDS_TOOL_DEVELOPMENT"
	else:
		out["verdict"] = "PHASE_4_CANARY_APPLY_OK"

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
