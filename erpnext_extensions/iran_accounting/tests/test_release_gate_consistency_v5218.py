# Copyright (c) 2026, ERPNext Extensions contributors
"""Release gate — Scan All ↔ topic scan count consistency (read-only, v5.2.18)."""

from __future__ import annotations

import time
import unittest

import frappe

COMPANY = "اسپاد فارمد دارو"


class TestScanAllTopicConsistencyV5218(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_dashboard_matches_topic_scans_same_company(self):
		from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
		from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
		from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

		t0 = time.perf_counter()
		full = run_full_integrity_scan(company=COMPANY, include_manufacture=False)
		scan_all_s = time.perf_counter() - t0
		dash = full["dashboard"]
		exp = full["topic_expectations"]

		# Topic scans with identical company filter (no extra filters)
		t1 = time.perf_counter()
		posting = run_full_history_scan(company=COMPANY)
		posting_s = time.perf_counter() - t1
		posting_n = sum(
			1
			for r in (posting.get("rows") or [])
			if str(r.get("optimizer_status") or r.get("status") or "") != "NO_REPAIR_NEEDED"
		)

		wrong = scan_wrong_rates(company=COMPANY, limit=4000)
		zero = scan_zero_rate_rows(company=COMPANY)
		sle = scan_sle_bin(company=COMPANY, limit=2000)
		gl = scan_gl_integrity(company=COMPANY, limit=500)
		riv = scan_failed_riv(limit=2000)

		checks = [
			("posting", dash["Posting Order"], exp["posting"]["count"], posting_n),
			("wrong", dash["Wrong Rate"], exp["wrong"]["count"], wrong.get("count")),
			("zero", dash["Zero Rate"], exp["zero"]["count"], zero.get("count")),
			("gl", dash["Broken GL"], exp["gl"]["count"], gl.get("count")),
			("riv", dash["Failed RIV"], exp["riv"]["count"], riv.get("count")),
			("sle", exp["sle"]["count"], exp["sle"]["count"], sle.get("count")),
		]
		for name, a, b, c in checks:
			self.assertEqual(a, b, f"{name}: dashboard/expectation mismatch {a} vs {b}")
			self.assertEqual(b, c, f"{name}: expectation vs topic scan mismatch {b} vs {c}")

		# Performance envelopes (dev host; soft — fail only on pathological slowdown)
		self.assertLess(scan_all_s, 120, f"Scan All too slow: {scan_all_s:.1f}s")
		self.assertLess(posting_s, 60, f"Posting scan too slow: {posting_s:.1f}s")

	def test_validate_dashboard_all_pass(self):
		from erpnext_extensions.iran_accounting.historical_stock._validation.dashboard_consistency_v5214 import (
			collect_kpi_matrix,
		)

		t0 = time.perf_counter()
		matrix = collect_kpi_matrix(company=COMPANY)
		elapsed = time.perf_counter() - t0
		self.assertTrue(matrix.get("all_pass"), f"fail_count={matrix.get('fail_count')} matrix={matrix.get('matrix')}")
		self.assertLess(elapsed, 180, f"Validate Dashboard too slow: {elapsed:.1f}s")

	def test_unload_contract_strings_in_js(self):
		from pathlib import Path

		js = (
			Path(__file__).resolve().parents[2]
			/ "erpnext_extensions"
			/ "page"
			/ "historical_repair"
			/ "historical_repair.js"
		).read_text(encoding="utf-8")
		self.assertIn("Rows are not loaded yet.", js)
		self.assertIn("Click Scan to load this topic.", js)


if __name__ == "__main__":
	unittest.main()
