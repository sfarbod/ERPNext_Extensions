# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression — Dashboard KPI ↔ topic_expectations ↔ topic scan contract (v5.2.18)."""

from __future__ import annotations

import unittest
from unittest.mock import patch


def _rows(n, **extra):
	out = []
	for i in range(n):
		row = {"voucher": f"V-{i}", "planner_status": "READY", "sql_updates": 1}
		row.update(extra)
		out.append(row)
	return out


class TestDashboardGridContract(unittest.TestCase):
	def test_topic_expectations_match_dashboard_kpis(self):
		from erpnext_extensions.iran_accounting.historical_stock import scan as scan_mod

		posting = {
			"rows": [
				{"optimizer_status": "CROSS_TIME_REPAIRABLE", "planner_status": "READY", "sql_updates": 1},
				{"optimizer_status": "NO_REPAIR_NEEDED", "planner_status": "NO_REPAIR_PATH", "sql_updates": 0},
			],
			"summary": {},
			"sle_scanned": 2,
		}
		zero = {"rows": _rows(3), "count": 3, "by_class": {}, "by_confidence": {}, "by_status": {}}
		wrong = {
			"rows": _rows(5, planner_status="READY_WRONG_RATE"),
			"count": 5,
			"by_flag": {"WRONG_AMOUNT": 2},
			"exact": 5,
			"likely": 0,
			"ambiguous": 0,
			"repairable": 5,
			"manual": 0,
		}
		sle = {
			"rows": _rows(4),
			"count": 4,
			"by_status": {},
			"bin_mismatches": [{"status": "BROKEN"}, {"status": "WAITING_DOWNSTREAM_REPAIR"}],
		}
		gl = {"rows": _rows(2, eligible=True), "count": 2, "by_class": {}}
		riv = {"rows": _rows(1), "count": 1, "by_status": {"SAFE_TO_RETRY": 1}}
		i4 = {"rows": [], "count": 0, "by_status": {}}

		with (
			patch.object(scan_mod, "run_full_history_scan", return_value=posting),
			patch.object(scan_mod, "scan_zero_rate_rows", return_value=zero),
			patch.object(scan_mod, "scan_wrong_rates", return_value=wrong) as wrong_fn,
			patch.object(scan_mod, "scan_sle_bin", return_value=sle),
			patch.object(scan_mod, "scan_gl_integrity", return_value=gl),
			patch.object(scan_mod, "scan_failed_riv", return_value=riv),
			patch.object(scan_mod, "_broken_sabb", return_value=0),
			patch.object(scan_mod, "_i4_leftover", return_value=0),
			patch.object(scan_mod, "_replay_kpis", return_value={"pending": 0, "complete": 0, "average_s": 0}),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.i4_repair.scan_i4_leftover",
				return_value=i4,
			),
		):
			result = scan_mod.run_full_integrity_scan(company="Co", include_manufacture=False)

		self.assertEqual(wrong_fn.call_args.kwargs.get("limit"), 4000)
		dash = result["dashboard"]
		exp = result["topic_expectations"]
		self.assertEqual(dash["Posting Order"], 1)
		self.assertEqual(exp["posting"]["count"], 1)
		self.assertEqual(dash["Wrong Rate"], 5)
		self.assertEqual(exp["wrong"]["count"], 5)
		self.assertEqual(dash["Zero Rate"], 3)
		self.assertEqual(exp["zero"]["count"], 3)
		self.assertEqual(dash["Broken GL"], 2)
		self.assertEqual(exp["gl"]["count"], 2)
		self.assertEqual(dash["Failed RIV"], 1)
		self.assertEqual(exp["riv"]["count"], 1)
		self.assertFalse(exp["manufacture"]["covered_by_scan_all"])
		self.assertIsNone(exp["manufacture"]["count"])
		self.assertTrue(exp["wrong"]["covered_by_scan_all"])

	def test_kpi_topic_mapping_covers_wrong_vs_zero(self):
		"""Static contract: Wrong Rate KPIs must not resolve to Zero topic."""
		# Mirrors KPI_TOPIC in historical_repair.js — keep in sync.
		kpi_topic = {
			"Wrong Rate": "wrong",
			"Wrong Rate READY": "wrong",
			"Wrong Rate WAITING": "wrong",
			"Wrong Rate MANUAL": "wrong",
			"Wrong Amount": "wrong",
			"Zero Rate": "zero",
			"Posting Order": "posting",
			"Broken GL": "gl",
			"Failed RIV": "riv",
			"Broken Bin": "sle",
		}
		self.assertEqual(kpi_topic["Wrong Rate"], "wrong")
		self.assertEqual(kpi_topic["Zero Rate"], "zero")
		self.assertNotEqual(kpi_topic["Wrong Rate"], "zero")

	def test_scan_job_result_preserves_topic_expectations(self):
		from erpnext_extensions.iran_accounting.historical_stock import scan_job

		store = {}

		class Cache:
			def get_value(self, key):
				return store.get(key)

			def set_value(self, key, value, expires_in_sec=None):
				store[key] = value

			def delete_value(self, key):
				store.pop(key, None)

		with patch("erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe") as frappe:
			frappe.cache.return_value = Cache()
			frappe.session.user = "Administrator"
			frappe.generate_hash.return_value = "TECONTRACT1"
			frappe.enqueue = lambda *a, **k: None
			frappe.get_traceback.return_value = ""
			started = scan_job.start_scan_all_job(company="Co")
			job_id = started["job_id"]
			payload = {
				"dashboard": {"Wrong Rate": 7, "Zero Rate": 2},
				"topic_expectations": {
					"wrong": {"count": 7, "kpi": "Wrong Rate", "covered_by_scan_all": True},
					"zero": {"count": 2, "kpi": "Zero Rate", "covered_by_scan_all": True},
				},
				"timing": {},
			}
			with patch(
				"erpnext_extensions.iran_accounting.historical_stock.scan.run_full_integrity_scan",
				return_value=payload,
			):
				scan_job.run_scan_all_job(company="Co", scan_job_id=job_id, user="Administrator")
			status = scan_job.get_scan_all_job(job_id)
			self.assertEqual(status["status"], "COMPLETED")
			self.assertEqual(status["result"]["topic_expectations"]["wrong"]["count"], 7)
			self.assertEqual(status["result"]["dashboard"]["Wrong Rate"], 7)


if __name__ == "__main__":
	unittest.main()
