# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 Master Repair Plan — per-class metrics from live scans (read-only)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from time import perf_counter

import frappe
from frappe.utils import cint, flt, nowdate

from erpnext_extensions.iran_accounting.historical_stock import (
	I4_LEFTOVER_REPAIR,
	RIV_SAFE_TO_RETRY,
)
from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

COMPANY_DEFAULT = "اسپاد فارمد دارو"
FROM_DATE = "2026-03-21"

# Class priority for operator roadmap (lower = earlier).
# Phase 2: Wrong Rate → Failed RIV → GL after Phase 1 exhausted.
CLASS_PRIORITY = {
	"WRONG_RATE": 1,
	"FAILED_RIV": 2,
	"GL": 3,
	"I4_LEFTOVER_REPAIR": 4,
	"ZERO_RATE": 5,
	"POSTING_ORDER": 6,
	"WAREHOUSE_WIDE": 7,
}


def build_master_repair_plan(company=None) -> dict:
	"""Full per-class roadmap with READY/WAITING/MANUAL/AMBIGUOUS and dependency stats."""
	company = company or COMPANY_DEFAULT
	t0 = perf_counter()
	classes = []
	classes.append(_class_posting(company))
	classes.append(_class_i4(company))
	classes.append(_class_zero(company))
	classes.append(_class_wrong(company))
	classes.append(_class_riv(company))
	classes.append(_class_gl(company))
	classes.append(_class_warehouse_placeholder(company))

	classes.sort(key=lambda c: c.get("priority") or 99)
	graph = _dependency_graph_summary(classes)
	return {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"version": "5.2.15",
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"classes": classes,
		"repair_order": [c["repair_class"] for c in classes],
		"dependency_graph": graph,
		"message": (
			"Prove each class with small SAFE clusters before bulk. "
			"Never Global Replay / Global RIV / Global GL."
		),
	}


def _status_bucket(row) -> str:
	ps = str(row.get("planner_status") or row.get("i4_status") or row.get("riv_status") or row.get("status") or "")
	conf = str(row.get("confidence") or "")
	if ps in READY_STATUSES or ps == "READY_I4" or (row.get("eligible") and "READY" in ps):
		return "READY"
	if "WAITING" in ps or "WAITING" in str(row.get("status") or ""):
		return "WAITING"
	if conf == "AMBIGUOUS" or "AMBIGUOUS" in ps:
		return "AMBIGUOUS"
	if "MANUAL" in ps or conf in ("LIKELY", "MANUAL"):
		return "MANUAL"
	if row.get("eligible"):
		return "READY"
	return "MANUAL"


def _agg(rows, repair_class, *, risk, expected_kpi, notes="") -> dict:
	buckets = defaultdict(int)
	depths = []
	pz = set()
	cross_wh = cross_batch = cross_wo = 0
	sql = replay = 0
	identities = set()
	for r in rows or []:
		b = _status_bucket(r)
		buckets[b] += 1
		depth = cint(r.get("dependency_depth") or r.get("depth") or 0)
		depths.append(depth)
		p = r.get("patient_zero")
		if isinstance(p, dict):
			if p.get("voucher_no"):
				pz.add(p["voucher_no"])
		elif p:
			pz.add(str(p))
		elif r.get("root_patient_zero"):
			pz.add(str(r["root_patient_zero"]))
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse") or r.get("s_warehouse") or r.get("t_warehouse")
		if item and wh:
			identities.add((item, wh))
		if r.get("cross_warehouse") or (r.get("s_warehouse") and r.get("t_warehouse") and r.get("s_warehouse") != r.get("t_warehouse")):
			cross_wh += 1
		if r.get("batch") or r.get("batch_no") or r.get("serial_and_batch_bundle"):
			cross_batch += 1
		if r.get("work_order"):
			cross_wo += 1
		sql += int(r.get("sql_updates") or r.get("sql_updates_estimate") or r.get("replay_count") or 1)
		replay += int(r.get("replay_count") or r.get("rows") or 0)
	ready = buckets["READY"]
	return {
		"repair_class": repair_class,
		"priority": CLASS_PRIORITY.get(repair_class, 99),
		"current_count": len(rows or []),
		"READY": ready,
		"WAITING": buckets["WAITING"],
		"MANUAL": buckets["MANUAL"],
		"AMBIGUOUS": buckets["AMBIGUOUS"],
		"patient_zero_count": len(pz),
		"identity_count": len(identities),
		"average_dependency_depth": round(sum(depths) / len(depths), 2) if depths else 0,
		"maximum_dependency_depth": max(depths) if depths else 0,
		"cross_warehouse": cross_wh,
		"cross_batch": cross_batch,
		"cross_work_order": cross_wo,
		"estimated_sql": sql,
		"estimated_replay": replay or sql,
		"estimated_runtime_seconds": round(max(1, ready) * 0.4 + (replay or sql) * 0.01, 1),
		"risk": risk,
		"expected_kpi_reduction": expected_kpi,
		"notes": notes,
		"can_bulk": False,
		"promotion_status": "NOT_PROVEN",
	}


