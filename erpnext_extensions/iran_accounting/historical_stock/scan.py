# Copyright (c) 2026, ERPNext Extensions contributors
"""Unified full-history scan (read-only)."""

from __future__ import annotations

from datetime import datetime
from time import perf_counter

from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
from erpnext_extensions.iran_accounting.historical_stock.manufacture import scan_manufacture_anomalies
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
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
	}
