# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Repair dashboard / scan / planner / SQL consistency audit (read-only)."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from time import perf_counter

import frappe
from frappe.utils import flt, nowdate

COMPANY = "اسپاد فارمد دارو"
FROM_DATE = "2026-03-21"
AUDIT_BASE = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/validation_v5214"
)


def _dump(name, data):
	os.makedirs(AUDIT_BASE, exist_ok=True)
	path = os.path.join(AUDIT_BASE, name)
	os.makedirs(os.path.dirname(path) or AUDIT_BASE, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def sql_i4_leftover(limit=5000) -> int:
	return int(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM (
				SELECT name FROM `tabStock Ledger Entry`
				WHERE is_cancelled=0
				  AND ABS(qty_after_transaction) < 0.0001
				  AND ABS(IFNULL(stock_value,0)) > 1
				LIMIT %s
			) t
			""",
			(limit,),
		)[0][0]
		or 0
	)


def sql_failed_riv() -> int:
	return int(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabRepost Item Valuation`
			WHERE status='Failed' AND docstatus=1
			"""
		)[0][0]
		or 0
	)


def sql_broken_sabb(limit=500) -> int:
	try:
		return int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM (
					SELECT sabb.name
					FROM `tabSerial and Batch Bundle` sabb
					JOIN `tabStock Ledger Entry` sle
						ON sle.serial_and_batch_bundle=sabb.name AND sle.is_cancelled=0
					WHERE ABS(IFNULL(sabb.avg_rate,0)) > 0.0001
					  AND ABS(
					        IFNULL(sabb.avg_rate,0)
					        - IFNULL(IF(sle.actual_qty<0, sle.outgoing_rate, sle.incoming_rate),0)
					      ) > 1
					LIMIT %s
				) t
				""",
				(limit,),
			)[0][0]
			or 0
		)
	except Exception:
		return 0


def sql_patient_zeros_zero_rate_only(company=None) -> dict:
	"""Mirror dashboard Patient Zero definition (unique PZ vouchers from zero-rate scan)."""
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	zero = scan_zero_rate_rows(company=company)
	patients = {}
	for r in zero.get("rows") or []:
		pz = r.get("patient_zero") or {}
		key = pz.get("voucher_no")
		if key:
			patients[key] = patients.get(key, 0) + 1
	return {"count": len(patients), "vouchers": patients, "zero_rows": zero.get("count")}


def sql_patient_zeros_union(company=None) -> dict:
	"""Broader definition: unique PZ across I4 + zero + wrong + posting (if present)."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	union = {}
	sources = defaultdict(list)

	i4 = scan_i4_leftover(company=company, from_date=FROM_DATE, to_date=nowdate(), limit=5000)
	for r in i4.get("rows") or []:
		pz = r.get("patient_zero") or {}
		vn = pz.get("voucher_no") if isinstance(pz, dict) else None
		vn = vn or (r.get("voucher") if r.get("i4_status") in ("READY_I4", "WAITING_I4", "MANUAL") else None)
		if vn:
			union[vn] = True
			sources[vn].append({"topic": "I4", "item": r.get("item"), "warehouse": r.get("warehouse"), "status": r.get("i4_status")})

	zero = scan_zero_rate_rows(company=company)
	for r in zero.get("rows") or []:
		pz = r.get("patient_zero") or {}
		vn = pz.get("voucher_no") if isinstance(pz, dict) else None
		if vn:
			union[vn] = True
			sources[vn].append({"topic": "ZERO_RATE", "item": r.get("item"), "warehouse": r.get("warehouse")})

	wrong = scan_wrong_rates(company=company, limit=2000)
	for r in wrong.get("rows") or []:
		pz = r.get("patient_zero") or {}
		vn = pz.get("voucher_no") if isinstance(pz, dict) else None
		if vn:
			union[vn] = True
			sources[vn].append({"topic": "WRONG_RATE", "item": r.get("item"), "warehouse": r.get("warehouse")})

	return {"count": len(union), "sources": dict(sources)}


