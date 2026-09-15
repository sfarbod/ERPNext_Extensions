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
	wrong = _mark("wrong_rate", lambda: scan_wrong_rates(company=company, limit=2000))
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
	zero_n = zero.get("count") or 0
	wrong_n = wrong.get("count") or 0
	posting_n = len(posting.get("rows") or [])
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
	# Phase 2 maturity KPIs
	wr_ready = sum(
		1
		for r in (wrong.get("rows") or [])
		if str(r.get("planner_status") or "") == "READY_WRONG_RATE" and (r.get("sql_updates") or 0) > 0
	)
	wr_waiting = sum(
		1
		for r in (wrong.get("rows") or [])
		if str(r.get("planner_status") or "")
		in ("WAITING_PATIENT_ZERO", "WAITING_RATE_DEPENDENCY", "WAITING_RATE_REPAIR")
	)
	wr_manual = sum(
		1
		for r in (wrong.get("rows") or [])
		if str(r.get("planner_status") or "") in ("RATE_MANUAL", "RATE_AMBIGUOUS", "MANUAL", "AMBIGUOUS")
	)
	riv_by = riv.get("by_status") or {}
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
	)
	# Waiting downstream bin is not scored as harshly as a true Broken Bin regression.
	penalty = (
		posting_n * 0.25
		+ zero_n * 0.5
		+ wrong_n
		+ sabb_n
		+ bin_n * 1.0
		+ bin_waiting * 0.15
		+ gl_n * 1.5
		+ riv_n * 1.5
		+ i4_n * 0.75
	)
	from math import log10

	integrity_score = max(0, min(100, round(100 - 18 * log10(1 + penalty))))
	i4_by = i4.get("by_status") or {}
	dashboard = {
		"Integrity Score": integrity_score,
		"Posting Order": posting_n,
		"Wrong Rate": wrong_n,
		"Zero Rate": zero_n,
		"Wrong Amount": (wrong.get("by_flag") or {}).get("WRONG_AMOUNT", 0),
		"Wrong Valuation": (wrong.get("by_flag") or {}).get("WRONG_VALUATION_RATE", 0),
		"Wrong Incoming": (wrong.get("by_flag") or {}).get("WRONG_INCOMING_RATE", 0),
		"Wrong Outgoing": (wrong.get("by_flag") or {}).get("WRONG_OUTGOING_RATE", 0),
		"Wrong Average": (wrong.get("by_flag") or {}).get("WRONG_AVG_RATE", 0),
		"I4 Leftover": i4_n,
		"Broken SABB": sabb_n,
		"Broken Bin": bin_n,
		"Waiting Downstream Bin": bin_waiting,
		"Broken GL": gl_n,
		"Failed RIV": riv_n,
		"Patient Zero": len(patients),
		"Zero Rate Patient Zero": len(patients_by_topic["ZERO_RATE"]),
		"READY_I4": ready_i4,
		"WAITING_I4": int(i4_by.get("WAITING_I4") or i4_by.get("I4_WAITING") or 0),
		"MANUAL_I4": int(i4_by.get("MANUAL") or 0),
		"REPLAY_REQUIRED_I4": int(i4_by.get("I4_REPLAY_REQUIRED") or 0),
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
	}
	return {
		"start_local": start.isoformat(timespec="seconds"),
		"end_local": end.isoformat(timespec="seconds"),
		"duration_s": round(perf_counter() - t0, 3),
		"timing": timing,
		"posting_order": {
			"summary": posting.get("summary"),
			# Actionable defects only — NO_REPAIR_NEEDED is healthy same-time noise.
			"count": len(
				[
					r
					for r in (posting.get("rows") or [])
					if (r.get("optimizer_status") or r.get("status")) not in ("NO_REPAIR_NEEDED",)
					and (r.get("planner_status") or "") not in ("NO_REPAIR_PATH",)
				]
			),
			"raw_row_count": len(posting.get("rows") or []),
			"sle_scanned": posting.get("sle_scanned"),
		},
		"zero_rate": {
			"count": zero.get("count"),
			"by_class": zero.get("by_class"),
			"by_confidence": zero.get("by_confidence"),
			"by_status": zero.get("by_status"),
			"exact": exact,
			"likely": likely,
			"ambiguous": ambiguous,
		},
		"wrong_rate": {
			"count": wrong.get("count"),
			"by_flag": wrong.get("by_flag"),
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
		"failed_riv": {"count": riv.get("count"), "by_status": riv.get("by_status")},
		"i4": {"count": i4.get("count"), "by_status": i4_by, "ready": ready_i4},
		"patient_zero_vouchers": patients,
		"patient_zero_count": len(patients),
		"patient_zero_by_topic": {k: len(v) for k, v in patients_by_topic.items()},
		"dashboard": dashboard,
	}


def _broken_sabb() -> int:
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
					WHERE ABS(IFNULL(sabb.avg_rate,0)) > 0.0001
					  AND ABS(
					        IFNULL(sabb.avg_rate,0)
					        - IFNULL(IF(sle.actual_qty<0, sle.outgoing_rate, sle.incoming_rate),0)
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

