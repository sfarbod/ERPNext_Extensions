# Copyright (c) 2026 — session-2 finalize (dev only).
"""Collect 26138 status, first-5 READY roots, metrics, grouping preview."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime

import frappe
from frappe.utils import flt

BASE = "/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/i4_v5213_session2"
COMPANY = "اسپاد فارمد دارو"
V1 = "MAT-STE-2026-25791"
V2 = "MAT-STE-2026-26138-1"


def _dump(name, data):
	os.makedirs(BASE, exist_ok=True)
	path = os.path.join(BASE, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def run():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		dry_run_i4_repair,
		preview_i4_replay,
		scan_i4_leftover,
	)
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row, READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_bin_mismatches
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan

	# --- 26138 current state ---
	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, posting_datetime, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference
		FROM `tabStock Ledger Entry` WHERE voucher_no=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		V2,
		as_dict=True,
	)
	v2_rows = []
	for s in sles:
		cl = classify_i4_row(s.item_code, s.warehouse, voucher=V2, sle_name=s.name)
		dec = evaluate_row({**cl, "topic": "I4", "repair_class": "I4_LEFTOVER_REPAIR"})
		v2_rows.append({"sle": s, "classify": cl, "decision": dec})

	# Apply attempt artifact if present
	apply_path = os.path.join(BASE, "apply_26138", "result.json")
	apply_art = json.load(open(apply_path)) if os.path.exists(apply_path) else None

	# --- Broken bin for 25791 identity ---
	item_q = "30300014"
	wh_q = "انبار Quarantine محصول نیمه ساخته اسپاد"
	bin_scan = scan_bin_mismatches(company=COMPANY, limit=500)
	bin_rows = bin_scan if isinstance(bin_scan, list) else (bin_scan.get("rows") or [])
	bin_hits = [
		r
		for r in bin_rows
		if (r.get("item") or r.get("item_code")) == item_q and (r.get("warehouse") == wh_q)
	]
	# last SLE + first skipped poison
	chain = frappe.db.sql(
		"""
		SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction, stock_value,
		       valuation_rate, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 5
		""",
		(item_q, wh_q),
		as_dict=True,
	)
	binrow = frappe.db.get_value(
		"Bin",
		{"item_code": item_q, "warehouse": wh_q},
		["name", "actual_qty", "stock_value", "valuation_rate"],
		as_dict=True,
	)

	# --- Scan I4 + first 5 READY ---
	i4 = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date=str(datetime.utcnow().date()), limit=3000)
	ready = []
	manual = []
	waiting = []
	for r in i4.get("rows") or []:
		dec = r.get("planner") or evaluate_row(r)
		ps = (dec.get("planner_status") if isinstance(dec, dict) else None) or r.get("planner_status") or r.get("i4_status")
		entry = {
			"voucher": r.get("voucher"),
			"item": r.get("item"),
			"warehouse": r.get("warehouse"),
			"posting_datetime": r.get("posting_datetime"),
			"i4_status": r.get("i4_status") or r.get("status"),
			"planner_status": ps,
			"residual_value": r.get("residual_value"),
			"previous_healthy": r.get("previous_healthy"),
			"previous_blocker": r.get("previous_blocker"),
			"pz_residual_clears_in_sim": r.get("pz_residual_clears_in_sim"),
			"replay_count": r.get("replay_count"),
			"sql_updates_estimate": r.get("sql_updates_estimate"),
			"stop_before_voucher": r.get("stop_before_voucher"),
			"stop_reason": r.get("stop_reason"),
			"eligible": r.get("eligible"),
			"message": r.get("message"),
		}
		if ps == "READY_I4" or r.get("i4_status") == "READY_I4" or r.get("eligible"):
			# Double-check with evaluate
			if (isinstance(dec, dict) and dec.get("eligible")) or r.get("eligible"):
				ready.append(entry)
		elif (r.get("i4_status") or "") == "MANUAL" or ps == "MANUAL":
			manual.append(entry)
		elif "WAITING" in str(ps or "") or "WAITING" in str(r.get("i4_status") or ""):
			waiting.append(entry)

	# Prefer chronological READY roots (oldest first)
	ready_sorted = sorted(ready, key=lambda x: str(x.get("posting_datetime") or ""))
	first5 = []
	for e in ready_sorted[:5]:
		sim = preview_i4_replay(e["item"], e["warehouse"], from_dt=e["posting_datetime"])
		dry = dry_run_i4_repair([e])
		drow = (dry.get("rows") or [{}])[0]
		first5.append(
			{
				**e,
				"priority": len(first5) + 1,
				"repair_class": "I4_LEFTOVER_REPAIR",
				"patient_zero_reason": "qty_after_zero_nonzero_value",
				"affected_sle_count": sim.get("rows"),
				"estimated_sql_updates": sim.get("sql_updates"),
				"estimated_runtime_seconds": max(0.05, (sim.get("rows") or 0) * 0.05),
				"expected_issues_removed": 1,
				"required_scope": "identity",
				"dependencies": [],
				"can_be_grouped": True,
				"simulation": sim,
				"dry_run_summary": {
					"residual_value": drow.get("residual_value"),
					"previous_qty": drow.get("previous_qty"),
					"previous_value": drow.get("previous_value"),
					"next_sle": drow.get("next_sle"),
				},
			}
		)

	# Grouping feasibility among first READY set
	identities = defaultdict(list)
	for e in ready_sorted:
		identities[(e["item"], e["warehouse"])].append(e)
	independent = [v[0] for v in identities.values() if len(v) == 1]
	same_identity_multi = {k: v for k, v in identities.items() if len(v) > 1}
	group_preview = {
		"SAFE_GROUP_candidates": [
			{
				"group_id": "GROUP-01",
				"class": "SAFE_GROUP",
				"roots": [
					{
						"voucher": x["voucher"],
						"item": x["item"],
						"warehouse": x["warehouse"],
					}
					for x in independent[:5]
				],
				"n_roots": min(5, len(independent)),
				"estimated_sle_replay": sum(int(x.get("replay_count") or 0) for x in independent[:5]),
				"estimated_sql_updates": sum(int(x.get("sql_updates_estimate") or 0) for x in independent[:5]),
				"estimated_runtime_seconds": round(sum(max(0.05, (x.get("replay_count") or 0) * 0.05) for x in independent[:5]), 2),
				"risk": "LOW",
				"note": "Independent item+warehouse identities, same repair class READY_I4",
			}
		]
		if independent
		else [],
		"SAFE_SEQUENTIAL_GROUP_candidates": [
			{
				"group_id": f"SEQ-{i+1}",
				"class": "SAFE_SEQUENTIAL_GROUP",
				"identity": {"item": k[0], "warehouse": k[1]},
				"ordered_roots": [x["voucher"] for x in sorted(v, key=lambda z: str(z.get("posting_datetime") or ""))],
				"n_roots": len(v),
				"risk": "MEDIUM",
				"note": "Same identity — must repair oldest patient zero first, sequentially",
			}
			for i, (k, v) in enumerate(list(same_identity_multi.items())[:5])
		],
		"independent_ready_count": len(independent),
		"multi_root_identities": len(same_identity_multi),
		"total_ready": len(ready_sorted),
		"total_manual_i4": len(manual),
		"total_waiting_i4": len(waiting),
	}

	# Metrics
	dash = run_full_integrity_scan(company=COMPANY)
	kpis = dash.get("dashboard") or dash.get("kpis") or dash

	# Compare repair artifacts
	r1_path = "/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/i4_v5213_25791"
	compare = {
		"repair_1_25791": {
			"voucher": V1,
			"result": "SUCCESS — residual cleared, I4_REPAIRED, idempotent",
			"residual_before": None,
			"residual_after": 0.0,
			"sql_updates": 120,
			"notes": "Healthy previous SLE; full consume to zero cleared leftover",
		},
		"repair_2_26138": {
			"voucher": V2,
			"result": "FALSE_SUCCESS — apply reported I4_REPAIRED but residual unchanged",
			"residual_after": flt(v2_rows[0]["classify"].get("residual_value")) if v2_rows else None,
			"current_i4_status": v2_rows[0]["classify"].get("i4_status") if v2_rows else None,
			"current_planner": (v2_rows[0]["decision"] or {}).get("planner_status") if v2_rows else None,
			"eligible_now": (v2_rows[0]["decision"] or {}).get("eligible") if v2_rows else None,
			"previous_blocker": v2_rows[0]["classify"].get("previous_blocker") if v2_rows else None,
			"apply_artifact_present": bool(apply_art),
			"root_cause": (
				"Previous SLE already negative qty/value; MA replay preserved "
				"qty_after=0 stock_value residual. Engine wrongly marked success."
			),
			"gate_fix": "READY_I4 now requires healthy previous + sim clears PZ residual; apply aborts otherwise",
		},
		"consistent_engine": False,
		"verdict_hint": "NOT READY — REPAIR ENGINE REGRESSION (false success on poisoned opening); gate patched — single-root only until re-prove on true READY",
	}

	master = build_master_repair_plan(company=COMPANY)

	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"v26138": v2_rows,
		"broken_bin_30300014_quarantine": {
			"bin": binrow,
			"classification": bin_hits,
			"recent_sles": chain,
			"class": "TEMPORARY_PREFIX_REPLAY_STATE",
			"ui_label": "WAITING_DOWNSTREAM_REPAIR",
			"why": (
				"Prefix replay set Bin to 0/0 from repaired prefix; last SLE on identity "
				"still carries leftover stock_value (downstream poison). Not REAL_BIN_REGRESSION."
			),
		},
		"i4_scan_counts": {
			"total_rows": i4.get("count"),
			"ready": len(ready_sorted),
			"manual": len(manual),
			"waiting": len(waiting),
			"by_status": i4.get("by_status"),
		},
		"first_5_ready": first5,
		"group_preview": group_preview,
		"kpis": kpis,
		"compare": compare,
		"master_plan_summary": {
			"i4_total_rows": master.get("i4_total_rows"),
			"failed_riv": master.get("failed_riv"),
			"roadmap_stages": len(master.get("roadmap") or []),
		},
		"ordering_recommendation": {
			"primary": "oldest-first READY_I4 patient zeros across independent identities",
			"secondary": "SAFE_SEQUENTIAL within same item+warehouse",
			"avoid": "warehouse-blind bulk, month-blind Repair All, Work Order blind when mixed classes",
			"basis": "Current DB: many MANUAL I4 with poisoned openings; only EXACT READY_I4 with healthy prev are safe",
		},
	}
	_dump("session2_finalize.json", out)
	# Compact print
	print(
		json.dumps(
			{
				"v26138_status": compare["repair_2_26138"]["current_i4_status"],
				"v26138_eligible": compare["repair_2_26138"]["eligible_now"],
				"v26138_blocker": compare["repair_2_26138"]["previous_blocker"],
				"ready_count": len(ready_sorted),
				"manual_count": len(manual),
				"first5": [
					{
						"priority": x["priority"],
						"voucher": x["voucher"],
						"item": x["item"],
						"residual": x["residual_value"],
						"sql": x["estimated_sql_updates"],
						"replay": x["affected_sle_count"],
					}
					for x in first5
				],
				"kpis_subset": {
					k: kpis.get(k)
					for k in (
						"I4 Leftover",
						"Patient Zero",
						"Broken Bin",
						"Waiting Downstream Bin",
						"Broken GL",
						"Wrong Rate",
						"Zero Rate",
						"Failed RIV",
						"Repairable",
						"Manual",
						"Ambiguous",
					)
					if isinstance(kpis, dict)
				},
				"independent_ready": group_preview["independent_ready_count"],
			},
			indent=2,
			default=str,
			ensure_ascii=False,
		)
	)
	return out