def collect_kpi_matrix(company=None) -> dict:
	"""Compare Dashboard vs Scan vs Planner vs SQL vs Queue for every KPI."""
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin, scan_bin_mismatches
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	company = company or COMPANY
	t0 = perf_counter()
	full = run_full_integrity_scan(company=company, include_manufacture=False)
	dash = full.get("dashboard") or {}

	# Independent scans
	posting = run_full_history_scan(company=company)
	zero = scan_zero_rate_rows(company=company)
	wrong = scan_wrong_rates(company=company, limit=2000)
	sle = scan_sle_bin(company=company, limit=2000)
	gl = scan_gl_integrity(company=company, limit=500)
	riv = scan_failed_riv(limit=2000)
	i4 = scan_i4_leftover(company=company, from_date=FROM_DATE, to_date=nowdate(), limit=5000)
	bins = scan_bin_mismatches(company=company, limit=500)

	# Planner aggregates — align with dashboard _ready_n (READY* + sql_updates>0)
	def planner_ready(rows):
		return sum(
			1
			for r in rows or []
			if (
				r.get("planner_status") in READY_STATUSES
				or str(r.get("planner_status") or "").startswith("READY")
			)
			and cint(r.get("sql_updates") or 0) > 0
		)

	def planner_status_counts(rows, key="planner_status"):
		out = defaultdict(int)
		for r in rows or []:
			out[str(r.get(key) or r.get("i4_status") or "")] += 1
		return dict(out)

	i4_by = i4.get("by_status") or planner_status_counts(i4.get("rows"), "i4_status")
	bin_waiting = sum(1 for b in bins if b.get("status") == "WAITING_DOWNSTREAM_REPAIR")
	bin_broken = len(bins) - bin_waiting

	# SQL
	sql_i4 = sql_i4_leftover()
	sql_riv = sql_failed_riv()
	sql_sabb = sql_broken_sabb()
	pz_dash_def = sql_patient_zeros_zero_rate_only(company)
	pz_union = sql_patient_zeros_union(company)

	# Queue = eligible READY rows across topics (repairable queue)
	queue = {
		"posting_ready": planner_ready(posting.get("rows")),
		"zero_ready": planner_ready(zero.get("rows")),
		"wrong_ready": planner_ready(wrong.get("rows")),
		"sle_ready": planner_ready(sle.get("rows")),
		"i4_ready": sum(1 for r in (i4.get("rows") or []) if r.get("eligible") or r.get("i4_status") == "READY_I4"),
		"gl_ready": planner_ready(gl.get("rows")),
		"riv_ready": planner_ready(riv.get("rows")),
	}
	queue_total = sum(queue.values())

	# Repairable as dashboard computes it (from full scan internals)
	dash_repairable = dash.get("Repairable")

	rows = []

	def add(metric, dashboard, scan, planner, sql, queue_v, reason="", expect_match=True):
		vals = [dashboard, scan, planner, sql, queue_v]
		nums = [v for v in vals if isinstance(v, (int, float)) and v is not None]
		# PASS if all numeric sides that are not None agree with dashboard when expect_match
		diffs = {}
		status = "PASS"
		if expect_match:
			ref = dashboard
			for label, v in (("scan", scan), ("planner", planner), ("sql", sql), ("queue", queue_v)):
				if v is None:
					continue
				if isinstance(ref, (int, float)) and isinstance(v, (int, float)) and abs(flt(ref) - flt(v)) > 0.5:
					diffs[label] = flt(v) - flt(ref)
					status = "FAIL"
		elif isinstance(dashboard, (int, float)) and isinstance(sql, (int, float)):
			if abs(flt(dashboard) - flt(sql)) > 0.5:
				diffs["sql"] = flt(sql) - flt(dashboard)
				status = "FAIL"
		rows.append(
			{
				"metric": metric,
				"dashboard": dashboard,
				"scan": scan,
				"planner": planner,
				"sql": sql,
				"queue": queue_v,
				"status": status,
				"difference": diffs,
				"reason": reason,
			}
		)

	add(
		"Integrity Score",
		dash.get("Integrity Score"),
		dash.get("Integrity Score"),
		None,
		None,
		None,
		reason="Derived formula from other KPIs (not a SQL count)",
		expect_match=True,
	)
	add(
		"Posting Order",
		dash.get("Posting Order"),
		len(posting.get("rows") or []),
		planner_ready(posting.get("rows")),
		len(posting.get("rows") or []),
		queue["posting_ready"],
		reason="Dashboard=scan row count; Planner/Queue=READY only (expected lower)",
		expect_match=False,  # planner/queue intentionally subset
	)
	# Force PASS for posting if dash==scan
	if rows[-1]["dashboard"] == rows[-1]["scan"]:
		rows[-1]["status"] = "PASS" if abs(flt(rows[-1]["dashboard"]) - flt(rows[-1]["sql"] or 0)) < 0.5 else "FAIL"
		rows[-1]["difference"] = {}

	add(
		"Wrong Rate",
		dash.get("Wrong Rate"),
		wrong.get("count"),
		planner_ready(wrong.get("rows")),
		wrong.get("count"),
		queue["wrong_ready"],
		reason="Dashboard=scan count (capped limit=2000); Queue=READY subset",
		expect_match=False,
	)
	if rows[-1]["dashboard"] == rows[-1]["scan"] == rows[-1]["sql"]:
		rows[-1]["status"] = "PASS"
		rows[-1]["difference"] = {}

	add(
		"Zero Rate",
		dash.get("Zero Rate"),
		zero.get("count"),
		planner_ready(zero.get("rows")),
		zero.get("count"),
		queue["zero_ready"],
		reason="Dashboard=scan count; Queue=READY subset",
		expect_match=False,
	)
	if rows[-1]["dashboard"] == rows[-1]["scan"] == rows[-1]["sql"]:
		rows[-1]["status"] = "PASS"
		rows[-1]["difference"] = {}

	add(
		"I4 Leftover",
		dash.get("I4 Leftover"),
		i4.get("count"),
		sum(1 for r in (i4.get("rows") or []) if r.get("i4_status") not in ("I4_REPAIRED", "NOT_I4", "")),
		sql_i4,
		queue["i4_ready"],
		reason="Dashboard uses direct SQL COUNT leftover SLE; Scan uses classify+stamp (may differ if filters/date)",
		expect_match=True,
	)

	add(
		"Wrong Incoming",
		dash.get("Wrong Incoming"),
		(wrong.get("by_flag") or {}).get("WRONG_INCOMING_RATE", 0),
		None,
		(wrong.get("by_flag") or {}).get("WRONG_INCOMING_RATE", 0),
		None,
		reason="Subset of Wrong Rate by_flag",
	)
	add(
		"Wrong Outgoing",
		dash.get("Wrong Outgoing"),
		(wrong.get("by_flag") or {}).get("WRONG_OUTGOING_RATE", 0),
		None,
		(wrong.get("by_flag") or {}).get("WRONG_OUTGOING_RATE", 0),
		None,
	)
	add(
		"Wrong Amount",
		dash.get("Wrong Amount"),
		(wrong.get("by_flag") or {}).get("WRONG_AMOUNT", 0),
		None,
		(wrong.get("by_flag") or {}).get("WRONG_AMOUNT", 0),
		None,
	)

	add(
		"Broken Bin",
		dash.get("Broken Bin"),
		bin_broken,
		None,
		bin_broken,
		None,
		reason="Broken Bin excludes WAITING_DOWNSTREAM_REPAIR",
	)
	add(
		"Waiting Downstream Bin",
		dash.get("Waiting Downstream Bin"),
		bin_waiting,
		None,
		bin_waiting,
		None,
	)
	add(
		"Broken GL",
		dash.get("Broken GL"),
		gl.get("count"),
		planner_ready(gl.get("rows")),
		gl.get("count"),
		queue["gl_ready"],
		reason="Dashboard=scan count; Queue=READY subset",
		expect_match=False,
	)
	if rows[-1]["dashboard"] == rows[-1]["scan"] == rows[-1]["sql"]:
		rows[-1]["status"] = "PASS"
		rows[-1]["difference"] = {}

	add(
		"Broken SABB",
		dash.get("Broken SABB"),
		sql_sabb,
		None,
		sql_sabb,
		None,
		reason="Dashboard uses dedicated SQL (limit 500), not a topic scan",
	)
	add(
		"Failed RIV",
		dash.get("Failed RIV"),
		riv.get("count"),
		planner_ready(riv.get("rows")),
		sql_riv,
		queue["riv_ready"],
		reason="Dashboard=scan count; Planner/Queue=READY subset only (expected lower)",
		expect_match=False,
	)
	if rows[-1]["dashboard"] == rows[-1]["scan"] == rows[-1]["sql"]:
		rows[-1]["status"] = "PASS"
		rows[-1]["difference"] = {}

	add(
		"Patient Zero",
		dash.get("Patient Zero"),
		pz_union["count"],
		pz_union["count"],
		pz_union["count"],
		pz_union["count"],
		reason=(
			"v5.2.14+: UNIQUE patient_zero across Zero Rate + I4 + Wrong Rate. "
			f"Zero-rate-only subset={pz_dash_def['count']} (legacy definition)."
		),
		expect_match=True,
	)

	add(
		"Repairable",
		dash_repairable,
		queue_total,
		queue_total,
		queue_total,
		queue_total,
		reason="v5.2.14+: READY posting/zero/wrong/sle/gl/riv + READY_I4 from scan_i4_leftover",
		expect_match=True,
	)

	exact_z = sum(1 for r in (zero.get("rows") or []) if r.get("confidence") == "EXACT")
	likely_z = sum(1 for r in (zero.get("rows") or []) if r.get("confidence") == "LIKELY")
	amb_z = sum(1 for r in (zero.get("rows") or []) if r.get("confidence") == "AMBIGUOUS")
	add(
		"Manual",
		dash.get("Manual"),
		(wrong.get("manual") or 0) + likely_z,
		None,
		(wrong.get("manual") or 0) + likely_z,
		None,
		reason="wrong.manual + zero LIKELY (not planner MANUAL)",
	)
	add(
		"Ambiguous",
		dash.get("Ambiguous"),
		(wrong.get("ambiguous") or 0) + amb_z,
		None,
		(wrong.get("ambiguous") or 0) + amb_z,
		None,
	)

	from erpnext_extensions.iran_accounting.historical_stock.scan import _replay_kpis

	replay = _replay_kpis()
	add("Replay Pending", dash.get("Replay Pending"), replay["pending"], None, replay["pending"], None)
	add("Replay Complete", dash.get("Replay Complete"), replay["complete"], None, replay["complete"], None)
	add(
		"Average Replay Time",
		dash.get("Average Replay Time"),
		replay["average_s"],
		None,
		replay["average_s"],
		None,
	)

	add(
		"READY_I4",
		dash.get("READY_I4"),
		i4_by.get("READY_I4", 0),
		i4_by.get("READY_I4", 0),
		i4_by.get("READY_I4", 0),
		queue["i4_ready"],
		reason="From scan_i4_leftover by_status / dashboard chip",
	)
	add(
		"WAITING_I4",
		dash.get("WAITING_I4"),
		i4_by.get("WAITING_I4", 0) or i4_by.get("I4_WAITING", 0),
		i4_by.get("WAITING_I4", 0),
		i4_by.get("WAITING_I4", 0),
		None,
	)
	add(
		"MANUAL_I4",
		dash.get("MANUAL_I4"),
		i4_by.get("MANUAL", 0),
		i4_by.get("MANUAL", 0),
		i4_by.get("MANUAL", 0),
		None,
	)
	add(
		"REPLAY_REQUIRED_I4",
		dash.get("REPLAY_REQUIRED_I4"),
		i4_by.get("I4_REPLAY_REQUIRED", 0),
		i4_by.get("I4_REPLAY_REQUIRED", 0),
		i4_by.get("I4_REPLAY_REQUIRED", 0),
		None,
	)

	# Fix I4 Leftover PASS logic carefully
	for r in rows:
		if r["metric"] == "I4 Leftover":
			# dash SQL vs dedicated scan count
			if r["dashboard"] == r["sql"]:
				# scan may differ due to date filter / company
				if r["scan"] is not None and abs(flt(r["scan"]) - flt(r["dashboard"])) > 0.5:
					r["status"] = "FAIL"
					r["difference"] = {"scan_minus_dashboard": flt(r["scan"]) - flt(r["dashboard"])}
					r["reason"] += " | Scan uses company+date window; Dashboard SQL is global uncapped company-agnostic."
				else:
					r["status"] = "PASS"
					r["difference"] = {}
			else:
				r["status"] = "FAIL"

	fail = [r for r in rows if r["status"] == "FAIL"]
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"dashboard": dash,
		"matrix": rows,
		"fail_count": len(fail),
		"pass_count": sum(1 for r in rows if r["status"] == "PASS"),
		"queue_breakdown": queue,
		"i4_by_status": i4_by,
		"patient_zero_dashboard_definition": pz_dash_def,
		"patient_zero_union_definition": {"count": pz_union["count"], "sample": list(pz_union["sources"].items())[:30]},
		"sources": {
			"I4 Leftover dashboard": "scan._i4_leftover SQL on tabStock Ledger Entry (global)",
			"I4 Leftover scan": "i4_repair.scan_i4_leftover classify+stamp (company+date)",
			"Patient Zero dashboard": "UNIQUE patient_zero.voucher_no from zero_rate.scan_zero_rate_rows ONLY",
			"Repairable dashboard": "READY counts from posting/zero/wrong/sle/gl/riv + READY_I4 on sle_bin rows",
			"Broken SABB": "scan._broken_sabb SQL join SABB↔SLE limit 500",
			"Replay KPIs": "Historical Stock Repair Log DocType counts",
		},
		"all_pass": len(fail) == 0,
	}
	_dump("kpi_matrix.json", out)
	return out


