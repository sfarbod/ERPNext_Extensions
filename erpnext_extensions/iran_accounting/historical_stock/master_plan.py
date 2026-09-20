# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.18 Master Repair Plan — per-class metrics from live scans (read-only)."""

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
from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company

# Class priority for operator roadmap (lower = earlier).
# Recovery loop: Warehouse → Posting Order → Zero → Wrong → I4 → GL → SLE_GL_DRIFT → Failed RIV.
CLASS_PRIORITY = {
	"WAREHOUSE_WIDE": 1,
	"I1_NEGATIVE_RATE_REPAIR": 2,
	"POSTING_ORDER": 3,
	"ZERO_RATE": 4,
	"WRONG_RATE": 5,
	"I4_LEFTOVER_REPAIR": 6,
	"GL": 7,
	"SLE_GL_DRIFT": 8,
	"FAILED_RIV": 9,
}


def build_master_repair_plan(company=None) -> dict:
	"""Full per-class roadmap with READY/WAITING/MANUAL/AMBIGUOUS and dependency stats.

	v5.3.0 Master Plan V2 adds root-cause graph, root-vs-downstream grouping,
	RIV preflight summary hooks, and false-complete awareness — without removing
	v5.2.x class metrics or recommended order.
	"""
	company = resolve_company(company)
	t0 = perf_counter()
	classes = []
	classes.append(_class_posting(company))
	classes.append(_class_i1(company))
	classes.append(_class_i4(company))
	classes.append(_class_zero(company))
	classes.append(_class_wrong(company))
	classes.append(_class_riv(company))
	classes.append(_class_gl(company))
	classes.append(_class_sle_gl_drift(company))
	classes.append(_class_warehouse_placeholder(company))

	classes.sort(key=lambda c: c.get("priority") or 99)
	graph = _dependency_graph_summary(classes)

	# V2 root-cause graph from live class rows (best-effort; never fails the plan).
	root_cause = {"classes": {}, "cycles": [], "edge_count": 0, "cycle_count": 0}
	try:
		from erpnext_extensions.iran_accounting.historical_stock.root_graph import (
			build_root_cause_graph,
		)

		class_rows = {}
		for c in classes:
			# Prefer retaining sample rows if scanners attached them; else empty.
			class_rows[c["repair_class"]] = c.get("sample_rows") or []
		root_cause = build_root_cause_graph(class_rows)
	except Exception as exc:
		root_cause = {"error": str(exc), "classes": {}, "cycles": [], "edge_count": 0, "cycle_count": 0}

	return {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"version": "5.3.0",
		"master_plan": "V2",
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"classes": classes,
		"repair_order": [c["repair_class"] for c in classes],
		"dependency_graph": graph,
		"root_cause_graph": root_cause,
		"phases": _build_v2_phases(classes, root_cause),
		"safety": {
			"no_global_riv": True,
			"no_weaken_integrity_guards": True,
			"riv_preflight_required": True,
			"false_rate_rebuild_complete_refused": True,
			"manufacture_exact_on_healthy_after": True,
			"irr_align_before_expected_gl_gate": True,
			# Deprecated: scrap WH alone no longer grants NO_ACTION.
			"legitimate_scrap_zero_no_action": False,
			"zero_rate_purpose_first": True,
			"material_receipt_no_invent_rate": True,
			"matched_but_corrupt_detected": True,
		},
		"message": (
			"Master Plan V2 — prove each class with small SAFE clusters before bulk. "
			"Repair roots before downstream. Never Global Replay / Global RIV / Global GL. "
			"Run RIV preflight before any controlled repost. "
			"Zero Rate is purpose-first: Material Receipt without authoritative source "
			"is USER_ACTION_REQUIRED (never invent a rate); Transfer/Manufacture "
			"reconstruct from source. Scrap/Reject warehouse is contextual only."
		),
	}


