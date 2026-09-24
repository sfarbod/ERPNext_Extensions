# Copyright (c) 2026, ERPNext Extensions contributors
"""Unified full-history scan (read-only)."""

from __future__ import annotations

from datetime import datetime
from time import perf_counter

from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
from erpnext_extensions.iran_accounting.historical_stock.manufacture import scan_manufacture_anomalies
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan


def run_full_integrity_scan(company=None, include_manufacture=True) -> dict:
	start = datetime.now()
	t0 = perf_counter()
	timing = {}

	def _mark(name, fn):
		a = perf_counter()
		result = fn()
		timing[name] = round(perf_counter() - a, 3)
		return result

	posting = _mark("posting_order", lambda: run_full_history_scan(company=company))
	zero = _mark("zero_rate", lambda: scan_zero_rate_rows(company=company))
	wrong = _mark("wrong_rate", lambda: scan_wrong_rates(company=company, limit=4000))
	mfg = {"count": 0, "rows": [], "scanned": 0}
	if include_manufacture:
		mfg = _mark("manufacture", lambda: scan_manufacture_anomalies(company=company, limit=5000))
	sle = _mark("sle_bin", lambda: scan_sle_bin(company=company, limit=2000))
	gl = _mark("gl", lambda: scan_gl_integrity(company=company, limit=500))
	riv = _mark("failed_riv", lambda: scan_failed_riv(limit=2000))
	# I4 scan aligns READY_I4 / Repairable / Patient Zero with the I4 topic scanner.
	from frappe.utils import nowdate
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	i4 = _mark(
		"i4",
		lambda: scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(nowdate()), limit=5000),
	)
	# I1 negative incoming rate — Manufacture pool roots that block Failed RIV retry.
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate

	i1 = _mark("i1", lambda: scan_i1_negative_rate(company=company, limit=2000))
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import scan_leftover_ma

	lma = _mark("leftover_ma", lambda: scan_leftover_ma(company=company, limit=2000))
	end = datetime.now()
	exact = sum(1 for r in zero.get("rows") or [] if r.get("confidence") == "EXACT")
	likely = sum(1 for r in zero.get("rows") or [] if r.get("confidence") == "LIKELY")
	ambiguous = sum(1 for r in zero.get("rows") or [] if r.get("confidence") == "AMBIGUOUS")
	# Patient Zero = UNIQUE patient-zero vouchers across Zero Rate + I4 + Wrong Rate.
	patients = {}
	patients_by_topic = {"ZERO_RATE": set(), "I4": set(), "WRONG_RATE": set()}

	def _add_pz(topic, row):
		pz = row.get("patient_zero") or {}
		key = pz.get("voucher_no") if isinstance(pz, dict) else None
		if not key and topic == "I4" and row.get("i4_status") in ("READY_I4", "WAITING_I4", "MANUAL", "I4_REPLAY_REQUIRED"):
			key = row.get("voucher")
		if key:
			patients[key] = patients.get(key, 0) + 1
			patients_by_topic[topic].add(key)

	for r in zero.get("rows") or []:
		_add_pz("ZERO_RATE", r)
	for r in i4.get("rows") or []:
		_add_pz("I4", r)
	for r in wrong.get("rows") or []:
		_add_pz("WRONG_RATE", r)

	replay = _replay_kpis()
	# KPI semantics (v5.3.0 purpose-first): actionable excludes Material Receipt
	# user-review and document-authorised zeros. Scrap warehouse alone is NOT no-action.
	zero_raw_n = int(zero.get("raw_count") or zero.get("count") or 0)
	zero_n = int(zero.get("actionable_count") if zero.get("actionable_count") is not None else zero.get("count") or 0)
	zero_no_action = int(zero.get("no_action_required_count") or 0)
	zero_scrap_legit = 0  # deprecated under purpose-first semantics
	zero_receipt_review = int(zero.get("material_receipt_user_review_count") or 0)
	zero_reconstructable = int(zero.get("reconstructable_count") or 0)
	wrong_raw_n = wrong.get("count") or 0
	# Posting Order KPI excludes optimizer-healthy / no-repair rows.
	posting_n = sum(
		1
		for r in (posting.get("rows") or [])
		if str(r.get("optimizer_status") or r.get("status") or "") != "NO_REPAIR_NEEDED"
	)
	gl_n = gl.get("count") or 0
	bin_rows = sle.get("bin_mismatches") or []
	bin_waiting = sum(1 for b in bin_rows if b.get("status") == "WAITING_DOWNSTREAM_REPAIR")
	bin_n = len(bin_rows) - bin_waiting
	riv_n = riv.get("count") or 0
	sabb_n = _broken_sabb()
	# Prefer classified I4 scan count; fall back to SQL if scan empty.
	i4_n = int(i4.get("count") or 0) or _i4_leftover()

	def _ready_n(result):
		from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

		return sum(
			1
			for r in (result.get("rows") or [])
			if (
				r.get("planner_status") in READY_STATUSES
				or str(r.get("planner_status") or "").startswith("READY")
			)
			and (r.get("sql_updates") or 0) > 0
		)

	ready_i4 = sum(
		1 for r in (i4.get("rows") or []) if r.get("eligible") or r.get("i4_status") == "READY_I4"
	)
	# Phase 2 maturity KPIs — canonical Wrong Rate buckets (exclude COMPLETE from active).
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import count_wrong_rate_buckets, wrong_rate_bucket

	wr_buckets = count_wrong_rate_buckets(wrong.get("rows") or [])
	wr_ready = wr_buckets.get("ready") or 0
	wr_waiting = wr_buckets.get("waiting") or 0
	wr_manual = wr_buckets.get("manual") or 0
	wr_complete = wr_buckets.get("complete") or 0
	# Active Wrong Rate problem count (excludes RATE_REPAIR_COMPLETE / already-valued).
	wrong_n = wr_buckets.get("active") or 0
	# Score units (v5.3.7): compress WAITING symptoms onto unique patient-zero /
	# voucher roots so 200 downstream WAITING rows of one PZ do not each add a
	# full Wrong Rate penalty. READY + MANUAL remain 1:1. Dashboard Wrong Rate
	# stays the raw active count (not gamed).
	def _root_key(row) -> str:
		pz = row.get("patient_zero") or {}
		if isinstance(pz, dict) and pz.get("voucher_no"):
			return f"pz:{pz['voucher_no']}"
		v = row.get("voucher") or row.get("voucher_no")
		return f"v:{v}" if v else f"row:{id(row)}"

	wr_rows = wrong.get("rows") or []
	wr_ready_rows = [r for r in wr_rows if wrong_rate_bucket(r) == "ready"]
	wr_manual_rows = [r for r in wr_rows if wrong_rate_bucket(r) == "manual"]
	wr_waiting_rows = [r for r in wr_rows if wrong_rate_bucket(r) == "waiting"]
	wr_waiting_roots = {_root_key(r) for r in wr_waiting_rows}
	wr_manual_roots = {_root_key(r) for r in wr_manual_rows}
	# READY stays 1:1 (actionable corruption). MANUAL/WAITING compress to unique
	# causal roots so sibling symptoms of one PZ do not each full-penalize.
	wrong_score_units = (
		len(wr_ready_rows) + len(wr_manual_roots) * 0.5 + len(wr_waiting_roots) * 0.35
	)

	# Zero Rate score units: NO_ACTION / legitimate excluded via zero_n=actionable.
	# WAITING_UPSTREAM compresses to unique roots at half the actionable weight.
	zero_rows = zero.get("rows") or []
	zero_waiting_rows = [
		r
		for r in zero_rows
		if r.get("status")
		in (
			"DEPENDENCY_REPAIR_REQUIRED",
			"VALUATION_POISON_DEPENDENCY",
		)
		or "WAITING" in str(r.get("planner_status") or "")
	]
	zero_waiting_roots = {_root_key(r) for r in zero_waiting_rows}
	zero_non_waiting = max(0, zero_n - len(zero_waiting_rows))
	zero_score_units = zero_non_waiting * 0.5 + len(zero_waiting_roots) * 0.25

	riv_by = riv.get("by_status") or {}
	riv_actionable = int(riv.get("actionable_count") or 0)
	if not riv_actionable and riv.get("rows"):
		riv_actionable = sum(
			1
			for r in (riv.get("rows") or [])
			if (r.get("riv_reconcile_status") or "")
			not in ("HISTORICAL_ONLY", "SUPERSEDED_BY_SUCCESSFUL_REPAIR")
		)
	riv_safe = int(riv_by.get("SAFE_TO_RETRY") or 0)
	riv_waiting = sum(
		int(riv_by.get(k) or 0)
		for k in (
			"WAITING_RATE",
			"WAITING_SLE",
			"WAITING_GL",
			"WAITING_PATIENT_ZERO",
			"WAITING_REPLAY",
			"WAITING_FOR_RATE_REPAIR",
			"WAITING_FOR_SLE_REPAIR",
			"WAITING_FOR_GL_REPAIR",
		)
	)
	riv_unsafe = sum(
		int(riv_by.get(k) or 0)
		for k in (
			"NEGATIVE_STOCK",
			"RAW_MATERIAL_COST",
			"VALUATION_INTEGRITY",
			"PERMANENTLY_UNSAFE",
			"UNSAFE",
			"UNKNOWN",
		)
	)
	gl_ready = sum(
		1
		for r in (gl.get("rows") or [])
		if r.get("eligible")
		and str(r.get("planner_status") or "").startswith("READY")
		and (r.get("sql_updates") or 0) > 0
		and not r.get("sle_poisoned")
	)
	gl_waiting = sum(
		1
		for r in (gl.get("rows") or [])
		if "WAITING" in str(r.get("planner_status") or "")
		or r.get("gl_role") in ("WAITING_SLE", "WAITING_RATE", "WAITING_REPLAY")
		or r.get("sle_poisoned")
	)
	gl_manual = sum(
		1
		for r in (gl.get("rows") or [])
		if str(r.get("planner_status") or "") in ("MANUAL", "NO_REPAIR_PATH", "BLOCKED")
		and not r.get("eligible")
	)

	repairable = (
		_ready_n(posting)
		+ _ready_n(zero)
		+ _ready_n(wrong)
		+ _ready_n(mfg)
		+ _ready_n(sle)
		+ _ready_n(gl)
		+ _ready_n(riv)
		+ ready_i4
		+ int(lma.get("repairable") or 0)
	)
	# Waiting downstream bin is not scored as harshly as a true Broken Bin regression.
	# v5.3.7: Wrong/Zero WAITING compressed to unique roots; SABB is derived-state
	# (report bundle) weighted like waiting-bin, not as primary SLE corruption.
	i1_n = int(i1.get("count") or 0)
	penalty = (
		posting_n * 0.25
		+ zero_score_units
		+ wrong_score_units
		+ sabb_n * 0.15
		+ bin_n * 1.0
		+ bin_waiting * 0.15
		+ gl_n * 1.5
		# Score only actionable Failed RIV (not historical/superseded raw count).
		+ riv_actionable * 1.5
		+ i4_n * 0.75
		+ i1_n * 1.25
	)
	from math import log10

	integrity_score = max(0, min(100, round(100 - 18 * log10(1 + penalty))))
	i4_by = i4.get("by_status") or {}
	i1_by = i1.get("by_status") or {}
	ready_i1 = sum(1 for r in (i1.get("rows") or []) if r.get("eligible"))
	dashboard = {
		"Integrity Score": integrity_score,
		"Integrity Score Version": "5.3.7",
		"Wrong Rate Score Units": round(wrong_score_units, 2),
		"Zero Rate Score Units": round(zero_score_units, 2),
		"Wrong Rate Waiting Roots": len(wr_waiting_roots),
		"Zero Rate Waiting Roots": len(zero_waiting_roots),
		"Posting Order": posting_n,
		"Wrong Rate": wrong_n,
		"Wrong Rate Complete": wr_complete,
		"Zero Rate": zero_n,
		"Zero Rate Raw": zero_raw_n,
		"Zero Rate Actionable": zero_n,
		"Zero Rate No Action": zero_no_action,
		"Zero Rate User Review": zero_receipt_review,
		"Zero Rate Reconstructable": zero_reconstructable,
		"Material Receipt Zero User Review": zero_receipt_review,
		"Legitimate Scrap Zero Rate": zero_scrap_legit,
		"Wrong Amount": (wrong.get("by_flag") or {}).get("WRONG_AMOUNT", 0),
		"Wrong Valuation": (wrong.get("by_flag") or {}).get("WRONG_VALUATION_RATE", 0),
		"Wrong Incoming": (wrong.get("by_flag") or {}).get("WRONG_INCOMING_RATE", 0),
		"Wrong Outgoing": (wrong.get("by_flag") or {}).get("WRONG_OUTGOING_RATE", 0),
		"Wrong Average": (wrong.get("by_flag") or {}).get("WRONG_AVG_RATE", 0),
		"Matched But Corrupt": (wrong.get("by_flag") or {}).get("MATCHED_BUT_CORRUPT", 0),
		"I4 Leftover": i4_n,
		"I4 Raw": int(i4.get("raw_count") or i4_n),
		"I4 Root Identities": int(i4.get("root_identity_count") or 0),
		"I1 Negative Rate": i1_n,
		"READY_I1": ready_i1,
		"WAITING_I1": int(i1_by.get("WAITING_I1") or 0),
		"MANUAL_I1": int(i1_by.get("MANUAL_I1") or 0),
		"Broken SABB": sabb_n,
		"Broken Bin": bin_n,
		"Waiting Downstream Bin": bin_waiting,
		"Broken GL": gl_n,
		"Failed RIV": riv_actionable,
		"Failed RIV Raw": riv_n,
		"Failed RIV Actionable": riv_actionable,
		"Failed RIV Historical": int((riv.get("by_reconcile") or {}).get("HISTORICAL_ONLY") or 0)
		or int((riv.get("stage1") or {}).get("historical_only") or 0),
		"Failed RIV Superseded": int((riv.get("by_reconcile") or {}).get("SUPERSEDED_BY_SUCCESSFUL_REPAIR") or 0)
		or int((riv.get("stage1") or {}).get("superseded") or 0),
		"Patient Zero": len(patients),
		"Patient Zero Findings": sum(patients.values()) if patients else 0,
		"Zero Rate Patient Zero": len(patients_by_topic["ZERO_RATE"]),
		"READY_I4": ready_i4,
		"WAITING_I4": int(i4_by.get("WAITING_I4") or i4_by.get("I4_WAITING") or 0),
		"MANUAL_I4": int(i4_by.get("MANUAL") or 0),
		"REPLAY_REQUIRED_I4": int(i4_by.get("I4_REPLAY_REQUIRED") or 0),
		"Leftover MA": int(lma.get("count") or 0),
		"READY_LEFTOVER_MA": int(lma.get("ready_count") or 0),
		"MANUAL_LEFTOVER_MA": int(lma.get("manual_count") or 0),
		"Proven Legitimate Zero": int((lma.get("by_status") or {}).get("NO_ACTION") or 0)
		+ int(zero_no_action or 0),
		"Wrong Rate READY": wr_ready,
		"Wrong Rate WAITING": wr_waiting,
		"Wrong Rate MANUAL": wr_manual,
		"RIV SAFE": riv_safe,
		"RIV WAITING": riv_waiting,
		"RIV UNSAFE": riv_unsafe,
		"GL READY": gl_ready,
		"GL WAITING": gl_waiting,
		"GL MANUAL": gl_manual,
		"Repairable": repairable,
		"Manual": (wrong.get("manual") or 0) + likely,
		"Ambiguous": (wrong.get("ambiguous") or 0) + ambiguous,
		"Replay Pending": replay["pending"],
		"Replay Complete": replay["complete"],
		"Average Replay Time": replay["average_s"],
		"Ready to Repair": repairable,
		"Needs Review": (wrong.get("manual") or 0) + likely + int(lma.get("manual_count") or 0),
		"Legitimate / No Action": int((lma.get("by_status") or {}).get("NO_ACTION") or 0)
		+ int(zero_no_action or 0)
		+ int(zero_scrap_legit or 0),
		"Blocked": wr_waiting + int(i4_by.get("WAITING_I4") or i4_by.get("I4_WAITING") or 0) + int(i1_by.get("WAITING_I1") or 0),
		"Failed": riv_actionable,
	}
	return {
		"start_local": start.isoformat(timespec="seconds"),
		"end_local": end.isoformat(timespec="seconds"),
		"duration_s": round(perf_counter() - t0, 3),
		"timing": timing,
		"posting_order": {
			"summary": posting.get("summary"),
			# Actionable defects only — NO_REPAIR_NEEDED is healthy same-time noise.
			# Do not filter on planner NO_REPAIR_PATH: dependency/resolution also uses
			# that label for non-healthy residuals and would under-count shortages.
			"count": len(
				[
					r
					for r in (posting.get("rows") or [])
					if (r.get("optimizer_status") or r.get("status")) not in ("NO_REPAIR_NEEDED",)
				]
			),
			"raw_row_count": len(posting.get("rows") or []),
			"sle_scanned": posting.get("sle_scanned"),
		},
		"zero_rate": {
			"count": zero.get("count"),
			"raw_count": zero_raw_n,
			"actionable_count": zero_n,
			"material_receipt_user_review_count": zero_receipt_review,
			"reconstructable_count": zero_reconstructable,
			"by_class": zero.get("by_class"),
			"by_confidence": zero.get("by_confidence"),
			"by_status": zero.get("by_status"),
			"by_kpi_bucket": zero.get("by_kpi_bucket"),
			"by_purpose": zero.get("by_purpose"),
			"exact": exact,
			"likely": likely,
			"ambiguous": ambiguous,
		},
		"wrong_rate": {
			"count": wrong_n,
			"raw_count": wrong_raw_n,
			"complete": wr_complete,
			"by_flag": wrong.get("by_flag"),
			"by_kpi_bucket": wr_buckets,
			"exact": wrong.get("exact"),
			"likely": wrong.get("likely"),
			"ambiguous": wrong.get("ambiguous"),
			"repairable": wrong.get("repairable"),
		},
		"manufacture": {"count": mfg.get("count"), "scanned": mfg.get("scanned")},
		"sle_bin": {
			"count": sle.get("count"),
			"by_status": sle.get("by_status"),
			"bin_mismatches": len(sle.get("bin_mismatches") or []),
		},
		"gl": {"count": gl.get("count"), "by_class": gl.get("by_class")},
		"failed_riv": {
			"count": riv.get("count"),
			"raw_count": riv.get("raw_count") or riv.get("count"),
			"actionable_count": riv_actionable,
			"by_status": riv.get("by_status"),
			"by_reconcile": riv.get("by_reconcile"),
			"stage1": riv.get("stage1"),
		},
		"i4": {"count": i4.get("count"), "by_status": i4_by, "ready": ready_i4},
		"i1": {"count": i1_n, "by_status": i1_by, "ready": ready_i1},
		"leftover_ma": {
			"count": lma.get("count"),
			"ready": lma.get("ready_count"),
			"manual": lma.get("manual_count"),
			"by_status": lma.get("by_status"),
		},
		"patient_zero_vouchers": patients,
		"patient_zero_count": len(patients),
		"patient_zero_by_topic": {k: len(v) for k, v in patients_by_topic.items()},
		"dashboard": dashboard,
		# Explicit topic ↔ dashboard contract for UI + Validate Dashboard.
		# Grid row counts must match these after an unfiltered topic Scan
		# (same company). Manufacture is optional in Scan All.
		"topic_expectations": {
			"posting": {
				"count": posting_n,
				"kpi": "Posting Order",
				"covered_by_scan_all": True,
			},
			"wrong": {
				"count": wrong_n,
				"kpi": "Wrong Rate",
				"covered_by_scan_all": True,
				"note": "Active only — excludes RATE_REPAIR_COMPLETE / already-valued.",
			},
			"wrong_complete": {
				"count": wr_complete,
				"kpi": "Wrong Rate Complete",
				"covered_by_scan_all": True,
			},
			"zero": {
				"count": zero_n,
				"kpi": "Zero Rate",
				"covered_by_scan_all": True,
			},
			"manufacture": {
				"count": mfg.get("count") if include_manufacture else None,
				"kpi": None,
				"covered_by_scan_all": bool(include_manufacture),
				"note": "Click Scan on Manufacture Valuation — not cached as dashboard KPI rows.",
			},
			"sle": {
				"count": sle.get("count") or 0,
				"kpi": "Broken Bin",
				"covered_by_scan_all": True,
				"note": "Dashboard also splits Broken Bin / Waiting Downstream Bin / I4 / SABB chips.",
			},
			"gl": {
				"count": gl_n,
				"kpi": "Broken GL",
				"covered_by_scan_all": True,
			},
			"i1": {
				"count": i1_n,
				"kpi": "I1 Negative Rate",
				"covered_by_scan_all": True,
				"note": "Manufacture negative incoming rate; READY_I1 is repriceable in-document.",
			},
			"riv": {
				"count": riv_n,
				"kpi": "Failed RIV",
				"covered_by_scan_all": True,
			},
		},
	}


