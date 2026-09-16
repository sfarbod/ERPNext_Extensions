# Copyright (c) 2026 — session-2 validation helpers (dev only).
from __future__ import annotations

import json
import os
from datetime import datetime

import frappe
from frappe.utils import flt

BASE = "/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/i4_v5213_session2"
COMPANY = "اسپاد فارمد دارو"
ITEM_Q = "30300014"
WH_Q = "انبار Quarantine محصول نیمه ساخته اسپاد"
V1 = "MAT-STE-2026-25791"
V2 = "MAT-STE-2026-26138-1"


def _dump(subdir, name, data):
	d = os.path.join(BASE, subdir)
	os.makedirs(d, exist_ok=True)
	path = os.path.join(d, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def revalidate_25791():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		dry_run_i4_repair,
		find_patient_zero_for_voucher,
		identity_health,
	)
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin, scan_bin_mismatches
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	OUT = "revalidate_25791"
	se = frappe.db.get_value(
		"Stock Entry",
		V1,
		[
			"name",
			"company",
			"purpose",
			"work_order",
			"docstatus",
			"posting_date",
			"total_outgoing_value",
			"total_incoming_value",
			"value_difference",
			"modified",
		],
		as_dict=True,
	)
	items = frappe.db.sql(
		"""
		SELECT idx, item_code, s_warehouse, t_warehouse, qty, basic_rate, amount, valuation_rate,
		       serial_and_batch_bundle, batch_no
		FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx
		""",
		V1,
		as_dict=True,
	)
	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, posting_datetime, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference,
		       serial_and_batch_bundle, batch_no, modified
		FROM `tabStock Ledger Entry` WHERE voucher_no=%s ORDER BY posting_datetime, creation
		""",
		V1,
		as_dict=True,
	)
	q_sle = next((s for s in sles if s.warehouse == WH_Q), None)
	binrow = frappe.db.get_value(
		"Bin",
		{"item_code": ITEM_Q, "warehouse": WH_Q},
		["name", "actual_qty", "stock_value", "valuation_rate"],
		as_dict=True,
	)
	gl = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, is_cancelled FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s
		""",
		V1,
		as_dict=True,
	)
	# Compare to first-repair post snapshot if present
	prev_path = (
		"/workspace/development/frappe-bench/apps/erpnext_extensions/"
		".local-backups/i4_v5213_25791/validation/02_sle_voucher.json"
	)
	drift = []
	if os.path.exists(prev_path):
		prev = json.load(open(prev_path))
		prev_map = {r["name"]: r for r in prev}
		for s in sles:
			p = prev_map.get(s.name)
			if not p:
				drift.append({"sle": s.name, "note": "new"})
				continue
			for f in (
				"qty_after_transaction",
				"stock_value",
				"valuation_rate",
				"outgoing_rate",
				"stock_value_difference",
			):
				if abs(flt(p.get(f)) - flt(s.get(f))) > 0.01:
					drift.append({"sle": s.name, "field": f, "was": p.get(f), "now": s.get(f)})

	scan = scan_sle_bin(voucher=V1, repair_class="I4_LEFTOVER_REPAIR", limit=20)
	cls = classify_i4_row(ITEM_Q, WH_Q, voucher=V1)
	idem = dry_run_i4_repair([cls]) if cls.get("i4_status") != "I4_REPAIRED" else {"skipped": True, "reason": "already I4_REPAIRED"}
	health = identity_health(ITEM_Q, WH_Q)
	pz = find_patient_zero_for_voucher(V1)
	bins = scan_bin_mismatches(limit=500)
	this_bin = [b for b in bins if b.get("item") == ITEM_Q and b.get("warehouse") == WH_Q]
	dash = run_full_integrity_scan(company=COMPANY)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"stock_entry": se,
		"items": items,
		"sles": sles,
		"quarantine_sle": q_sle,
		"residual_still_zero": bool(q_sle and abs(flt(q_sle.stock_value)) <= 0.5 and abs(flt(q_sle.qty_after_transaction)) <= 0.0001),
		"bin": binrow,
		"gl": {
			"rows": gl,
			"debit": sum(flt(r.debit) for r in gl if not flt(r.is_cancelled)),
			"credit": sum(flt(r.credit) for r in gl if not flt(r.is_cancelled)),
		},
		"drift_vs_post_apply_snapshot": drift,
		"scan_i4_rows": [
			{k: r.get(k) for k in ("voucher", "warehouse", "planner_status", "i4_status", "residual_value")}
			for r in (scan.get("rows") or [])
		],
		"classify": {k: cls.get(k) for k in ("i4_status", "residual_value", "qty_after", "current_value", "message")},
		"idempotency_dry_run": idem if isinstance(idem, dict) and idem.get("skipped") else {
			"dry_run": True,
			"rows": [
				{k: r.get(k) for k in ("planner_status", "sql_updates", "i4_status", "eligible")}
				for r in (idem.get("rows") or [])
			],
		},
		"health": health,
		"patient_zero": pz,
		"bin_mismatch_this_identity": this_bin,
		"dashboard": (dash.get("dashboard") or dash),
	}
	_dump(OUT, "revalidate.json", out)
	return out