def _build_v2_phases(classes: list, root_cause: dict) -> list:
	"""Dependency-ordered campaign phases (read-only roadmap)."""
	by = {c["repair_class"]: c for c in classes or []}

	def _n(key, field="ready"):
		c = by.get(key) or {}
		if field == "ready":
			return int((c.get("ready") if c.get("ready") is not None else c.get("READY")) or 0)
		if field == "total":
			return int(c.get("total") or c.get("count") or 0)
		return int(c.get(field) or 0)

	i1_total = _n("I1_NEGATIVE_RATE_REPAIR", "total")
	po_total = _n("POSTING_ORDER", "total")
	zero_total = _n("ZERO_RATE", "total")
	wrong_total = _n("WRONG_RATE", "total")
	i4_total = _n("I4_LEFTOVER_REPAIR", "total")
	gl_total = _n("GL", "total")
	drift_total = _n("SLE_GL_DRIFT", "total")
	riv_total = _n("FAILED_RIV", "total")
	cycles = int((root_cause or {}).get("cycle_count") or 0)

	return [
		{
			"phase": 1,
			"name": "Negative-rate patient-zero roots",
			"repair_classes": ["I1_NEGATIVE_RATE_REPAIR"],
			"candidate_count": i1_total,
			"root_chain_count": i1_total,
			"expected_downstream_healed": "manufacture + dependent Wrong/Zero",
			"repost_requirement": "min-scope after root cleared",
			"risk": "HIGH",
			"blocking_dependencies": [],
		},
		{
			"phase": 2,
			"name": "Posting-order / chronology roots",
			"repair_classes": ["POSTING_ORDER", "WAREHOUSE_WIDE"],
			"candidate_count": po_total,
			"root_chain_count": po_total,
			"expected_downstream_healed": "negative stock / zero-rate chronology",
			"repost_requirement": "often none if pure timestamp shift",
			"risk": "MEDIUM",
			"blocking_dependencies": ["I1_NEGATIVE_RATE_REPAIR"] if i1_total else [],
		},
		{
			"phase": 3,
			"name": "I4 / zero-qty-nonzero-value roots",
			"repair_classes": ["I4_LEFTOVER_REPAIR"],
			"candidate_count": i4_total,
			"root_chain_count": i4_total,
			"expected_downstream_healed": "Bin leftover + downstream SVD",
			"repost_requirement": "selective after patient-zero",
			"risk": "MEDIUM",
			"blocking_dependencies": ["POSTING_ORDER"],
		},
		{
			"phase": 4,
			"name": "Wrong/Zero authoritative-rate reconstruction",
			"repair_classes": ["ZERO_RATE", "WRONG_RATE"],
			"candidate_count": zero_total + wrong_total,
			"root_chain_count": zero_total + wrong_total,
			"expected_downstream_healed": "SLE rates + manufacture inputs",
			"repost_requirement": "controlled min-scope RIV after preflight",
			"risk": "HIGH",
			"blocking_dependencies": ["I1_NEGATIVE_RATE_REPAIR", "POSTING_ORDER"],
			"notes": "LEGITIMATE_SCRAP_ZERO_RATE excluded from actionable Zero KPI",
		},
		{
			"phase": 5,
			"name": "Transfer propagation roots then Manufacture dependency",
			"repair_classes": ["WRONG_RATE", "ZERO_RATE"],
			"candidate_count": wrong_total + zero_total,
			"root_chain_count": wrong_total + zero_total,
			"expected_downstream_healed": "multi-hop transfers + FG + unlocked Zero/I4",
			"repost_requirement": "controlled identity replay after EXACT reconstruction; no global RIV",
			"risk": "CRITICAL",
			"blocking_dependencies": ["POSTING_ORDER", "I1_NEGATIVE_RATE_REPAIR"],
			"notes": (
				"reconstruct_transfer_valuation + manufacture input_health; "
				"HEALED_BY_UPSTREAM_TRANSFER / HEALED_BY_MANUFACTURE_REBUILD; "
				f"dependency_cycles_detected={cycles}"
			),
		},
		{
			"phase": 6,
			"name": "Manufacture deterministic reconstruction (I1-linked)",
			"repair_classes": ["I1_NEGATIVE_RATE_REPAIR", "WRONG_RATE"],
			"candidate_count": i1_total,
			"root_chain_count": i1_total,
			"expected_downstream_healed": "FG + scrap/by-product + pool balance",
			"repost_requirement": "yes after EXACT/RECONSTRUCTABLE preview",
			"risk": "CRITICAL",
			"blocking_dependencies": ["ZERO_RATE", "WRONG_RATE"],
			"notes": "post-replay neg valuation gate required",
		},
		{
			"phase": 7,
			"name": "Controlled repost waves",
			"repair_classes": ["FAILED_RIV"],
			"candidate_count": riv_total,
			"root_chain_count": int((by.get("FAILED_RIV") or {}).get("safe_to_retry") or 0),
			"expected_downstream_healed": "moving-average + dependent Manufacture",
			"repost_requirement": "RIV preflight REQUIRED; refuse poison closure",
			"risk": "CRITICAL",
			"blocking_dependencies": ["I1_NEGATIVE_RATE_REPAIR", "WRONG_RATE"],
		},
		{
			"phase": 8,
			"name": "SLE_GL_DRIFT / GL reconciliation",
			"repair_classes": ["SLE_GL_DRIFT", "GL"],
			"candidate_count": drift_total + gl_total,
			"root_chain_count": drift_total + gl_total,
			"expected_downstream_healed": "GL balance after SLE healthy",
			"repost_requirement": "no — selective GL only",
			"risk": "HIGH",
			"blocking_dependencies": ["FAILED_RIV"],
		},
		{
			"phase": 9,
			"name": "Residual manual negative-stock cases",
			"repair_classes": [],
			"candidate_count": None,
			"root_chain_count": None,
			"expected_downstream_healed": "operator-driven chronology / inbound",
			"repost_requirement": "only after user confirms operational cause",
			"risk": "HIGH",
			"blocking_dependencies": ["all prior phases"],
			"notes": "See negative_stock_report.build_negative_stock_root_report",
		},
	]


