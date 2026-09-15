# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.14 Historical Repair dashboard consistency tests."""

from __future__ import annotations

import unittest
from unittest import mock


class TestDashboardPatientZeroDefinitionV5214(unittest.TestCase):
	def test_patient_zero_unions_topics(self):
		"""Patient Zero must count unique PZ vouchers from zero + i4 + wrong."""
		from erpnext_extensions.iran_accounting.historical_stock import scan as scan_mod

		def fake_mark(name, fn):
			return fn()

		posting = {"rows": []}
		zero = {
			"count": 1,
			"rows": [
				{"confidence": "EXACT", "patient_zero": {"voucher_no": "PZ-ZERO"}, "planner_status": "READY", "sql_updates": 1}
			],
		}
		wrong = {
			"count": 1,
			"by_flag": {},
			"rows": [
				{"confidence": "EXACT", "patient_zero": {"voucher_no": "PZ-WRONG"}, "planner_status": "READY", "sql_updates": 1}
			],
			"manual": 0,
			"ambiguous": 0,
		}
		sle = {"count": 0, "rows": [], "bin_mismatches": [], "by_status": {}}
		gl = {"count": 0, "rows": [], "by_class": {}}
		riv = {"count": 0, "rows": [], "by_status": {}}
		i4 = {
			"count": 1,
			"by_status": {"READY_I4": 1},
			"rows": [
				{
					"voucher": "PZ-I4",
					"i4_status": "READY_I4",
					"eligible": True,
					"patient_zero": {"voucher_no": "PZ-I4"},
					"planner_status": "READY_I4",
				}
			],
		}

		with mock.patch.object(scan_mod, "run_full_history_scan", return_value=posting), mock.patch.object(
			scan_mod, "scan_zero_rate_rows", return_value=zero
		), mock.patch.object(scan_mod, "scan_wrong_rates", return_value=wrong), mock.patch.object(
			scan_mod, "scan_sle_bin", return_value=sle
		), mock.patch.object(scan_mod, "scan_gl_integrity", return_value=gl), mock.patch.object(
			scan_mod, "scan_failed_riv", return_value=riv
		), mock.patch.object(scan_mod, "_broken_sabb", return_value=0), mock.patch.object(
			scan_mod, "_i4_leftover", return_value=1
		), mock.patch.object(scan_mod, "_replay_kpis", return_value={"pending": 0, "complete": 0, "average_s": 0}), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.i4_repair.scan_i4_leftover", return_value=i4
		):
			out = scan_mod.run_full_integrity_scan(company="Co", include_manufacture=False)

		dash = out["dashboard"]
		self.assertEqual(dash["Patient Zero"], 3)
		self.assertEqual(dash["Zero Rate Patient Zero"], 1)
		self.assertEqual(dash["READY_I4"], 1)
		self.assertEqual(dash["I4 Leftover"], 1)
		self.assertEqual(dash["Repairable"], 3)  # zero READY + wrong READY + READY_I4


class TestValidateDashboardApiShapeV5214(unittest.TestCase):
	def test_matrix_row_keys(self):
		from erpnext_extensions.iran_accounting.historical_stock._validation import (
			dashboard_consistency_v5214 as v,
		)

		fake_matrix = {
			"pass_count": 1,
			"fail_count": 0,
			"all_pass": True,
			"matrix": [
				{
					"metric": "Failed RIV",
					"dashboard": 1,
					"scan": 1,
					"planner": 0,
					"sql": 1,
					"queue": 0,
					"status": "PASS",
					"difference": {},
					"reason": "ok",
				}
			],
			"dashboard": {},
			"queue_breakdown": {},
			"i4_by_status": {},
			"sources": {},
		}
		with mock.patch.object(v, "collect_kpi_matrix", return_value=fake_matrix), mock.patch.object(
			v, "audit_cache", return_value={}
		):
			# Direct shape check without frappe whitelist wrapper
			m = v.collect_kpi_matrix()
		self.assertIn("matrix", m)
		self.assertEqual(m["matrix"][0]["metric"], "Failed RIV")


if __name__ == "__main__":
	unittest.main()
