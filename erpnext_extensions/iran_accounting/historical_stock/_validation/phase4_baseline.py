# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 4 pre-baseline + Zero/Wrong root graph (read-only except FRESH snapshot write).

  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.phase4_baseline.run
"""

from __future__ import annotations

import json
from collections import Counter
from time import perf_counter

COMPANY = "اسپاد فارمد دارو"


def run(*, save_snapshot: int = 1, wrong_limit: int = 4000, zero_limit: int = 8000):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		save_metrics_snapshot,
		worker_queue_status,
	)
	from erpnext_extensions.iran_accounting.historical_stock.root_graph import (
		build_zero_wrong_root_graph,
	)
	from erpnext_extensions.iran_accounting.historical_stock.transaction_semantics import (
		PURPOSE_REGISTRY,
		purpose_semantics,
	)
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	out = {
		"phase": "PHASE_4_BASELINE",
		"company": COMPANY,
		"version": frappe.get_attr("erpnext_extensions.__version__"),
		"head_hint": "expect >= 9527831 purpose-first",
	}

	out["worker"] = worker_queue_status("long")
	out["active_riv"] = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabRepost Item Valuation`
		WHERE docstatus=1 AND status IN ('Queued','In Progress')
		"""
	)[0][0]

	out["neg_valuation"] = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
	)[0][0]
	out["neg_incoming"] = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND actual_qty > 0 AND incoming_rate < -0.0001
		"""
	)[0][0]
	out["neg_fg"] = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.is_cancelled=0 AND se.purpose='Manufacture'
		  AND sle.actual_qty > 0 AND (sle.valuation_rate < -0.0001 OR sle.incoming_rate < -0.0001)
		"""
	)[0][0]

	# Blockers
	if frappe.db.exists("DocType", "Historical Repair Blocker"):
		out["blockers"] = dict(
			frappe.db.sql(
				"""
				SELECT lane, COUNT(*) FROM `tabHistorical Repair Blocker`
				WHERE status != 'RESOLVED' GROUP BY lane
				"""
			)
		)
	else:
		out["blockers"] = {}

	# Semantics coverage
	out["registry_purposes"] = sorted(PURPOSE_REGISTRY.keys())

	t0 = perf_counter()
	zero = scan_zero_rate_rows(company=COMPANY, limit=zero_limit)
	out["zero_seconds"] = round(perf_counter() - t0, 3)
	zrows = [attach_plan(r) for r in (zero.get("rows") or [])]
	out["zero"] = {
		"raw": int(zero.get("raw_count") or len(zrows)),
		"actionable": int(zero.get("actionable_count") or 0),
		"reconstructable": int(zero.get("reconstructable_count") or 0),
		"waiting": int(zero.get("waiting_upstream_count") or 0),
		"mr_user_review": int(zero.get("material_receipt_user_review_count") or 0),
		"by_purpose": dict(zero.get("by_purpose") or Counter(r.get("purpose") for r in zrows)),
		"by_status": dict(zero.get("by_status") or Counter(r.get("status") for r in zrows)),
		"by_kpi_bucket": dict(zero.get("by_kpi_bucket") or {}),
		"purpose_policies": {
			p: purpose_semantics(p).get("policy")
			for p in sorted(set(r.get("purpose") for r in zrows if r.get("purpose")))
		},
		"unregistered_purposes": sorted(
			{
				r.get("purpose")
				for r in zrows
				if r.get("purpose") and r.get("purpose") not in PURPOSE_REGISTRY
			}
		),
	}

	t0 = perf_counter()
	wrong = scan_wrong_rates(company=COMPANY, limit=wrong_limit)
	out["wrong_seconds"] = round(perf_counter() - t0, 3)
	wrows = [attach_plan(r) for r in (wrong.get("rows") or [])]
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import count_wrong_rate_buckets

	wb = count_wrong_rate_buckets(wrows)
	out["wrong"] = {
		"raw": int(wrong.get("count") or len(wrows)),
		"by_flag": dict(wrong.get("by_flag") or {}),
		"buckets": wb,
		"matched_but_corrupt": sum(
			1
			for r in wrows
			if r.get("flag") == "MATCHED_BUT_CORRUPT"
			or "MATCHED_BUT_CORRUPT" in (r.get("flags") or [])
		),
		"exact": int(wrong.get("exact") or 0),
		"likely": int(wrong.get("likely") or 0),
		"ambiguous": int(wrong.get("ambiguous") or 0),
	}

	graph = build_zero_wrong_root_graph(zrows, wrows)
	# Drop heavy nested groups from printed report (keep samples).
	out["root_graph"] = {
		k: v
		for k, v in graph.items()
		if k
		not in (
			"zero_group",
			"wrong_group",
		)
	}

	t0 = perf_counter()
	i1 = scan_i1_negative_rate(company=COMPANY, limit=500)
	out["i1_seconds"] = round(perf_counter() - t0, 3)
	out["i1_count"] = i1.get("count")

	t0 = perf_counter()
	riv = scan_failed_riv(limit=2000)
	out["failed_riv_seconds"] = round(perf_counter() - t0, 3)
	out["failed_riv"] = {
		"raw": riv.get("raw_count") or riv.get("count"),
		"actionable": riv.get("actionable_count"),
		"by_reconcile": riv.get("by_reconcile"),
	}

	# Allow-zero Material Receipt context
	out["material_receipt_allow_zero"] = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name=sed.parent
		WHERE se.docstatus=1 AND se.purpose='Material Receipt' AND se.company=%s
		  AND ABS(sed.qty)>0.0001
		  AND ABS(IFNULL(sed.basic_rate,0))<0.0001
		  AND ABS(IFNULL(sed.valuation_rate,0))<0.0001
		  AND IFNULL(sed.allow_zero_valuation_rate,0)=1
		""",
		COMPANY,
	)[0][0]

	gate_ok = (
		out["active_riv"] == 0
		and out["neg_valuation"] == 0
		and out["neg_incoming"] == 0
		and out["neg_fg"] == 0
		and out["i1_count"] == 0
		and out["worker"].get("available")
		and not out["zero"]["unregistered_purposes"]
	)
	out["gate_ok"] = gate_ok
	out["verdict"] = (
		"PHASE_4_BASELINE_READY" if gate_ok else "PHASE_4_BASELINE_BLOCKED"
	)

	if int(save_snapshot) and gate_ok:
		dash = {
			"Integrity Score Version": "5.3.0",
			"Zero Rate": out["zero"]["actionable"],
			"Zero Rate Raw": out["zero"]["raw"],
			"Zero Rate Reconstructable": out["zero"]["reconstructable"],
			"Wrong Rate": wb.get("active") or out["wrong"]["raw"],
			"Matched But Corrupt": out["wrong"]["matched_but_corrupt"],
			"I1 Negative Rate": out["i1_count"],
			"Failed RIV": out["failed_riv"]["actionable"],
			"Failed RIV Raw": out["failed_riv"]["raw"],
			"Failed RIV Actionable": out["failed_riv"]["actionable"],
			"Patient Zero": graph.get("UNIFIED_CHAIN_COUNT"),
			"User Action Required": out["blockers"].get("USER_ACTION_REQUIRED", 0),
			"Tool Limit": out["blockers"].get("TOOL_LIMIT", 0),
			"Independent Repairable Roots": graph.get("INDEPENDENT_REPAIRABLE_ROOTS"),
			"Waiting Upstream Roots": graph.get("WAITING_UPSTREAM_ROOTS"),
		}
		snap = save_metrics_snapshot(
			company=COMPANY,
			dashboard=dash,
			timing={
				"zero": out["zero_seconds"],
				"wrong": out["wrong_seconds"],
				"i1": out["i1_seconds"],
				"failed_riv": out["failed_riv_seconds"],
			},
			source="phase4_baseline",
			extra={"root_graph_summary": {k: graph.get(k) for k in (
				"ZERO_RAW_FINDINGS",
				"WRONG_RAW_FINDINGS",
				"ZERO_ROOT_CHAINS",
				"WRONG_ROOT_CHAINS",
				"CROSS_KPI_ROOT_CHAINS",
				"INDEPENDENT_REPAIRABLE_ROOTS",
				"MATCHED_BUT_CORRUPT",
			)}},
		)
		frappe.db.commit()
		out["snapshot"] = {
			"freshness": snap.get("freshness"),
			"scanned_at": snap.get("scanned_at"),
			"source": snap.get("source"),
		}

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