def _status_bucket(row) -> str:
	ps = str(
		row.get("planner_status")
		or row.get("drift_status")
		or row.get("i4_status")
		or row.get("riv_status")
		or row.get("status")
		or ""
	)
	conf = str(row.get("confidence") or "")
	if ps in READY_STATUSES or ps == "READY_I4" or ps == "READY_GL_ONLY" or (row.get("eligible") and "READY" in ps):
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
	# Compact sample for Master Plan V2 root-cause graph (no full row dump).
	sample_rows = []
	for r in (rows or [])[:80]:
		sample_rows.append(
			{
				"voucher": r.get("voucher") or r.get("voucher_no") or r.get("outbound_document"),
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse") or r.get("s_warehouse") or r.get("t_warehouse"),
				"status": r.get("planner_status") or r.get("status") or r.get("drift_status") or r.get("i1_status") or r.get("i4_status"),
				"confidence": r.get("confidence"),
				"patient_zero": r.get("patient_zero") or r.get("root_patient_zero"),
				"topic": r.get("topic"),
				"required_prerequisite": r.get("required_prerequisite"),
			}
		)
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
		"sample_rows": sample_rows,
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


def _class_i1(company):
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate

	scan = scan_i1_negative_rate(company=company, limit=2000)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"I1_NEGATIVE_RATE_REPAIR",
		risk="LOW",
		expected_kpi={"I1 Negative Rate": f"-{sum(1 for r in rows if r.get('eligible'))}"},
		notes=(
			"EXACT in-document repair: secondary inbound repriced from this document's issue rate. "
			"WAITING rows need Zero/Wrong Rate upstream first. Blocks Failed RIV VALUATION_INTEGRITY."
		),
	)
	out["by_status"] = scan.get("by_status")
	out["promotion_status"] = "NOT_PROVEN"
	return out


def _class_i4(company):
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	scan = scan_i4_leftover(company=company, from_date=None, to_date=nowdate(), limit=5000)
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
	# Actionable only — legitimate scrap zeros are NO_ACTION_REQUIRED.
	actionable_rows = [
		r
		for r in rows
		if not r.get("no_action_required")
		and r.get("status") not in ("NO_ACTION_REQUIRED", "Z0_LEGITIMATE_ZERO", "LEGITIMATE_SCRAP_ZERO_RATE")
		and r.get("zero_class") not in ("LEGITIMATE_SCRAP_ZERO_RATE", "Z0_LEGITIMATE_ZERO")
	]
	out = _agg(
		actionable_rows,
		"ZERO_RATE",
		risk="MEDIUM",
		expected_kpi={"Zero Rate": "cluster campaigns only"},
		notes="Campaign 1: smallest SAFE_GROUP (9 roots) proven with residual verify + Validate Dashboard PASS",
	)
	out["by_confidence"] = scan.get("by_confidence")
	out["by_class"] = scan.get("by_class")
	out["by_zero_reason"] = scan.get("by_zero_reason")
	out["raw_count"] = scan.get("raw_count") or len(rows)
	out["no_action_required_count"] = scan.get("no_action_required_count") or 0
	out["legitimate_scrap_zero_count"] = scan.get("legitimate_scrap_zero_count") or 0
	out["actionable_count"] = scan.get("actionable_count") or len(actionable_rows)
	out["sample_rows"] = actionable_rows[:50]
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
	out["promotion_status"] = "PRODUCTION_PROVEN_SMALL_CLUSTER"
	out["notes"] = (
		"EXACT/SAFE_GROUP proven on lab; residual MANUAL/AMBIGUOUS/WAITING require roots. "
		"Own patient-zero graph; never reuse Zero Rate logic; never Bin as truth."
	)
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
	out["promotion_status"] = "LIMITED_PROVEN"
	return out


