# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5 final metrics + blocker sync (no global repost)."""

from __future__ import annotations

import json
from collections import Counter
from time import perf_counter

COMPANY = "اسپاد فارمد دارو"


def run(*, sync_blockers: int = 1, save_snapshot: int = 1):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		save_metrics_snapshot,
		worker_queue_status,
	)
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import (
		count_wrong_rate_buckets,
		wrong_rate_bucket,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manual_reason import summarize_manual_groups
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.root_graph import build_zero_wrong_root_graph
	from erpnext_extensions.iran_accounting.historical_stock.blockers import scan_and_sync_blockers
	from erpnext_extensions.iran_accounting.historical_stock.negative_stock_report import (
		build_negative_stock_root_report,
	)

	out = {
		"phase": "PHASE_5_FINAL",
		"company": COMPANY,
		"worker": worker_queue_status("long"),
		"active_riv": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabRepost Item Valuation`
			WHERE docstatus=1 AND status IN ('Queued','In Progress')
			"""
		)[0][0],
		"neg": {
			"valuation": frappe.db.sql(
				"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
			)[0][0],
			"incoming": frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabStock Ledger Entry`
				WHERE is_cancelled=0 AND actual_qty>0 AND incoming_rate < -0.0001
				"""
			)[0][0],
			"fg": frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
				JOIN `tabStock Entry` se ON se.name=sle.voucher_no
				WHERE sle.is_cancelled=0 AND se.purpose='Manufacture' AND sle.actual_qty>0
				  AND (sle.valuation_rate < -0.0001 OR sle.incoming_rate < -0.0001)
				"""
			)[0][0],
		},
	}

	t0 = perf_counter()
	w = scan_wrong_rates(company=COMPANY, limit=6000)
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	wb = count_wrong_rate_buckets(wrows)
	manual = [r for r in wrows if wrong_rate_bucket(r) == "manual"]
	ms = summarize_manual_groups(manual)
	out["wrong_seconds"] = round(perf_counter() - t0, 3)
	out["wrong"] = {
		"RAW": len(wrows),
		"ACTIONABLE": wb.get("active"),
		"READY": wb.get("ready"),
		"WAITING": wb.get("waiting"),
		"MANUAL": wb.get("manual"),
		"COMPLETE": wb.get("complete"),
		"MATCHED_BUT_CORRUPT": sum(
			1
			for r in wrows
			if r.get("flag") == "MATCHED_BUT_CORRUPT"
			or "MATCHED_BUT_CORRUPT" in (r.get("flags") or [])
		),
		"manual_by_reason": ms.get("by_reason"),
		"manual_by_lane": ms.get("by_lane"),
		"WRONG_MANUAL_ROOT_CHAINS": ms.get("root_chains"),
		"WRONG_MANUAL_UNIQUE_ITEM_WAREHOUSE": ms.get("unique_item_warehouse"),
		"WRONG_MANUAL_UNIQUE_PATIENT_ZERO": ms.get("unique_patient_zero"),
	}

	z = scan_zero_rate_rows(company=COMPANY, limit=8000)
	out["zero"] = {
		"RAW": z.get("raw_count"),
		"ACTIONABLE": z.get("actionable_count"),
		"RECONSTRUCTABLE": z.get("reconstructable_count"),
		"WAITING": z.get("waiting_upstream_count"),
		"by_status": z.get("by_status"),
		"by_purpose": z.get("by_purpose"),
		"by_kpi_bucket": z.get("by_kpi_bucket"),
	}

	i4 = scan_i4_leftover(company=COMPANY, limit=5000)
	out["i4"] = {
		"RAW": i4.get("raw_count") or i4.get("count"),
		"ACTIONABLE": i4.get("actionable_count") or i4.get("count"),
		"ROOT_IDENTITIES": i4.get("root_identity_count"),
		"READY": i4.get("ready_count"),
		"by_status": i4.get("by_status"),
	}

	po = run_full_history_scan(company=COMPANY)
	porows = [attach_plan(dict(r)) for r in (po.get("rows") or [])]
	po_act = [
		r for r in porows if (r.get("optimizer_status") or r.get("status")) not in ("NO_REPAIR_NEEDED",)
	]
	out["posting_order"] = {
		"RAW": len(porows),
		"ACTIONABLE": len(po_act),
		"by_optimizer": dict(Counter(str(r.get("optimizer_status") or r.get("status")) for r in porows)),
		"by_planner": dict(Counter(str(r.get("planner_status") or "") for r in po_act)),
		"by_confidence": dict(Counter(str(r.get("confidence") or "") for r in po_act)),
		"REAL_STOCK_SHORTAGE": sum(
			1
			for r in po_act
			if (r.get("optimizer_status") or r.get("status")) == "REAL_STOCK_SHORTAGE"
		),
		"ready": sum(
			1
			for r in po_act
			if str(r.get("planner_status") or "").startswith("READY")
			and int(r.get("sql_updates") or 0) > 0
		),
	}

	riv = scan_failed_riv(limit=2000)
	i1 = scan_i1_negative_rate(company=COMPANY, limit=100)
	out["failed_riv"] = {
		"RAW": riv.get("raw_count") or riv.get("count"),
		"ACTIONABLE": riv.get("actionable_count"),
		"by_reconcile": riv.get("by_reconcile"),
	}
	out["i1"] = i1.get("count")

	neg_rep = build_negative_stock_root_report(company=COMPANY, limit_chains=300)
	out["negative_stock"] = {
		"chains": neg_rep.get("count") or len(neg_rep.get("chains") or neg_rep.get("rows") or []),
		"by_class": neg_rep.get("by_class") or neg_rep.get("by_status"),
	}

	g = build_zero_wrong_root_graph(z.get("rows") or [], wrows)
	out["graph"] = {k: g[k] for k in g if not isinstance(g[k], (list, dict))}

	if int(sync_blockers):
		blk = scan_and_sync_blockers(company=COMPANY, include_tool_limits=True, limit=400)
		out["blockers_sync"] = {
			"created": blk.get("created"),
			"updated": blk.get("updated"),
			"user": blk.get("user_action_required"),
			"tool": blk.get("tool_limit"),
		}
	out["blockers"] = dict(
		frappe.db.sql(
			"""
			SELECT lane, COUNT(*) FROM `tabHistorical Repair Blocker`
			WHERE status!='RESOLVED' GROUP BY lane
			"""
		)
	) if frappe.db.exists("DocType", "Historical Repair Blocker") else {}

	# Patient zero breakdown
	pz_by = Counter()
	for r in wrows:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v:
			pz_by["Wrong Rate"] += 1
	for r in z.get("rows") or []:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v:
			pz_by["Zero Rate"] += 1
	for r in i4.get("rows") or []:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v:
			pz_by["I4"] += 1
	out["patient_zero_by_topic_refs"] = dict(pz_by)
	out["patient_zero_unique"] = g.get("UNIFIED_CHAIN_COUNT")

	gate = (
		out["active_riv"] == 0
		and out["neg"]["valuation"] == 0
		and out["neg"]["incoming"] == 0
		and out["neg"]["fg"] == 0
		and out["i1"] == 0
		and out["wrong"]["READY"] == 0
	)
	# Remaining work characterization
	remaining_tool = int((ms.get("by_lane") or {}).get("TOOL_LIMIT") or 0)
	remaining_user_po = int(out["posting_order"]["REAL_STOCK_SHORTAGE"] or 0)
	if not gate:
		out["verdict"] = "RIV_INCIDENT_STOPPED_CAMPAIGN" if out["active_riv"] else "PHASE_5_REGRESSION"
	elif remaining_tool > 500 and remaining_user_po > 50:
		out["verdict"] = "PHASE_5_NEEDS_FURTHER_TOOL_DEVELOPMENT"
	elif remaining_user_po > 50 and remaining_tool < 100:
		out["verdict"] = "PHASE_5_BLOCKED_BY_USER_ACTION"
	else:
		# Large manufacture/transfer ambiguous remains → tool development
		out["verdict"] = "PHASE_5_NEEDS_FURTHER_TOOL_DEVELOPMENT"

	out["verdict_notes"] = {
		"wrong_ready_drained": out["wrong"]["READY"] == 0,
		"zero_healed_vs_phase4_end": f"phase4_end_raw=303 → now {out['zero']['RAW']}",
		"i4_63_vs_65": "65=all voucher types company; 63=Stock Entry only; identities SE=17 / all=19",
		"manual_still_large": out["wrong"]["MANUAL"],
		"manual_root_chains": out["wrong"]["WRONG_MANUAL_ROOT_CHAINS"],
		"po_shortage_user": remaining_user_po,
		"tool_limit_lane_rows": remaining_tool,
	}

	if int(save_snapshot):
		dash = {
			"Integrity Score Version": "5.3.0",
			"Wrong Rate": out["wrong"]["ACTIONABLE"],
			"Wrong Rate Raw": out["wrong"]["RAW"],
			"Wrong Rate READY": out["wrong"]["READY"],
			"Wrong Rate WAITING": out["wrong"]["WAITING"],
			"Wrong Rate MANUAL": out["wrong"]["MANUAL"],
			"Zero Rate": out["zero"]["ACTIONABLE"],
			"Zero Rate Raw": out["zero"]["RAW"],
			"I4 Leftover": out["i4"]["ACTIONABLE"],
			"I4 Raw": out["i4"]["RAW"],
			"I4 Root Identities": out["i4"]["ROOT_IDENTITIES"],
			"Posting Order": out["posting_order"]["ACTIONABLE"],
			"Failed RIV": out["failed_riv"]["ACTIONABLE"],
			"Failed RIV Raw": out["failed_riv"]["RAW"],
			"I1 Negative Rate": out["i1"],
			"Patient Zero": out["patient_zero_unique"],
			"User Action Required": out["blockers"].get("USER_ACTION_REQUIRED", 0),
			"Tool Limit": out["blockers"].get("TOOL_LIMIT", 0),
			"Matched But Corrupt": out["wrong"]["MATCHED_BUT_CORRUPT"],
		}
		snap = save_metrics_snapshot(
			company=COMPANY, dashboard=dash, source="phase5_final", timing={"wrong": out["wrong_seconds"]}
		)
		frappe.db.commit()
		out["snapshot"] = {"freshness": snap.get("freshness"), "scanned_at": snap.get("scanned_at")}

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
