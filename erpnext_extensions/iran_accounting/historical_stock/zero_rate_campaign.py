# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 Zero Rate campaign — cluster → small SAFE_GROUP prove-out (no bulk)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from time import perf_counter

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
	SAFE_GROUP,
	cluster_independent_roots,
	identity_key,
)
from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
	_load_detail,
	repair_zero_rate_selected,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import (
	classify_zero_row,
	scan_zero_rate_rows,
)

RATE_EPS = 0.0001



from erpnext_extensions.iran_accounting.historical_stock.util import dump_artifact, resolve_company


def classify_zero_clusters(company=None, max_cluster=15) -> dict:
	company = resolve_company(company)
	scan = scan_zero_rate_rows(company=company)
	rows = scan.get("rows") or []
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	exact_ready = [
		r
		for r in rows
		if (r.get("confidence") == "EXACT")
		and (
			r.get("eligible")
			or r.get("planner_status") in READY_STATUSES
			or str(r.get("planner_status") or "").startswith("READY")
		)
		and abs(flt(r.get("proposed_rate") or 0)) > RATE_EPS
	]
	clusters = cluster_independent_roots(exact_ready, max_cluster=max_cluster)
	# Annotate each root
	for g in clusters.get("safe_groups") or []:
		for r in g.get("roots") or []:
			r["cluster_identity"] = list(identity_key(r))
			r["cross_warehouse"] = bool(r.get("s_warehouse") and r.get("t_warehouse") and r["s_warehouse"] != r["t_warehouse"])
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"total_zero_rows": scan.get("count"),
		"exact_ready": len(exact_ready),
		"clusters": clusters,
		"by_confidence": scan.get("by_confidence"),
		"promotion_status": "NOT_PROVEN",
	}
	dump_artifact("zero_rate", "clusters.json", out)
	return out


def preview_zero_campaign(company=None, max_roots=15) -> dict:
	"""Dry-run the recommended smallest SAFE_GROUP (no writes)."""
	cls = classify_zero_clusters(company=company, max_cluster=max_roots)
	group = cls["clusters"].get("recommended_first_group")
	if not group or group.get("group_class") != SAFE_GROUP:
		return {"ok": False, "reason": "no SAFE_GROUP available", "clusters": cls["clusters"]}
	roots = group.get("roots") or []
	dry_rows = []
	blocked = []
	for r in roots:
		try:
			detail = _load_detail(r)
			classified = classify_zero_row(detail)
			merged = {**classified, **{k: v for k, v in r.items() if v not in (None, "")}}
			dry = repair_zero_rate_selected([merged], dry_run=True)
			dry_rows.append({"voucher": merged.get("voucher"), "item": merged.get("item"), "dry": dry, "proposed_rate": merged.get("proposed_rate")})
		except Exception as exc:
			blocked.append({"voucher": r.get("voucher"), "error": str(exc)})
	out = {
		"ok": not blocked,
		"dry_run": True,
		"group": {k: group.get(k) for k in group if k != "roots"},
		"n_roots": len(roots),
		"dry_rows": dry_rows,
		"blocked": blocked,
		"database_backup_required": True,
	}
	dump_artifact("zero_rate", "preview.json", out)
	return out