def _broken_sabb() -> int:
	"""Count Serial and Batch Bundle rows that disagree with SLE economics.

	v5.3.7: compare ``sabb.total_amount`` to SLE ``stock_value_difference``
	(currency precision), not ``avg_rate`` vs ``|SVD|/qty`` — fractional qty
	makes unit-rate noise look like thousands of false Broken SABB rows.
	Caps at 500 for dashboard parity.
	"""
	import frappe

	try:
		return int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM (
					SELECT sabb.name
					FROM `tabSerial and Batch Bundle` sabb
					JOIN `tabStock Ledger Entry` sle
						ON sle.serial_and_batch_bundle=sabb.name AND sle.is_cancelled=0
					WHERE ABS(
					        IFNULL(sabb.total_amount,0)
					        - IFNULL(sle.stock_value_difference,0)
					      ) > 1
					LIMIT 500
				) t
				"""
			)[0][0]
			or 0
		)
	except Exception:
		return 0


def _i4_leftover() -> int:
	import frappe

	try:
		return int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM (
					SELECT name FROM `tabStock Ledger Entry`
					WHERE is_cancelled=0
					  AND ABS(qty_after_transaction) < 0.0001
					  AND ABS(IFNULL(stock_value,0)) > 1
					LIMIT 5000
				) t
				"""
			)[0][0]
			or 0
		)
	except Exception:
		return 0


def _replay_kpis() -> dict:
	import frappe

	if not frappe.db.exists("DocType", "Historical Stock Repair Log"):
		return {"pending": 0, "complete": 0, "average_s": 0}
	pending = frappe.db.count("Historical Stock Repair Log", {"status": "In Progress"})
	complete = frappe.db.count("Historical Stock Repair Log", {"status": "Completed"})
	avg = frappe.db.sql(
		"""
		SELECT AVG(TIMESTAMPDIFF(SECOND, started_on, ended_on))
		FROM `tabHistorical Stock Repair Log`
		WHERE status='Completed' AND started_on IS NOT NULL AND ended_on IS NOT NULL
		"""
	)[0][0]
	return {"pending": pending, "complete": complete, "average_s": round(float(avg or 0), 2)}