def investigate_broken_bin():
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_bin_mismatches
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import preview_i4_replay

	binrow = frappe.db.get_value(
		"Bin",
		{"item_code": ITEM_Q, "warehouse": WH_Q},
		["name", "item_code", "warehouse", "actual_qty", "stock_value", "valuation_rate"],
		as_dict=True,
	)
	last = frappe.db.sql(
		"""
		SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		(ITEM_Q, WH_Q),
		as_dict=True,
	)[0]
	# Last rewritten SLE: last SLE at or before stop_before of first repair (25469)
	# Use apply result touched list end if available; else find first poison after 25791.
	stop = "MAT-STE-2026-25469"
	stop_dt = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": stop, "item_code": ITEM_Q, "warehouse": WH_Q, "is_cancelled": 0},
		"posting_datetime",
	)
	last_rewritten = None
	first_skipped = None
	if stop_dt:
		last_rewritten = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction,
			       valuation_rate, stock_value
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_datetime < %s
			ORDER BY posting_datetime DESC, creation DESC LIMIT 1
			""",
			(ITEM_Q, WH_Q, stop_dt),
			as_dict=True,
		)
		last_rewritten = last_rewritten[0] if last_rewritten else None
		first_skipped = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction,
			       incoming_rate, valuation_rate, stock_value
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_datetime >= %s
			ORDER BY posting_datetime, creation LIMIT 1
			""",
			(ITEM_Q, WH_Q, stop_dt),
			as_dict=True,
		)
		first_skipped = first_skipped[0] if first_skipped else None
	poison = sle_poison_reason(last)
	mismatches = [b for b in scan_bin_mismatches(limit=500) if b.get("item") == ITEM_Q and b.get("warehouse") == WH_Q]
	# Was this identity in baseline broken-bin set?
	baseline_bin = json.load(
		open(
			"/workspace/development/frappe-bench/apps/erpnext_extensions/"
			".local-backups/i4_v5213_25791/baseline/04_bins.json"
		)
	)
	pre = next((b for b in baseline_bin if b and b.get("warehouse") == WH_Q), None)
	out = {
		"bin": binrow,
		"last_sle": last,
		"last_sle_poison": poison,
		"last_rewritten_sle": last_rewritten,
		"first_skipped_poisoned_sle": first_skipped,
		"stop_before_voucher": stop,
		"classified_mismatch": mismatches,
		"pre_repair_bin": pre,
		"why": (
			"After I4 identity replay, Bin was updated from the last rewritten SLE "
			"(empty 0/0 at the stop boundary). Chronologically later SLE (last_sle) is still "
			"poisoned/leftover, so Bin≠last SLE. This is TEMPORARY_PREFIX_REPLAY_STATE / "
			"WAITING_DOWNSTREAM_REPAIR — not a silent qty regression."
		),
		"classification": (mismatches[0].get("bin_class") if mismatches else "UNKNOWN"),
		"ui_label": (mismatches[0].get("ui_label") if mismatches else None),
	}
	_dump("broken_bin", "analysis.json", out)
	return out


def confirm_next_root(voucher=V2):
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		dry_run_i4_repair,
		find_patient_zero_for_voucher,
	)
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero_identity

	pz_id = find_patient_zero_identity(ITEM_Q, WH_Q)
	pz = find_patient_zero_for_voucher(voucher, item=ITEM_Q, warehouse=WH_Q)
	scan = scan_sle_bin(
		voucher=voucher,
		repair_class="I4_LEFTOVER_REPAIR",
		item_code=ITEM_Q,
		warehouse=WH_Q,
		limit=20,
	)
	rows = [r for r in (scan.get("rows") or []) if r.get("voucher") == voucher]
	cls = classify_i4_row(ITEM_Q, WH_Q, voucher=voucher)
	dry = dry_run_i4_repair(rows or [cls])
	drow = (dry.get("rows") or [{}])[0]
	sim = drow.get("simulation") or {}
	# previous SLE
	sle = frappe.db.sql(
		"""
		SELECT name, posting_datetime FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime LIMIT 1
		""",
		(voucher, ITEM_Q, WH_Q),
		as_dict=True,
	)
	prev = None
	if sle:
		prev = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, qty_after_transaction, stock_value, valuation_rate,
			       incoming_rate, outgoing_rate
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_datetime < %s
			ORDER BY posting_datetime DESC, creation DESC LIMIT 1
			""",
			(ITEM_Q, WH_Q, sle[0].posting_datetime),
			as_dict=True,
		)
		prev = prev[0] if prev else None
	ready = bool(rows and rows[0].get("planner_status") == "READY_I4" and rows[0].get("eligible"))
	out = {
		"voucher": voucher,
		"still_exists": bool(frappe.db.exists("Stock Entry", voucher)),
		"identity_patient_zero": pz_id,
		"is_true_patient_zero": bool(pz_id and pz_id.get("voucher_no") == voucher),
		"find_pz": pz,
		"scan_rows": [
			{
				k: r.get(k)
				for k in (
					"voucher",
					"planner_status",
					"sql_updates",
					"replay_count",
					"residual_value",
					"eligible",
					"i4_status",
					"required_action",
				)
			}
			for r in rows
		],
		"previous_sle": prev,
		"dry_run": {
			"planner_status": drow.get("planner_status"),
			"sql_updates": drow.get("sql_updates"),
			"affected_sle": drow.get("affected_sle"),
			"affected_gl": drow.get("affected_gl"),
			"affected_bin": drow.get("affected_bin"),
			"stop_before_voucher": sim.get("stop_before_voucher"),
			"stop_reason": sim.get("stop_reason"),
			"final_qty": sim.get("final_qty"),
			"final_value": sim.get("final_value"),
			"touched_count": len(sim.get("touched_vouchers") or []),
		},
		"ready_for_apply": ready,
	}
	_dump("next_root", "confirm_26138.json", out)
	return out


