# Copyright (c) 2026, ERPNext Extensions contributors
"""GF01–GF20 — Job Card Golden Rule Audit Status + Operation filters (v5.5.12)."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.erpnext_extensions.report.job_card_golden_rule_audit import (
	job_card_golden_rule_audit as report_mod,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
	ST_BALANCED,
	ST_REVIEW,
	_aggregate_item_rows,
	_item_status_and_reason,
	get_job_card_status_options,
	list_job_cards_in_range,
	run_golden_rule_audit,
)

RANGE = {"from_date": "2026-06-01", "to_date": "2026-06-30"}
JC = "PO-JOB08760"
ITEM = "13200544"
OP_PACK = "1.2 mL  بسته بندی"
WIP_JC = "PO-JOB08588"  # Work In Progress + packaging in range


class TestGoldenRuleAuditFiltersV5512(unittest.TestCase):
	def test_gf01_job_card_status_column(self):
		cols = {c["fieldname"] for c in report_mod.get_columns()}
		self.assertIn("job_card_status", cols)

	def test_gf02_operation_column(self):
		cols = {c["fieldname"] for c in report_mod.get_columns()}
		self.assertIn("operation", cols)

	def test_gf03_status_equals_authoritative(self):
		actual = frappe.db.get_value("Job Card", JC, "status")
		res = run_golden_rule_audit({**RANGE, "job_card": JC, "show_balanced": 1})
		self.assertTrue(res["rows"])
		self.assertTrue(all(r["job_card_status"] == actual for r in res["rows"]))

	def test_gf04_operation_equals_job_card(self):
		actual = frappe.db.get_value("Job Card", JC, "operation") or ""
		res = run_golden_rule_audit({**RANGE, "job_card": JC, "show_balanced": 1})
		self.assertTrue(res["rows"])
		self.assertTrue(all((r.get("operation") or "") == actual for r in res["rows"]))

	def test_gf05_blank_status_all(self):
		opts = get_job_card_status_options()
		self.assertIn("Open", opts)
		self.assertIn("Work In Progress", opts)
		self.assertIn("Submitted", opts)
		self.assertIn("Completed", opts)
		# Submitted is a real status value, not merely docstatus
		self.assertIn("Submitted", opts)
		cards = list_job_cards_in_range(**RANGE)
		statuses = {c.status for c in cards}
		self.assertGreaterEqual(len(statuses), 2)

	def test_gf06_status_filter_only_matching(self):
		cards = list_job_cards_in_range(**RANGE, job_card_status="Work In Progress")
		self.assertTrue(cards)
		self.assertTrue(all(c.status == "Work In Progress" for c in cards))
		res = run_golden_rule_audit({**RANGE, "job_card_status": "Work In Progress", "show_balanced": 1})
		self.assertTrue(res["rows"])
		self.assertTrue(all(r["job_card_status"] == "Work In Progress" for r in res["rows"]))
		self.assertEqual(res["counts"]["total_job_cards"], len(cards))

	def test_gf07_blank_operation_all(self):
		cards = list_job_cards_in_range(**RANGE)
		ops = {c.operation for c in cards if c.operation}
		self.assertGreaterEqual(len(ops), 2)

	def test_gf08_operation_filter_only_matching(self):
		cards = list_job_cards_in_range(**RANGE, operation=OP_PACK)
		self.assertTrue(cards)
		self.assertTrue(all(c.operation == OP_PACK for c in cards))
		res = run_golden_rule_audit({**RANGE, "operation": OP_PACK, "show_balanced": 1})
		self.assertTrue(res["rows"])
		self.assertTrue(all(r["operation"] == OP_PACK for r in res["rows"]))

	def test_gf09_status_and_operation_intersection(self):
		cards = list_job_cards_in_range(
			**RANGE, job_card_status="Work In Progress", operation=OP_PACK
		)
		self.assertTrue(cards)
		self.assertTrue(
			all(c.status == "Work In Progress" and c.operation == OP_PACK for c in cards)
		)
		names = {c.name for c in cards}
		self.assertIn(WIP_JC, names)
		# Completed packaging must not appear
		completed_pack = {
			c.name
			for c in list_job_cards_in_range(**RANGE, job_card_status="Completed", operation=OP_PACK)
		}
		self.assertFalse(names & completed_pack)

	def test_gf10_summary_respects_status(self):
		all_c = run_golden_rule_audit({**RANGE, "show_balanced": 0})
		wip = run_golden_rule_audit(
			{**RANGE, "job_card_status": "Work In Progress", "show_balanced": 0}
		)
		self.assertLess(wip["counts"]["total_job_cards"], all_c["counts"]["total_job_cards"])
		self.assertEqual(
			wip["counts"]["total_job_cards"],
			wip["counts"]["balanced_job_cards"] + wip["counts"]["review_job_cards"],
		)
		self.assertTrue(
			all(r["job_card_status"] == "Work In Progress" for r in wip["rows"])
		)

	def test_gf11_summary_respects_operation(self):
		all_c = run_golden_rule_audit({**RANGE, "show_balanced": 0})
		pack = run_golden_rule_audit({**RANGE, "operation": OP_PACK, "show_balanced": 0})
		self.assertLess(pack["counts"]["total_job_cards"], all_c["counts"]["total_job_cards"])
		self.assertEqual(
			pack["counts"]["total_job_cards"],
			pack["counts"]["balanced_job_cards"] + pack["counts"]["review_job_cards"],
		)

	def test_gf12_summary_respects_intersection(self):
		inter = run_golden_rule_audit(
			{
				**RANGE,
				"job_card_status": "Work In Progress",
				"operation": OP_PACK,
				"show_balanced": 0,
			}
		)
		wip = run_golden_rule_audit(
			{**RANGE, "job_card_status": "Work In Progress", "show_balanced": 0}
		)
		pack = run_golden_rule_audit({**RANGE, "operation": OP_PACK, "show_balanced": 0})
		self.assertLessEqual(inter["counts"]["total_job_cards"], wip["counts"]["total_job_cards"])
		self.assertLessEqual(inter["counts"]["total_job_cards"], pack["counts"]["total_job_cards"])
		self.assertGreater(inter["counts"]["total_job_cards"], 0)

	def test_gf13_null_operation_blank_no_crash(self):
		fake = frappe._dict(
			name="JC-NULL-OP",
			status="Open",
			operation=None,
			work_order="WO-X",
			posting_date="2026-06-15",
			finished_good="FG",
			production_item="FG",
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
				return_value=[fake],
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.scan_golden_rule",
				return_value={
					"rows": [
						{
							"item_code": "X",
							"item_name": "X",
							"batch_no": "B1",
							"issued": 10,
							"returned": 0,
							"consumed": 10,
							"mi_consumed": 0,
							"scrap": 0,
							"remaining_wip": 0,
							"status": "OK",
							"suggested_action": "",
							"confidence": "",
						}
					],
					"manufactures": [],
					"multiple_manufacture": False,
					"stamp_conflict": False,
					"evidence_items": [],
				},
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.discover_material_issues",
				return_value=[],
			),
		):
			res = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		self.assertEqual(res["rows"][0]["operation"], "")

	def test_gf14_null_operation_excluded_when_filter_set(self):
		cards = list_job_cards_in_range(**RANGE, operation=OP_PACK)
		self.assertTrue(all((c.operation or "") == OP_PACK for c in cards))
		self.assertFalse(any(not (c.operation or "").strip() for c in cards))

	def test_gf15_grain_job_card_x_item(self):
		res = run_golden_rule_audit({**RANGE, "job_card": JC, "show_balanced": 1})
		self.assertEqual(res.get("grain"), "Job Card × Item")
		cols = {c["fieldname"] for c in report_mod.get_columns()}
		self.assertNotIn("batch_no", cols)

	def test_gf16_multi_batch_one_row(self):
		agg = _aggregate_item_rows(
			[
				{
					"item_code": "ITEM-X",
					"item_name": "X",
					"batch_no": "A",
					"issued": 100,
					"returned": 0,
					"consumed": 90,
					"mi_consumed": 0,
					"scrap": 0,
					"status": "OK",
				},
				{
					"item_code": "ITEM-X",
					"item_name": "X",
					"batch_no": "B",
					"issued": 0,
					"returned": 10,
					"consumed": 0,
					"mi_consumed": 0,
					"scrap": 0,
					"status": "OK",
				},
			]
		)
		self.assertEqual(len(agg), 1)
		self.assertAlmostEqual(flt(agg[0]["remaining_wip"]), 0)

	def test_gf17_batch_mismatch_not_review(self):
		agg = _aggregate_item_rows(
			[
				{
					"item_code": "ITEM-X",
					"issued": 100,
					"returned": 0,
					"consumed": 90,
					"mi_consumed": 0,
					"scrap": 0,
					"status": "MISSING RETURN / UNRESOLVED WIP",
				},
				{
					"item_code": "ITEM-X",
					"issued": 0,
					"returned": 10,
					"consumed": 0,
					"mi_consumed": 0,
					"scrap": 0,
					"status": "OK",
				},
			]
		)[0]
		st, reason = _item_status_and_reason(agg, multi_mfg=False, mi_label="", blocked=False)
		self.assertEqual(st, ST_BALANCED)
		self.assertNotEqual(reason, "BATCH_MISMATCH")

	def test_gf18_canary_08760(self):
		res = run_golden_rule_audit(
			{**RANGE, "job_card": JC, "show_balanced": 1, "item": ITEM}
		)
		rows = [r for r in res["rows"] if r["component_item"] == ITEM]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertAlmostEqual(flt(row["issued_qty"]), 1160)
		self.assertAlmostEqual(flt(row["returned_qty"]), 12)
		self.assertAlmostEqual(flt(row["manufacture_consumed_qty"]), 1148)
		self.assertAlmostEqual(flt(row["remaining_wip"]), 0)
		self.assertEqual(row["golden_status"], ST_BALANCED)
		self.assertEqual(row["job_card_status"], "Completed")
		self.assertEqual(row["operation"], OP_PACK)

	def test_gf19_read_only(self):
		before = {
			"se": frappe.db.sql("select count(*) from `tabStock Entry` where docstatus=1")[0][0],
			"jc": frappe.db.sql("select count(*) from `tabJob Card`")[0][0],
			"ws": frappe.db.sql("select count(*) from `tabWorkstation`")[0][0],
			"ws_mod": frappe.db.sql(
				"select max(modified) from `tabWorkstation`"
			)[0][0],
		}
		run_golden_rule_audit(
			{**RANGE, "job_card_status": "Completed", "operation": OP_PACK, "show_balanced": 0}
		)
		after = {
			"se": frappe.db.sql("select count(*) from `tabStock Entry` where docstatus=1")[0][0],
			"jc": frappe.db.sql("select count(*) from `tabJob Card`")[0][0],
			"ws": frappe.db.sql("select count(*) from `tabWorkstation`")[0][0],
			"ws_mod": frappe.db.sql(
				"select max(modified) from `tabWorkstation`"
			)[0][0],
		}
		self.assertEqual(before, after)

	def test_gf20_no_n1_bulk_fetch(self):
		# list_job_cards_in_range is one SQL; timing smoke for filtered range
		t0 = time.perf_counter()
		cards = list_job_cards_in_range(
			**RANGE, job_card_status="Work In Progress", operation=OP_PACK
		)
		elapsed = time.perf_counter() - t0
		self.assertTrue(cards)
		self.assertLess(elapsed, 2.0)
		# Ensure operation column present on bulk rows (no per-row follow-up needed)
		self.assertTrue(all(hasattr(c, "operation") or "operation" in c for c in cards))


def run():
	loader = unittest.TestLoader()
	suite = loader.loadTestsFromTestCase(TestGoldenRuleAuditFiltersV5512)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	details = []
	for test, err in list(result.failures) + list(result.errors):
		details.append({"test": str(test), "error": err[:2000]})
	return {
		"ok": result.wasSuccessful(),
		"testsRun": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"details": details,
	}
