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
	end = datetime.now()
	exact = sum(1 for r in zero.get("rows") or [] if r.get("confidence") == "EXACT")
	likely = sum(1 for r in zero.get("rows") or [] if r.get("confidence") == "LIKELY")
	ambiguous = sum(1 for r in zero.get("rows") or [] if r.get("confidence") == "AMBIGUOUS")
	patients = {}
	for r in zero.get("rows") or []:
		pz = r.get("patient_zero") or {}
		key = pz.get("voucher_no")
		if key:
			patients[key] = patients.get(key, 0) + 1
	replay = _replay_kpis()
	zero_n = zero.get("count") or 0
	wrong_n = wrong.get("count") or 0
	posting_n = len(posting.get("rows") or [])
	gl_n = gl.get("count") or 0
	bin_n = len(sle.get("bin_mismatches") or [])
	riv_n = riv.get("count") or 0
	sabb_n = _broken_sabb()
	repairable = (wrong.get("repairable") or 0) + exact
	penalty = posting_n * 0.25 + zero_n * 0.5 + wrong_n + sabb_n + bin_n + gl_n * 1.5 + riv_n * 1.5
	from math import log10

	integrity_score = max(0, min(100, round(100 - 18 * log10(1 + penalty))))
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
		"Broken SABB": sabb_n,
		"Broken Bin": bin_n,
		"Broken GL": gl_n,
		"Failed RIV": riv_n,
		"Patient Zero": len(patients),
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
			"count": len(posting.get("rows") or []),
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
		"patient_zero_vouchers": patients,
		"patient_zero_count": len(patients),
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