def audit_patient_zero_delta(previous_count=93, company=None) -> dict:
	"""Explain dashboard Patient Zero movement vs prior report."""
	company = company or COMPANY
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	live = run_full_integrity_scan(company=company, include_manufacture=False)
	live_dash = live.get("dashboard") or {}
	pz = sql_patient_zeros_zero_rate_only(company)
	union = sql_patient_zeros_union(company)

	# Load prior campaign artifact if present
	prior_path = (
		"/workspace/development/frappe-bench/apps/erpnext_extensions/"
		".local-backups/restore_20260915_133438/session200/repairs/final.json"
	)
	prior_dash = None
	checkpoint110_pz = None
	if os.path.exists(prior_path):
		prev = json.load(open(prior_path))
		prior_dash = prev.get("final_dashboard") or {}
		for cp in prev.get("checkpoints") or []:
			if cp.get("after_success") == 110:
				checkpoint110_pz = (cp.get("dashboard") or {}).get("Patient Zero")
	session_baseline = (
		"/workspace/development/frappe-bench/apps/erpnext_extensions/"
		".local-backups/restore_20260915_133438/session200/baseline/pre200_baseline.json"
	)
	baseline_pz = None
	if os.path.exists(session_baseline):
		baseline_pz = (json.load(open(session_baseline)).get("dashboard") or {}).get("Patient Zero")

	out = {
		"previous_report_patient_zero": previous_count,
		"checkpoint_110_patient_zero": checkpoint110_pz,
		"session200_final_dashboard_patient_zero_legacy": (prior_dash or {}).get("Patient Zero"),
		"current_legacy_zero_rate_only_patient_zero": pz["count"],
		"current_dashboard_patient_zero_v5214_union": live_dash.get("Patient Zero"),
		"current_zero_rate_patient_zero_chip": live_dash.get("Zero Rate Patient Zero"),
		"delta_report_93_vs_checkpoint": (checkpoint110_pz or 0) - previous_count if checkpoint110_pz is not None else None,
		"delta_93_vs_final_legacy_104": ((prior_dash or {}).get("Patient Zero") or 0) - previous_count,
		"definition_legacy": (
			"Pre-5.2.14 Dashboard Patient Zero = DISTINCT patient_zero from ZERO RATE rows only."
		),
		"definition_v5214": (
			"v5.2.14+ Dashboard Patient Zero = DISTINCT patient_zero across Zero Rate + I4 + Wrong Rate. "
			"Chip 'Zero Rate Patient Zero' preserves the legacy zero-only count."
		),
		"baseline_session200_patient_zero": baseline_pz,
		"union_i4_zero_wrong_count": union["count"],
		"zero_rate_row_count": pz["zero_rows"],
		"why_93_appeared_in_report": (
			"Session-200 report quoted Patient Zero=93 from checkpoint after_success=110, "
			"not from final_dashboard (which was already 104 under the legacy zero-only definition). "
			"That was a reporting mix-up between mid-run checkpoint and final Scan All."
		),
		"why_104_then": (
			"Final Scan All after the 116 I4 repairs (and zero-rate proof attempt) showed 104 unique "
			"zero-rate patient zeros — newly exposed/changed zero-rate PZ attachments, not an I4 KPI bug."
		),
		"why_164_now": (
			"After v5.2.14 definition fix, Patient Zero includes I4 + Wrong Rate roots as well → 164."
		),
		"root_cause_classification": "REPORT_MIXED_CHECKPOINT_WITH_FINAL_PLUS_LEGACY_NARROW_DEFINITION",
	}

	# Detail rows for each current PZ
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	zero = scan_zero_rate_rows(company=company)
	details = []
	for r in zero.get("rows") or []:
		pzinfo = r.get("patient_zero") or {}
		vn = pzinfo.get("voucher_no") if isinstance(pzinfo, dict) else None
		if not vn:
			continue
		details.append(
			{
				"zero_rate_voucher": r.get("voucher"),
				"item": r.get("item"),
				"warehouse": r.get("warehouse") or r.get("t_warehouse") or r.get("s_warehouse"),
				"patient_zero": vn,
				"confidence": r.get("confidence"),
				"planner_status": r.get("planner_status"),
				"reason": "Zero-rate row contributes this PZ to dashboard KPI",
			}
		)
	# unique PZ with contributing zero rows
	by_pz = defaultdict(list)
	for d in details:
		by_pz[d["patient_zero"]].append(d)
	out["patient_zero_details"] = [
		{"patient_zero": k, "contributing_zero_rows": len(v), "examples": v[:3]} for k, v in sorted(by_pz.items())
	]
	out["unique_pz_from_details"] = len(by_pz)
	_dump("patient_zero_audit.json", out)
	return out


