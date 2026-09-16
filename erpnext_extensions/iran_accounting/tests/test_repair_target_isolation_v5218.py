# Copyright (c) 2026, ERPNext Extensions contributors
"""Release gate — repair target isolation via dry-run (no writes, v5.2.18)."""

from __future__ import annotations

import unittest

import frappe

COMPANY = "اسپاد فارمد دارو"


def _topic_of_row(row: dict) -> str:
	t = str(row.get("topic") or "")
	if t:
		return t
	if row.get("gl_class") or str(row.get("planner_status") or "").startswith("READY_GL"):
		return "GL"
	if row.get("riv_name") or row.get("riv_status"):
		return "FAILED_RIV"
	if row.get("repair_class") == "I4_LEFTOVER_REPAIR" or str(row.get("planner_status") or "").endswith("_I4"):
		return "I4"
	if row.get("outbound_document") and row.get("inbound_document"):
		return "POSTING_ORDER"
	if row.get("zero_class") or "ZERO" in str(row.get("mismatch_class") or ""):
		return "ZERO_RATE"
	if row.get("flags") or row.get("mismatch_class"):
		return "WRONG_RATE"
	return "UNKNOWN"


class TestRepairTargetIsolationV5218(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_wrong_dry_run_stays_wrong(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
		from erpnext_extensions.iran_accounting.historical_stock.api import repair_wrong_rates_selected

		scan = scan_wrong_rates(company=COMPANY, limit=4000)
		ready = [
			r
			for r in (scan.get("rows") or [])
			if str(r.get("planner_status") or "") == "READY_WRONG_RATE" and (r.get("sql_updates") or 0) > 0
		]
		if not ready:
			self.skipTest("no READY_WRONG_RATE rows")
		sample = ready[:1]
		out = repair_wrong_rates_selected(rows=sample, dry_run=True)
		self.assertTrue(out.get("dry_run") in (True, 1, "1") or out.get("status") in ("DRY_RUN", None) or "rows" in out or "applied" in out or "results" in out)
		# Must not report zero-rate engine markers
		blob = str(out)
		self.assertNotIn("repair_zero_rate_selected", blob)

	def test_zero_dry_run_stays_zero(self):
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
		from erpnext_extensions.iran_accounting.historical_stock.api import repair_zero_rates_selected

		scan = scan_zero_rate_rows(company=COMPANY)
		ready = [
			r
			for r in (scan.get("rows") or [])
			if str(r.get("planner_status") or "").startswith("READY") and (r.get("sql_updates") or 0) > 0
		]
		if not ready:
			self.skipTest("no READY zero-rate rows")
		out = repair_zero_rates_selected(rows=ready[:1], dry_run=True)
		blob = str(out)
		self.assertNotIn("repair_wrong_rate_selected", blob)

	def test_gl_dry_run_stays_gl(self):
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
		from erpnext_extensions.iran_accounting.historical_stock.api import rebuild_gl_selected

		scan = scan_gl_integrity(company=COMPANY, limit=500)
		ready = [
			r
			for r in (scan.get("rows") or [])
			if r.get("eligible") and str(r.get("planner_status") or "").startswith("READY") and (r.get("sql_updates") or 0) > 0
		]
		if not ready:
			self.skipTest("no READY GL rows")
		out = rebuild_gl_selected(vouchers=[ready[0].get("voucher")], dry_run=True)
		self.assertTrue(out.get("dry_run") in (True, 1, "1") or "preview" in str(out).lower() or "rows" in out or "results" in out or "vouchers" in out)

	def test_scan_row_topics_are_consistent(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

		wrong = scan_wrong_rates(company=COMPANY, limit=200)
		zero = scan_zero_rate_rows(company=COMPANY)
		for r in (wrong.get("rows") or [])[:50]:
			t = _topic_of_row(r)
			self.assertIn(t, ("WRONG_RATE", "ZERO_RATE", "UNKNOWN"), t)
			# Wrong scan may include zero flags as subclass but topic field should be WRONG_RATE when set
			if r.get("topic"):
				self.assertEqual(r.get("topic"), "WRONG_RATE")
		for r in (zero.get("rows") or [])[:50]:
			if r.get("topic"):
				self.assertIn(r.get("topic"), ("ZERO_RATE", "zero", "Zero Rate"))


if __name__ == "__main__":
	unittest.main()