def run_zero_safe_cluster_campaign(company=None, max_roots=12, *, apply=False) -> dict:
	"""Lifecycle: classify → dry run → (optional apply) → verify residual clear → validate.

	apply=False by default for safety; set True only after preview OK and backup taken.
	"""
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock._validation.dashboard_consistency_v5214 import (
		collect_kpi_matrix,
	)

	company = resolve_company(company)
	t0 = perf_counter()
	before = run_full_integrity_scan(company=company, include_manufacture=False)
	cls = classify_zero_clusters(company=company, max_cluster=max_roots)
	group = cls["clusters"].get("recommended_first_group")
	if not group or group.get("group_class") != SAFE_GROUP:
		return {"ok": False, "aborted": True, "reason": "no SAFE_GROUP", "before": before.get("dashboard")}

	roots = (group.get("roots") or [])[:max_roots]
	# Re-confirm READY + EXACT and load details
	prepared = []
	for r in roots:
		try:
			detail = _load_detail(r)
			classified = classify_zero_row(detail)
			merged = {**classified, **{k: v for k, v in r.items() if v not in (None, "")}}
			if merged.get("confidence") != "EXACT" or not (
				merged.get("eligible") or str(merged.get("planner_status") or "") == "READY"
			):
				continue
			if abs(flt(merged.get("proposed_rate") or 0)) <= RATE_EPS:
				continue
			prepared.append(merged)
		except Exception:
			continue

	if not prepared:
		return {"ok": False, "aborted": True, "reason": "no prepared READY EXACT roots", "before": before.get("dashboard")}

	# Dry run all
	dry = repair_zero_rate_selected(prepared, dry_run=True)
	result = {
		"started_at": datetime.utcnow().isoformat() + "Z",
		"apply": apply,
		"group_class": SAFE_GROUP,
		"n_prepared": len(prepared),
		"vouchers": [p.get("voucher") for p in prepared],
		"dry_run": {"count": dry.get("count"), "aborted": dry.get("aborted"), "blocked": dry.get("blocked")},
		"applied": [],
		"failed": [],
		"verified": [],
	}
	if not apply:
		result["ok"] = not dry.get("aborted")
		result["promotion_status"] = "PREVIEW_ONLY"
		result["before"] = before.get("dashboard")
		result["elapsed_seconds"] = round(perf_counter() - t0, 2)
		dump_artifact("zero_rate", "campaign_preview_only.json", result)
		return result

	# Apply one root at a time with savepoint + residual verification
	frappe.flags["historical_repair"] = True
	sql_total = 0
	try:
		for row in prepared:
			sp = f"zr_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(sp)
			try:
				out = repair_zero_rate_selected([row], dry_run=False)
				if out.get("aborted"):
					frappe.db.rollback(save_point=sp)
					result["failed"].append({"voucher": row.get("voucher"), "reason": out.get("reason"), "blocked": out.get("blocked")})
					# stop group on first structural abort
					result["stop_reason"] = f"aborted at {row.get('voucher')}"
					break
				# Verify defect cleared: basic_rate written AND no longer zero-rate eligible
				detail2 = _load_detail(row)
				after = classify_zero_row(detail2)
				basic = flt(
					frappe.db.get_value(
						"Stock Entry Detail",
						row.get("voucher_detail") or detail2.get("name") or detail2.get("voucher_detail"),
						"basic_rate",
					)
					or 0
				)
				still_zero_defect = abs(basic) <= RATE_EPS
				still_eligible = bool(after.get("eligible"))
				cleared = (not still_zero_defect) and (not still_eligible)
				entry = {
					"voucher": row.get("voucher"),
					"item": row.get("item"),
					"warehouse": row.get("warehouse"),
					"proposed_rate": row.get("proposed_rate"),
					"basic_rate_after": basic,
					"cleared": cleared,
					"after_status": after.get("status") or after.get("planner_status"),
					"after_confidence": after.get("confidence"),
					"sql_updates": out.get("sql_updates_executed"),
					"repair_run_id": out.get("repair_run_id"),
				}
				if cleared:
					result["verified"].append(entry)
					result["applied"].append(entry)
					sql_total += int(out.get("sql_updates_executed") or 0)
					frappe.db.commit()
				else:
					frappe.db.rollback(save_point=sp)
					result["failed"].append({**entry, "reason": "residual_not_cleared"})
					result["stop_reason"] = f"false-success / residual at {row.get('voucher')}"
					break
			except Exception as exc:
				frappe.db.rollback(save_point=sp)
				result["failed"].append({"voucher": row.get("voucher"), "error": str(exc)})
				result["stop_reason"] = str(exc)
				break
	finally:
		frappe.flags["historical_repair"] = False

	after_scan = run_full_integrity_scan(company=company, include_manufacture=False)
	matrix = collect_kpi_matrix(company=company)
	result.update(
		{
			"ok": bool(result["verified"]) and not result.get("stop_reason"),
			"sql_updates_executed": sql_total,
			"elapsed_seconds": round(perf_counter() - t0, 2),
			"before": before.get("dashboard"),
			"after": after_scan.get("dashboard"),
			"kpi_delta": _delta(before.get("dashboard") or {}, after_scan.get("dashboard") or {}),
			"dashboard_validation": {
				"all_pass": matrix.get("all_pass"),
				"pass_count": matrix.get("pass_count"),
				"fail_count": matrix.get("fail_count"),
			},
			"promotion_status": (
				"PRODUCTION_PROVEN"
				if result["verified"]
				and not result["failed"]
				and matrix.get("all_pass")
				and len(result["verified"]) >= 5
				else "NOT_PROVEN"
			),
		}
	)
	dump_artifact("zero_rate", "campaign_result.json", result)
	return result


def _delta(before, after):
	keys = [
		"Zero Rate",
		"Patient Zero",
		"Zero Rate Patient Zero",
		"I4 Leftover",
		"Wrong Rate",
		"Broken Bin",
		"Broken GL",
		"Failed RIV",
		"Repairable",
		"Integrity Score",
	]
	out = {}
	for k in keys:
		b, a = before.get(k), after.get(k)
		if b is None and a is None:
			continue
		try:
			out[k] = {"before": b, "after": a, "delta": (a or 0) - (b or 0)}
		except Exception:
			out[k] = {"before": b, "after": a}
	return out