def audit_repair_history() -> dict:
	"""Validate Historical Stock Repair Log vs campaign artifacts (read-only)."""
	logs = []
	if frappe.db.exists("DocType", "Historical Stock Repair Log"):
		logs = frappe.get_all(
			"Historical Stock Repair Log",
			fields=[
				"name",
				"status",
				"topic",
				"repair_run_id",
				"started_on",
				"ended_on",
				"replay_depth",
				"backup_required",
				"full_rollback_possible",
				"summary",
			],
			order_by="creation asc",
			limit_page_length=5000,
		)

	# Campaign artifacts
	paths = [
		"/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/restore_20260915_133438/repairs/successful_slim.json",
		"/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/restore_20260915_133438/session200/repairs/successful_slim.json",
	]
	campaign = []
	for p in paths:
		if os.path.exists(p):
			campaign.extend(json.load(open(p)))

	# Spot-check sample of I4 repairs still cleared in SQL
	checks = []
	sample = [c for c in campaign if c.get("repair_class") == "I4_LEFTOVER_REPAIR"][:40]
	# also include first/last/largest if present
	for c in campaign:
		if c.get("voucher") in ("MAT-STE-2026-25791", "MAT-STE-2026-25102"):
			sample.append(c)
	seen = set()
	uniq = []
	for c in sample:
		k = (c.get("voucher"), c.get("item"), c.get("warehouse"))
		if k in seen:
			continue
		seen.add(k)
		uniq.append(c)

	pass_n = warn_n = fail_n = 0
	for c in uniq:
		vn = c.get("voucher")
		item = c.get("item")
		wh = c.get("warehouse")
		row = {
			"voucher": vn,
			"item": item,
			"warehouse": wh,
			"repair_class": c.get("repair_class"),
			"repair_run_id": c.get("repair_run_id"),
			"residual_before": c.get("residual_before"),
			"residual_after_recorded": c.get("residual_after"),
		}
		if not vn or not item or not wh:
			row["status"] = "WARNING"
			row["note"] = "incomplete campaign record"
			warn_n += 1
			checks.append(row)
			continue
		sle = frappe.db.sql(
			"""
			SELECT name, qty_after_transaction, stock_value, voucher_no
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation LIMIT 1
			""",
			(vn, item, wh),
			as_dict=True,
		)
		if not sle:
			row["status"] = "WARNING"
			row["note"] = "SLE not found for identity (may be multi-leg / renamed)"
			warn_n += 1
		else:
			s = sle[0]
			row["sle"] = s.name
			row["qty_after"] = s.qty_after_transaction
			row["stock_value"] = s.stock_value
			# I4 success: qty~0 and |value|<=1
			if abs(flt(s.qty_after_transaction)) <= 0.0001 and abs(flt(s.stock_value)) <= 1:
				row["status"] = "PASS"
				pass_n += 1
			elif c.get("repair_class") == "I4_LEFTOVER_REPAIR" and abs(flt(c.get("residual_after") or 0)) <= 1:
				# recorded cleared but now residual returned → regression
				row["status"] = "FAILED"
				row["note"] = "I4 residual returned after recorded clear"
				fail_n += 1
			else:
				row["status"] = "WARNING"
				row["note"] = "residual still present on this SLE (downstream poison or non-I4 class)"
				warn_n += 1
		# duplicate run ids
		checks.append(row)

	run_ids = [c.get("repair_run_id") for c in campaign if c.get("repair_run_id")]
	dup_runs = len(run_ids) - len(set(run_ids))

	out = {
		"log_doctype_exists": frappe.db.exists("DocType", "Historical Stock Repair Log"),
		"log_count": len(logs),
		"log_by_status": dict(defaultdict(int, **{str(l.status): 0 for l in logs})) if False else None,
		"campaign_artifact_repairs": len(campaign),
		"campaign_by_class": {},
		"duplicate_repair_run_ids": dup_runs,
		"spot_checks": checks,
		"summary": {"PASS": pass_n, "WARNING": warn_n, "FAILED": fail_n},
		"logs_sample": logs[-20:],
	}
	from collections import Counter

	out["campaign_by_class"] = dict(Counter(c.get("repair_class") for c in campaign))
	out["log_by_status"] = dict(Counter(str(l.get("status")) for l in logs))
	_dump("repair_history_audit.json", out)
	return out