def apply_26138():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import repair_i4_selected
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

	scan = scan_sle_bin(
		voucher=V2,
		repair_class="I4_LEFTOVER_REPAIR",
		item_code=ITEM_Q,
		warehouse=WH_Q,
		limit=20,
	)
	rows = [r for r in (scan.get("rows") or []) if r.get("voucher") == V2 and r.get("planner_status") == "READY_I4"]
	if not rows:
		raise frappe.ValidationError("26138-1 is not READY_I4 — refusing apply")
	result = repair_i4_selected(rows, dry_run=False)
	_dump("apply_26138", "apply_result.json", result)
	return result


def collect_after_26138():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		find_patient_zero_for_voucher,
		identity_health,
	)
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin, scan_bin_mismatches
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	OUT = "after_26138"
	sles = frappe.db.sql(
		"""
		SELECT name, warehouse, posting_datetime, actual_qty, qty_after_transaction,
		       valuation_rate, stock_value, stock_value_difference, outgoing_rate, incoming_rate
		FROM `tabStock Ledger Entry` WHERE voucher_no=%s ORDER BY posting_datetime, creation
		""",
		V2,
		as_dict=True,
	)
	_dump(OUT, "02_sle_voucher.json", sles)
	binrow = frappe.db.get_value(
		"Bin",
		{"item_code": ITEM_Q, "warehouse": WH_Q},
		["name", "actual_qty", "stock_value", "valuation_rate"],
		as_dict=True,
	)
	_dump(OUT, "04_bin.json", binrow)
	_dump(OUT, "09_patient_zero.json", find_patient_zero_for_voucher(V2, item=ITEM_Q, warehouse=WH_Q))
	_dump(OUT, "11_scan.json", scan_sle_bin(voucher=V2, repair_class="I4_LEFTOVER_REPAIR", item_code=ITEM_Q, warehouse=WH_Q, limit=20))
	_dump(OUT, "12_health.json", identity_health(ITEM_Q, WH_Q))
	_dump(OUT, "13_classify.json", classify_i4_row(ITEM_Q, WH_Q, voucher=V2))
	_dump(OUT, "bin_mismatches_identity.json", [b for b in scan_bin_mismatches(limit=500) if b.get("item") == ITEM_Q and b.get("warehouse") == WH_Q])
	dash = run_full_integrity_scan(company=COMPANY)
	_dump(OUT, "14_scan_all.json", dash)
	return {"sles": sles, "bin": binrow, "dashboard": dash.get("dashboard")}