def _class_posting(company):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	scan = run_full_history_scan(company=company)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"POSTING_ORDER",
		risk="MEDIUM",
		expected_kpi={"Posting Order": "READY batch-scoped only"},
		notes="Only READY_BATCH_SCOPED may auto-apply; others preview-only",
	)
	out["promotion_status"] = "LIMITED_PROVEN"  # 2 roots proven earlier
	return out


def _class_i4(company):
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	scan = scan_i4_leftover(company=company, from_date=FROM_DATE, to_date=nowdate(), limit=5000)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		I4_LEFTOVER_REPAIR,
		risk="LOW",
		expected_kpi={"I4 Leftover": f"-{sum(1 for r in rows if r.get('eligible'))}"},
		notes="Gated READY_I4 (healthy previous + sim clears). PRODUCTION_PROVEN at scale.",
	)
	out["by_status"] = scan.get("by_status")
	out["promotion_status"] = "PRODUCTION_PROVEN"
	return out


def _class_zero(company):
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	scan = scan_zero_rate_rows(company=company)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"ZERO_RATE",
		risk="MEDIUM",
		expected_kpi={"Zero Rate": "cluster campaigns only"},
		notes="Campaign 1: smallest SAFE_GROUP (9 roots) proven with residual verify + Validate Dashboard PASS",
	)
	out["by_confidence"] = scan.get("by_confidence")
	out["by_class"] = scan.get("by_class")
	out["promotion_status"] = "PRODUCTION_PROVEN_SMALL_CLUSTER"
	return out


def _class_wrong(company):
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=company, limit=2000)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"WRONG_RATE",
		risk="HIGH",
		expected_kpi={"Wrong Rate": "after zero/I4 roots"},
		notes="Own patient-zero graph; never reuse Zero Rate logic",
	)
	out["by_flag"] = scan.get("by_flag")
	return out


def _class_riv(company):
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv

	scan = scan_failed_riv(company=company, limit=2000)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"FAILED_RIV",
		risk="HIGH",
		expected_kpi={"Failed RIV": "SAFE_TO_RETRY only"},
		notes="Classify SAFE_TO_RETRY / WAITING_* / UNSAFE; never retry all",
	)
	out["by_status"] = scan.get("by_status")
	out["safe_to_retry"] = sum(1 for r in rows if r.get("riv_status") == RIV_SAFE_TO_RETRY or r.get("eligible"))
	return out


def _class_gl(company):
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity

	scan = scan_gl_integrity(company=company, limit=500)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"GL",
		risk="HIGH",
		expected_kpi={"Broken GL": "only after SLE healthy"},
		notes="G1–G4 only; SLE is truth; never rebuild from poisoned SLE",
	)
	out["by_class"] = scan.get("by_class")
	return out


def _class_warehouse_placeholder(company):
	return {
		"repair_class": "WAREHOUSE_WIDE",
		"priority": CLASS_PRIORITY["WAREHOUSE_WIDE"],
		"current_count": 0,
		"READY": 0,
		"WAITING": 0,
		"MANUAL": 0,
		"AMBIGUOUS": 0,
		"patient_zero_count": 0,
		"identity_count": 0,
		"average_dependency_depth": 0,
		"maximum_dependency_depth": 0,
		"cross_warehouse": 0,
		"cross_batch": 0,
		"cross_work_order": 0,
		"estimated_sql": 0,
		"estimated_replay": 0,
		"estimated_runtime_seconds": 0,
		"risk": "CRITICAL",
		"expected_kpi_reduction": {},
		"notes": "Requires Warehouse Dependency Engine before any apply",
		"can_bulk": False,
		"promotion_status": "ENGINE_REQUIRED",
	}


def _dependency_graph_summary(classes) -> dict:
	"""Lightweight visual graph for UI (nodes=classes, edges=recommended order)."""
	nodes = [
		{
			"id": c["repair_class"],
			"label": c["repair_class"],
			"ready": c.get("READY"),
			"risk": c.get("risk"),
			"promotion": c.get("promotion_status"),
		}
		for c in classes
	]
	order = [c["repair_class"] for c in classes]
	edges = [{"from": order[i], "to": order[i + 1], "kind": "recommended_after"} for i in range(len(order) - 1)]
	# Explicit dependency edges
	edges.extend(
		[
			{"from": "I4_LEFTOVER_REPAIR", "to": "ZERO_RATE", "kind": "soft"},
			{"from": "ZERO_RATE", "to": "WRONG_RATE", "kind": "soft"},
			{"from": "WRONG_RATE", "to": "GL", "kind": "hard_before"},
			{"from": "I4_LEFTOVER_REPAIR", "to": "FAILED_RIV", "kind": "hard_before"},
			{"from": "GL", "to": "FAILED_RIV", "kind": "hard_before"},
			{"from": "WAREHOUSE_WIDE", "to": "FAILED_RIV", "kind": "blocked_until_engine"},
		]
	)
	return {"nodes": nodes, "edges": edges}