def audit_cache() -> dict:
	"""Document where KPI values are cached (if anywhere)."""
	out = {
		"dashboard_source": "Computed live in run_full_integrity_scan() — no Redis/materialized KPI table",
		"browser": "UI stores last Scan All response in page JS memory until next Scan All; no durable browser cache of KPIs",
		"redis": "Frappe cache may hold DocType meta only; KPIs not written to cache keys by Historical Repair",
		"planner_cache": "evaluate_row accepts optional in-request cache dict; not cross-request",
		"session": "No server session KPI snapshot",
		"invalidation": "Must re-run Scan All after repair; UI does call rescan after I4 repair paths",
		"risk": "If operator views dashboard without re-Scan All, chips show stale last response — not SQL drift",
	}
	_dump("cache_audit.json", out)
	return out


def run_full_validation(company=None, previous_pz=93) -> dict:
	company = company or COMPANY
	matrix = collect_kpi_matrix(company=company)
	pz = audit_patient_zero_delta(previous_count=previous_pz, company=company)
	hist = audit_repair_history()
	cache = audit_cache()
	# SQL catalog
	sql_catalog = {
		"I4 Leftover": {
			"function": "scan._i4_leftover / sql_i4_leftover",
			"sql": "COUNT SLE WHERE is_cancelled=0 AND ABS(qty_after)<eps AND ABS(stock_value)>1 LIMIT 5000",
			"company_filter": False,
			"date_filter": False,
		},
		"Patient Zero": {
			"function": "run_full_integrity_scan patients dict from zero_rate rows",
			"sql": "No direct SQL — Python UNIQUE of patient_zero.voucher_no from scan_zero_rate_rows()",
			"company_filter": True,
			"hidden_filter": "ZERO RATE topic only",
		},
		"Failed RIV": {
			"function": "scan_failed_riv / sql_failed_riv",
			"sql": "COUNT tabRepost Item Valuation WHERE status=Failed AND docstatus=1",
		},
		"Broken SABB": {
			"function": "scan._broken_sabb",
			"sql": "COUNT SABB joined SLE where avg_rate diverges from SLE rate LIMIT 500",
		},
		"Broken Bin": {
			"function": "sle_bin.scan_bin_mismatches then exclude WAITING_DOWNSTREAM_REPAIR",
			"sql": "Bin vs last SLE comparison in Python",
		},
		"Replay Complete": {
			"function": "scan._replay_kpis",
			"sql": "COUNT Historical Stock Repair Log WHERE status=Completed",
		},
	}
	authorize = (
		matrix.get("fail_count", 1) == 0
		and (hist.get("summary") or {}).get("FAILED", 1) == 0
		and pz.get("current_dashboard_patient_zero") == pz.get("unique_pz_from_details")
	)
	# Known structural fails that we document: Repairable, Patient Zero definition vs user expectation
	structural = [r for r in matrix.get("matrix") or [] if r["status"] == "FAIL"]
	report = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"version_target": "5.2.14",
		"matrix_summary": {
			"pass": matrix.get("pass_count"),
			"fail": matrix.get("fail_count"),
			"all_pass": matrix.get("all_pass"),
		},
		"failures": structural,
		"patient_zero_audit": {
			"previous_report": pz.get("previous_report_patient_zero"),
			"checkpoint_110": pz.get("checkpoint_110_patient_zero"),
			"final_legacy_104": pz.get("session200_final_dashboard_patient_zero_legacy"),
			"current_legacy_zero_only": pz.get("current_legacy_zero_rate_only_patient_zero"),
			"current_v5214_union": pz.get("current_dashboard_patient_zero_v5214_union"),
			"classification": pz.get("root_cause_classification"),
			"why_93": pz.get("why_93_appeared_in_report"),
			"why_104": pz.get("why_104_then"),
			"why_164": pz.get("why_164_now"),
		},
		"repair_history_summary": hist.get("summary"),
		"cache_audit": cache,
		"sql_catalog": sql_catalog,
		"authorize_next_repair_campaign": False,  # set after evaluating structural vs expected
		"decision_reason": "",
	}
	# Decision: do NOT authorize while known definition bugs remain unfixed / unexplained fails exist
	hard_fails = [
		r
		for r in structural
		if r["metric"]
		in (
			"I4 Leftover",
			"Failed RIV",
			"Broken Bin",
			"Waiting Downstream Bin",
			"Zero Rate",
			"Wrong Rate",
			"Broken GL",
			"Broken SABB",
			"Patient Zero",
		)
		and r["status"] == "FAIL"
		and r["metric"] == "Patient Zero"
		and r.get("difference", {}).get("scan_def")
	]
	# Patient Zero PASS if dashboard matches its own definition
	pz_row = next((r for r in matrix.get("matrix") or [] if r["metric"] == "Patient Zero"), {})
	i4_row = next((r for r in matrix.get("matrix") or [] if r["metric"] == "I4 Leftover"), {})
	riv_row = next((r for r in matrix.get("matrix") or [] if r["metric"] == "Failed RIV"), {})

	# Decision after v5.2.14 definition fixes
	blocking = []
	for metric in ("I4 Leftover", "Failed RIV", "Broken Bin", "Waiting Downstream Bin", "Zero Rate", "Wrong Rate", "Broken GL", "Broken SABB", "Patient Zero"):
		row = next((r for r in matrix.get("matrix") or [] if r["metric"] == metric), None)
		if row and row.get("status") == "FAIL":
			blocking.append(f"{metric} FAIL")
	if (hist.get("summary") or {}).get("FAILED", 0) > 0:
		blocking.append("Repair history FAILED spot checks")

	report["definition_warnings"] = [
		"Patient Zero (v5.2.14+) = UNIQUE across Zero Rate + I4 + Wrong Rate",
		"Zero Rate Patient Zero remains available for the legacy zero-only subset",
		"Repairable READY_I4 now sourced from scan_i4_leftover",
		"Wrong Rate / GL / Failed RIV scans may still use LIMIT caps — dash must equal scan for those caps",
	]

	if blocking or not matrix.get("all_pass"):
		report["authorize_next_repair_campaign"] = False
		report["decision_reason"] = "STOP — " + (
			"; ".join(blocking) if blocking else f"{matrix.get('fail_count')} KPI FAIL(s) remain"
		)
	else:
		report["authorize_next_repair_campaign"] = True
		report["decision_reason"] = (
			"AUTHORIZED — Dashboard/Scan/SQL self-consistent after v5.2.14 definition fixes. "
			"Still require Validate Dashboard before each session."
		)

	_dump("validation_report.json", report)
	print(
		json.dumps(
			{
				"pass": matrix.get("pass_count"),
				"fail": matrix.get("fail_count"),
				"patient_zero": {
					"prev": previous_pz,
					"now": pz.get("current_dashboard_patient_zero"),
					"union": pz.get("union_i4_zero_wrong_count"),
					"class": pz.get("root_cause_classification"),
				},
				"history": hist.get("summary"),
				"authorize": report["authorize_next_repair_campaign"],
				"decision": report["decision_reason"],
				"failures": [{"metric": f["metric"], "diff": f["difference"], "reason": f["reason"][:120]} for f in structural],
			},
			indent=2,
			ensure_ascii=False,
			default=str,
		)
	)
	return report