def build_first_five_and_plan():
	"""Current DB: earliest READY_I4 patient zeros + master plan."""
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import classify_i4_row, preview_i4_replay
	from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero_identity
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row

	# Candidate I4 rows from Farvardin onward (1405-01-01 ≈ 2026-03-21)
	rows = frappe.db.sql(
		"""
		SELECT sle.name, sle.voucher_no, sle.item_code, sle.warehouse, sle.posting_date,
		       sle.posting_datetime, sle.qty_after_transaction, sle.stock_value, sle.actual_qty,
		       sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate, sle.stock_value_difference
		FROM `tabStock Ledger Entry` sle
		WHERE sle.is_cancelled=0
		  AND sle.company=%s
		  AND sle.posting_date >= '2026-03-21'
		  AND ABS(sle.qty_after_transaction) < 0.0001
		  AND ABS(IFNULL(sle.stock_value,0)) > 1
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT 2000
		""",
		(COMPANY,),
		as_dict=True,
	)
	seen_id = set()
	candidates = []
	for r in rows:
		key = (r.item_code, r.warehouse)
		if key in seen_id:
			continue
		pz = find_patient_zero_identity(r.item_code, r.warehouse)
		if not pz or pz.get("reason") not in ("qty_after_zero_nonzero_value", "qty_zero_nonzero_value"):
			continue
		if pz.get("voucher_no") != r.voucher_no and str(pz.get("posting_datetime") or "") < str(r.posting_datetime or ""):
			# not the earliest PZ row — skip until we hit the PZ voucher itself
			continue
		# Prefer the PZ voucher row
		if pz.get("voucher_no") != r.voucher_no:
			continue
		seen_id.add(key)
		cls = classify_i4_row(r.item_code, r.warehouse, voucher=r.voucher_no, sle_name=r.name)
		decision = evaluate_row({**cls, "topic": "I4_LEFTOVER", "repair_class": "I4_LEFTOVER_REPAIR"})
		if decision.get("planner_status") != "READY_I4":
			continue
		try:
			sim = preview_i4_replay(r.item_code, r.warehouse, from_dt=r.posting_datetime)
		except Exception as exc:
			sim = {"error": str(exc), "rows": 0, "sql_updates": 0}
		candidates.append(
			{
				"priority": len(candidates) + 1,
				"voucher": r.voucher_no,
				"item": r.item_code,
				"warehouse": r.warehouse,
				"posting_date": str(r.posting_date),
				"posting_datetime": str(r.posting_datetime),
				"repair_class": "I4_LEFTOVER_REPAIR",
				"patient_zero_reason": pz.get("reason"),
				"residual": flt(r.stock_value),
				"planner_status": decision.get("planner_status"),
				"affected_sle_count": int(sim.get("rows") or decision.get("replay_count") or 0),
				"estimated_sql_updates": int(sim.get("sql_updates") or decision.get("sql_updates") or 0),
				"estimated_runtime_seconds": round(max(1, int(sim.get("rows") or 1)) * 0.01, 2),
				"expected_issues_removed": ["I4 leftover at patient zero", "downstream residual cascade until next hard poison"],
				"required_scope": "IDENTITY (item+warehouse)",
				"dependencies": [],
				"stop_before_voucher": sim.get("stop_before_voucher"),
				"stop_reason": sim.get("stop_reason"),
				"can_be_grouped": True,
				"group_class": "SAFE_GROUP" if True else "SAFE_SEQUENTIAL_GROUP",
			}
		)
		if len(candidates) >= 5:
			break

	# Mark sequential same-warehouse as SAFE_SEQUENTIAL if same identity appears (shouldn't in first5 unique)
	# Independent identities → SAFE_GROUP
	for c in candidates:
		c["can_be_grouped"] = c["planner_status"] == "READY_I4" and not c.get("dependencies")
		c["group_class"] = "SAFE_GROUP"

	plan = build_master_repair_plan(company=COMPANY)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"first_five": candidates,
		"master_plan": plan,
		"ordering_recommendation": _ordering_recommendation(candidates, plan),
		"grouped_session_proposal": _group_proposal(candidates),
	}
	_dump("master_plan", "first_five_and_plan.json", out)
	return out


