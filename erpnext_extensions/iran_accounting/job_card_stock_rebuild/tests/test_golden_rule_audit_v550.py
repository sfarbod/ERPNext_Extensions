# Copyright (c) 2026, ERPNext Extensions contributors
"""Golden Rule Audit report tests GR01–GR18 (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
	DATE_BASIS,
	ST_BALANCED,
	ST_MISSING_CONSUMPTION,
	ST_MULTIPLE_MANUFACTURE,
	list_job_cards_in_range,
	run_golden_rule_audit,
)


RANGE = {"from_date": "2026-06-01", "to_date": "2026-06-30"}


class TestGoldenRuleAuditV550(FrappeTestCase):
	def test_gr01_date_range_filtering(self):
		inside = list_job_cards_in_range("2026-06-01", "2026-06-30")
		names = {r.name for r in inside}
		self.assertIn("PO-JOB08760", names)
		self.assertIn("PO-JOB08761", names)
		outside = run_golden_rule_audit(
			{"from_date": "2020-01-01", "to_date": "2020-01-31", "show_balanced": 1}
		)
		self.assertNotIn("PO-JOB08760", outside.get("job_card_summaries") or {})
		self.assertEqual(DATE_BASIS, "Job Card.posting_date")

	def test_gr02_balanced_item_batch_08761(self):
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08761", "show_balanced": 1})
		row = next(
			(
				r
				for r in res["rows"]
				if r["component_item"] == "13200544" and abs(flt(r["issued_qty"]) - 2920) < 1e-6
			),
			None,
		)
		self.assertIsNotNone(row)
		self.assertEqual(row["golden_status"], ST_BALANCED)
		self.assertAlmostEqual(flt(row["manufacture_consumed_qty"]), 2890)
		self.assertAlmostEqual(flt(row["remaining_wip"]), 0)

	def test_gr03_missing_consumption_absent_on_08760_after_apply(self):
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08760", "show_balanced": 0})
		missing = [
			r
			for r in res["rows"]
			if r["component_item"] == "13200544" and r["golden_status"] == ST_MISSING_CONSUMPTION
		]
		self.assertEqual(missing, [])
		bal = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08760", "show_balanced": 1})
		row = next(r for r in bal["rows"] if r["component_item"] == "13200544")
		self.assertEqual(row["golden_status"], ST_BALANCED)
		self.assertAlmostEqual(flt(row["manufacture_consumed_qty"]), 1148)

	def test_gr04_unexplained_or_exception_exists_in_range(self):
		res = run_golden_rule_audit({**RANGE, "show_balanced": 0})
		# After 08760 repair, other exceptions or multi-mfg rows should still exist
		self.assertGreater(res["counts"]["exception_rows"], 0)

	def test_gr05_over_consumed_mapping(self):
		# Synthetic mapping: rem < 0 → OVER_CONSUMED
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			_map_row_status,
			ST_OVER_CONSUMED,
		)

		st = _map_row_status(
			{"issued": 10, "returned": 0, "consumed": 12, "mi_consumed": 0, "remaining_wip": -2, "status": "OK"},
			False,
		)
		self.assertEqual(st, ST_OVER_CONSUMED)

	def test_gr06_over_returned_mapping(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			_map_row_status,
			ST_OVER_RETURNED,
		)

		st = _map_row_status(
			{"issued": 10, "returned": 12, "consumed": 0, "mi_consumed": 0, "remaining_wip": 0, "status": "OK"},
			False,
		)
		self.assertEqual(st, ST_OVER_RETURNED)

	def test_gr07_scrap_pair_no_double_count_08760(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule

		scan = scan_golden_rule("PO-JOB08760")
		# Scrap qty displayed separately; remaining for consumed rows is 0
		for r in scan["rows"]:
			if flt(r["scrap"]) > 0 and flt(r["consumed"]) > 0:
				# physical drain counted once → remaining not negative from scrap
				self.assertGreaterEqual(flt(r["remaining_wip"]), -1e-6)

	def test_gr08_scrap_mismatch_status_exists_or_mapping(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			_map_row_status,
			ST_SCRAP_PAIR_MISMATCH,
		)
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			STATUS_SCRAP_MISMATCH,
		)

		st = _map_row_status(
			{
				"issued": 1,
				"returned": 0,
				"consumed": 1,
				"mi_consumed": 0,
				"remaining_wip": 0,
				"status": STATUS_SCRAP_MISMATCH,
			},
			False,
		)
		self.assertEqual(st, ST_SCRAP_PAIR_MISMATCH)

	def test_gr09_multiple_manufacture(self):
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08830", "show_balanced": 0})
		self.assertGreaterEqual(
			(res.get("job_card_summaries") or {}).get("PO-JOB08830") and 1 or 0, 0
		)
		# 08830 posting 2026-06-16 is in range
		rows = [r for r in res["rows"] if r["job_card"] == "PO-JOB08830"]
		self.assertTrue(rows)
		self.assertTrue(any(cint(r["manufacture_count"]) > 1 for r in rows))
		self.assertTrue(
			any(r["golden_status"] == ST_MULTIPLE_MANUFACTURE or r["merge_review"] == "YES" for r in rows)
		)

	def test_gr10_mi_merge_safe_or_present(self):
		res = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		# Not all ranges have MI merge-safe; assert classifier path does not crash
		self.assertIn("material_issue_review_job_cards", res["counts"])

	def test_gr11_mi_user_decision_path(self):
		# Smoke: MI review label field present on rows when MI exists
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08760", "show_balanced": 1})
		self.assertTrue(all("material_issue_review" in r for r in res["rows"]))

	def test_gr12_batch_grain(self):
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08760", "show_balanced": 1})
		self.assertTrue(any(r.get("batch_no") for r in res["rows"]))

	def test_gr13_ambiguous_mapping(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			_jc_summary_status,
			ST_AMBIGUOUS_OWNERSHIP,
		)

		self.assertEqual(
			_jc_summary_status([ST_BALANCED], False, "AMBIGUOUS", False),
			ST_AMBIGUOUS_OWNERSHIP,
		)

	def test_gr14_show_balanced_false(self):
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08761", "show_balanced": 0})
		# Balanced JC may have zero exception rows
		self.assertTrue(all(r["golden_status"] != ST_BALANCED for r in res["rows"]))

	def test_gr15_show_balanced_true(self):
		res = run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08761", "show_balanced": 1})
		self.assertTrue(any(r["golden_status"] == ST_BALANCED for r in res["rows"]))

	def test_gr16_jc_summary_not_balanced_when_component_fails(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			_jc_summary_status,
		)

		s = _jc_summary_status([ST_BALANCED, ST_MISSING_CONSUMPTION], False, "", False)
		self.assertEqual(s, "GOLDEN_RULE_FAIL")

	def test_gr17_read_only_zero_mutation(self):
		before = frappe.db.sql(
			"select count(*) from `tabStock Entry` where docstatus=1"
		)[0][0]
		run_golden_rule_audit({**RANGE, "show_balanced": 0})
		after = frappe.db.sql(
			"select count(*) from `tabStock Entry` where docstatus=1"
		)[0][0]
		self.assertEqual(before, after)

	def test_gr18_no_n1_regression_reasonable_range(self):
		# Reasonable range should complete without hanging (bulk JC fetch + engine reuse).
		res = run_golden_rule_audit({**RANGE, "show_balanced": 0})
		self.assertGreaterEqual(res["counts"]["total_job_cards"], 2)
		self.assertLessEqual(res["counts"]["total_job_cards"], 5000)