def _class_sle_gl_drift(company):
	"""GL-only drift: healthy SLE, stale posted GL (covers false G0 transfers)."""
	from erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift import scan_sle_gl_drift

	scan = scan_sle_gl_drift(company=company, limit=200)
	rows = scan.get("rows") or []
	out = _agg(
		rows,
		"SLE_GL_DRIFT",
		risk="MEDIUM",
		expected_kpi={"SLE↔GL drift": "GL-only; SLE fingerprint frozen"},
		notes=(
			"Rebuild GL from current healthy SLE only. Refuses when SLE integrity is unhealthy. "
			"False G0 transfers are discoverable. Never full RIV / SLE mutation."
		),
	)
	out["by_status"] = scan.get("by_status")
	out["ready_gl_only"] = scan.get("ready_gl_only")
	out["waiting_sle_repair"] = scan.get("waiting_sle_repair")
	out["promotion_status"] = "NEW"
	return out


def _class_warehouse_placeholder(company):
	"""Live warehouse campaign discovery via Warehouse Engine optimizer."""
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)

	try:
		disc = discover_warehouse_campaigns(company=company)
	except Exception as exc:
		return {
			"repair_class": "WAREHOUSE_WIDE",
			"priority": CLASS_PRIORITY["WAREHOUSE_WIDE"],
			"current_count": 0,
			"READY": 0,
			"WAITING": 0,
			"MANUAL": 0,
			"AMBIGUOUS": 0,
			"risk": "CRITICAL",
			"notes": f"Warehouse engine discover failed: {exc}",
			"can_bulk": False,
			"promotion_status": "ENGINE_ERROR",
		}

	campaigns = disc.get("campaigns") or []
	ready = disc.get("n_ready_campaigns") or 0
	depths = [int(c.get("dependency_depth") or 0) for c in campaigns]
	sql = sum(int(c.get("expected_sql") or 0) for c in (disc.get("ready_campaigns") or []))
	replay = sum(int(c.get("expected_replay") or 0) for c in (disc.get("ready_campaigns") or []))
	runtime = sum(float(c.get("expected_runtime_seconds") or 0) for c in (disc.get("ready_campaigns") or []))
	return {
		"repair_class": "WAREHOUSE_WIDE",
		"priority": CLASS_PRIORITY["WAREHOUSE_WIDE"],
		"current_count": len(campaigns),
		"READY": ready,
		"WAITING": disc.get("n_campaign_required") or 0,
		"MANUAL": disc.get("n_shortage") or 0,
		"AMBIGUOUS": disc.get("n_ambiguous") or 0,
		"patient_zero_count": len(disc.get("ready_campaigns") or []),
		"identity_count": disc.get("n_identities") or 0,
		"average_dependency_depth": round(sum(depths) / len(depths), 2) if depths else 0,
		"maximum_dependency_depth": max(depths) if depths else 0,
		"cross_warehouse": (disc.get("graph_summary") or {}).get("n_cross_wh_edges") or 0,
		"cross_batch": (disc.get("graph_summary") or {}).get("n_bridge_nodes") or 0,
		"cross_work_order": 0,
		"estimated_sql": sql,
		"estimated_replay": replay,
		"estimated_runtime_seconds": round(runtime, 2),
		"risk": "LOW" if ready else "HIGH",
		"expected_kpi_reduction": {"Posting Order BLOCKED/WAREHOUSE": ready},
		"notes": disc.get("message")
		or "Warehouse Engine campaigns — joint multi-pair + cross-identity proof required",
		"can_bulk": False,
		"promotion_status": "READY_CAMPAIGNS" if ready else "NO_READY_CAMPAIGN",
		"ready_campaign_ids": [c.get("campaign_id") for c in (disc.get("ready_campaigns") or [])],
		"n_eligible_pairs": disc.get("n_eligible_pairs"),
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
			{"from": "GL", "to": "SLE_GL_DRIFT", "kind": "soft"},
			{"from": "I1_NEGATIVE_RATE_REPAIR", "to": "SLE_GL_DRIFT", "kind": "hard_before"},
			{"from": "I4_LEFTOVER_REPAIR", "to": "SLE_GL_DRIFT", "kind": "hard_before"},
			{"from": "I4_LEFTOVER_REPAIR", "to": "FAILED_RIV", "kind": "hard_before"},
			{"from": "SLE_GL_DRIFT", "to": "FAILED_RIV", "kind": "soft"},
			{"from": "GL", "to": "FAILED_RIV", "kind": "hard_before"},
			{"from": "WAREHOUSE_WIDE", "to": "FAILED_RIV", "kind": "after_warehouse_campaigns"},
			{"from": "POSTING_ORDER", "to": "WAREHOUSE_WIDE", "kind": "escalate_multi_pair"},
		]
	)
	return {"nodes": nodes, "edges": edges}