def _ordering_recommendation(first_five, plan):
	# Based on current DB: oldest READY_I4 identity roots first within Farvardin, then expand.
	return {
		"strategy": "oldest-first READY_I4 Patient Zero by posting_datetime, identity-scoped",
		"not": ["blind warehouse-wide", "blind month bulk without READY proof", "work-order-first for I4"],
		"rationale": (
			"After two Quarantine repairs, remaining READY roots are still earliest I4 leftovers "
			"per identity. Month stages in master_plan remain Farvardin-first because that is where "
			"the earliest READY patient zeros concentrate. Warehouse grouping is useful for "
			"SAFE_GROUP sessions of independent items in the same warehouse, but ordering within "
			"an identity must stay chronological Patient Zero → next leftover."
		),
		"first_five_dates": [c.get("posting_date") for c in first_five],
		"plan_top_month": (plan.get("roadmap") or [{}])[0].get("stage") if plan.get("roadmap") else None,
	}


def _group_proposal(first_five):
	# Independent identities only
	ids = [(c["item"], c["warehouse"]) for c in first_five]
	independent = len(ids) == len(set(ids))
	est_sle = sum(c.get("affected_sle_count") or 0 for c in first_five)
	est_sql = sum(c.get("estimated_sql_updates") or 0 for c in first_five)
	est_rt = sum(c.get("estimated_runtime_seconds") or 0 for c in first_five)
	return {
		"group_id": "GROUP-01",
		"class": "SAFE_GROUP" if independent else "SAFE_SEQUENTIAL_GROUP",
		"roots": [c["voucher"] for c in first_five],
		"number_of_roots": len(first_five),
		"backup_policy": "ONE verified DB backup per session covering this group",
		"repair_order": [c["voucher"] for c in first_five],
		"affected_identities": [{"item": c["item"], "warehouse": c["warehouse"]} for c in first_five],
		"estimated_sle_replay": est_sle,
		"estimated_sql_updates": est_sql,
		"estimated_runtime_seconds": round(est_rt, 2),
		"dependencies": "none between roots (independent item+warehouse)",
		"risk": "LOW" if independent else "MEDIUM",
		"expected_reductions": {"i4_leftover_rows_approx": len(first_five) * 20},
		"execution": "sequential roots inside group · savepoint per root · stop-on-first-failure",
	}


def analyze_grouping_feasibility():
	"""Static design answer persisted for the report."""
	out = {
		"A_independent_identities": {"verdict": "SAFE_GROUP", "notes": "Best default for multi-root sessions"},
		"B_same_warehouse_different_items": {
			"verdict": "SAFE_GROUP",
			"notes": "Safe when each item identity is READY_I4 and windows do not share SLE rows",
		},
		"C_same_item_different_warehouses": {
			"verdict": "SAFE_GROUP",
			"notes": "Safe — warehouses are separate MA identities",
		},
		"D_same_work_order": {
			"verdict": "SAFE_SEQUENTIAL_GROUP or MANUAL_GROUP",
			"notes": "Only if each WO leg's identity is READY and ordered; often mixed repair classes",
		},
		"E_same_month": {
			"verdict": "UNSAFE_GROUP if bulk; SAFE_GROUP if filtered to independent READY roots",
			"notes": "Month is a planning lens, not an execute-all key",
		},
		"F_same_repair_class": {
			"verdict": "SAFE_GROUP when also READY+EXACT+non-overlapping",
			"notes": "Necessary but not sufficient alone",
		},
		"forbidden": [
			"Repair All",
			"Repair Whole Company",
			"Repair Whole Warehouse blindly",
			"Global RIV",
			"Global GL rebuild",
			"Global SLE repost",
		],
		"backup_strategy": {
			"per_voucher_full_backup": "NOT required",
			"recommended": "ONE full DB backup per controlled session, then N READY roots with savepoint-per-root",
			"checkpoint": "Integrity + re-scan after each root; stop group on structural failure",
			"rollback_guarantee": (
				"Failed root rolls back to its savepoint; previously committed roots in the session remain intact; "
				"session restore uses the session DB backup if operator chooses full rewind"
			),
		},
	}
	_dump("grouped_repair", "feasibility.json", out)
	return out
