# Copyright (c) 2026, ERPNext Extensions contributors
"""Golden Rule Audit GA01–GA12 — Job Card × Item grain (v5.5.10)."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
	REASON_MISSING_CONSUMPTION,
	REASON_OVER_CONSUMED,
	ST_BALANCED,
	ST_REVIEW,
	_aggregate_item_rows,
	_item_status_and_reason,
	run_golden_rule_audit,
)
from erpnext_extensions.erpnext_extensions.report.job_card_golden_rule_audit import (
	job_card_golden_rule_audit as report_mod,
)


RANGE = {"from_date": "2026-06-01", "to_date": "2026-06-30"}


def _batch_rows_cancelling():
	"""Same JC item across 2 batches; batch remainders +10 / -10 → item rem 0."""
	return [
		{
			"item_code": "ITEM-X",
			"item_name": "Item X",
			"batch_no": "BATCH-A",
			"issued": 100,
			"returned": 0,
			"consumed": 90,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": 10,
			"status": "MISSING RETURN / UNRESOLVED WIP",
			"suggested_action": "",
			"confidence": "",
		},
		{
			"item_code": "ITEM-X",
			"item_name": "Item X",
			"batch_no": "BATCH-B",
			"issued": 0,
			"returned": 10,
			"consumed": 0,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": -10,
			"status": "OK",
			"suggested_action": "",
			"confidence": "",
		},
	]


def _fake_scan(rows, *, multi=False, manufactures=None):
	return {
		"rows": rows,
		"manufactures": manufactures or [{"name": "MFG-1"}],
		"multiple_manufacture": multi,
		"stamp_conflict": False,
		"evidence_items": [],
		"scrap_pairing": {"ok": True, "unpaired_scrap": []},
	}


class TestGoldenRuleAuditV5510(unittest.TestCase):
	def test_ga01_multi_batch_one_row(self):
		agg = _aggregate_item_rows(_batch_rows_cancelling())
		self.assertEqual(len(agg), 1)
		self.assertEqual(agg[0]["item_code"], "ITEM-X")
		self.assertAlmostEqual(flt(agg[0]["issued"]), 100)
		self.assertAlmostEqual(flt(agg[0]["returned"]), 10)
		self.assertAlmostEqual(flt(agg[0]["consumed"]), 90)
		self.assertAlmostEqual(flt(agg[0]["remaining_wip"]), 0)

	def test_ga02_batch_mismatch_not_review(self):
		agg = _aggregate_item_rows(_batch_rows_cancelling())[0]
		st, reason = _item_status_and_reason(
			agg, multi_mfg=False, mi_label="", blocked=False
		)
		self.assertEqual(st, ST_BALANCED)
		self.assertEqual(reason, "")
		self.assertNotEqual(reason, "BATCH_MISMATCH")

	def test_ga03_item_totals_balance(self):
		agg = {
			"issued": 150,
			"returned": 15,
			"consumed": 135,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": 0,
			"engine_statuses": {"OK"},
		}
		st, reason = _item_status_and_reason(
			agg, multi_mfg=False, mi_label="", blocked=False
		)
		self.assertEqual(st, ST_BALANCED)
		self.assertEqual(reason, "")

	def test_ga04_item_imbalance_review(self):
		agg = {
			"issued": 100,
			"returned": 0,
			"consumed": 80,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": 20,
			"engine_statuses": {"MISSING CONSUMPTION"},
		}
		st, reason = _item_status_and_reason(
			agg, multi_mfg=False, mi_label="", blocked=False
		)
		self.assertEqual(st, ST_REVIEW)
		self.assertEqual(reason, REASON_MISSING_CONSUMPTION)

		agg2 = {
			"issued": 10,
			"returned": 0,
			"consumed": 12,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": -2,
			"engine_statuses": {"OK"},
		}
		st2, reason2 = _item_status_and_reason(
			agg2, multi_mfg=False, mi_label="", blocked=False
		)
		self.assertEqual(st2, ST_REVIEW)
		self.assertEqual(reason2, REASON_OVER_CONSUMED)

	def test_ga05_no_job_card_excluded(self):
		"""Rows without job_card never appear; orphan evidence is not reported."""
		fake_jc = frappe._dict(
			name="JC-SYNTH-GA05",
			status="Open",
			work_order="WO-X",
			posting_date="2026-06-15",
			finished_good="FG",
			production_item="FG",
		)
		with patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
			return_value=[],
		):
			res = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		self.assertEqual(res["rows"], [])
		self.assertEqual(res["counts"]["total_job_cards"], 0)
		# Hard filter: even if a row leaked without JC it is stripped
		self.assertTrue(all((r.get("job_card") or "").strip() for r in res["rows"]))
		# Synthetic: list returns one JC — rows always carry job_card
		with (
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
				return_value=[fake_jc],
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.scan_golden_rule",
				return_value=_fake_scan(_batch_rows_cancelling()),
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.discover_material_issues",
				return_value=[],
			),
		):
			res2 = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		self.assertTrue(res2["rows"])
		self.assertTrue(all(r["job_card"] == "JC-SYNTH-GA05" for r in res2["rows"]))

	def test_ga06_show_balanced_off(self):
		fake_jc = frappe._dict(
			name="JC-SYNTH-GA06",
			status="Open",
			work_order="WO-X",
			posting_date="2026-06-15",
			finished_good="FG",
			production_item="FG",
		)
		rows = _batch_rows_cancelling() + [
			{
				"item_code": "ITEM-BAD",
				"item_name": "Bad",
				"batch_no": "B1",
				"issued": 50,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 50,
				"status": "MISSING CONSUMPTION",
				"suggested_action": "CONSUME",
				"confidence": "HIGH",
			}
		]
		with (
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
				return_value=[fake_jc],
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.scan_golden_rule",
				return_value=_fake_scan(rows),
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.discover_material_issues",
				return_value=[],
			),
		):
			res = run_golden_rule_audit({**RANGE, "show_balanced": 0})
		self.assertTrue(res["rows"])
		self.assertTrue(all(r["golden_status"] == ST_REVIEW for r in res["rows"]))
		self.assertFalse(any(r["golden_status"] == ST_BALANCED for r in res["rows"]))

	def test_ga07_show_balanced_on(self):
		fake_jc = frappe._dict(
			name="JC-SYNTH-GA07",
			status="Open",
			work_order="WO-X",
			posting_date="2026-06-15",
			finished_good="FG",
			production_item="FG",
		)
		rows = _batch_rows_cancelling() + [
			{
				"item_code": "ITEM-BAD",
				"item_name": "Bad",
				"batch_no": "B1",
				"issued": 50,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 50,
				"status": "MISSING CONSUMPTION",
				"suggested_action": "CONSUME",
				"confidence": "HIGH",
			}
		]
		with (
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
				return_value=[fake_jc],
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.scan_golden_rule",
				return_value=_fake_scan(rows),
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.discover_material_issues",
				return_value=[],
			),
		):
			res = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		statuses = {r["golden_status"] for r in res["rows"]}
		self.assertIn(ST_BALANCED, statuses)
		self.assertIn(ST_REVIEW, statuses)

	def test_ga08_jc_rollup_review(self):
		fake_jc = frappe._dict(
			name="JC-SYNTH-GA08",
			status="Open",
			work_order="WO-X",
			posting_date="2026-06-15",
			finished_good="FG",
			production_item="FG",
		)
		rows = _batch_rows_cancelling() + [
			{
				"item_code": "ITEM-BAD",
				"item_name": "Bad",
				"batch_no": "B1",
				"issued": 50,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 50,
				"status": "MISSING CONSUMPTION",
				"suggested_action": "",
				"confidence": "",
			}
		]
		with (
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
				return_value=[fake_jc],
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.scan_golden_rule",
				return_value=_fake_scan(rows),
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.discover_material_issues",
				return_value=[],
			),
		):
			res = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		self.assertEqual(res["job_card_summaries"]["JC-SYNTH-GA08"], ST_REVIEW)
		self.assertEqual(res["counts"]["review_job_cards"], 1)

	def test_ga09_jc_rollup_balanced(self):
		fake_jc = frappe._dict(
			name="JC-SYNTH-GA09",
			status="Open",
			work_order="WO-X",
			posting_date="2026-06-15",
			finished_good="FG",
			production_item="FG",
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.list_job_cards_in_range",
				return_value=[fake_jc],
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.scan_golden_rule",
				return_value=_fake_scan(_batch_rows_cancelling()),
			),
			patch(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit.discover_material_issues",
				return_value=[],
			),
		):
			res = run_golden_rule_audit({**RANGE, "show_balanced": 1})
		self.assertEqual(res["job_card_summaries"]["JC-SYNTH-GA09"], ST_BALANCED)
		self.assertEqual(res["counts"]["balanced_job_cards"], 1)

	def test_ga10_component_scrap_not_double_counted(self):
		# Scrap is display-only; remaining = issued - returned - consumed - mi
		agg = _aggregate_item_rows(
			[
				{
					"item_code": "SCRAP-ITEM",
					"item_name": "S",
					"batch_no": "B1",
					"issued": 100,
					"returned": 0,
					"consumed": 95,
					"mi_consumed": 0,
					"scrap": 5,
					"remaining_wip": 5,
					"status": "OK",
					"suggested_action": "",
					"confidence": "",
				}
			]
		)[0]
		# Aggregate recomputes remaining without subtracting scrap again
		self.assertAlmostEqual(flt(agg["scrap"]), 5)
		self.assertAlmostEqual(flt(agg["remaining_wip"]), 5)  # 100-0-95-0
		# If consume already includes physical scrap path at engine, rem would be 0:
		agg2 = _aggregate_item_rows(
			[
				{
					"item_code": "SCRAP-ITEM",
					"item_name": "S",
					"batch_no": "B1",
					"issued": 100,
					"returned": 0,
					"consumed": 100,
					"mi_consumed": 0,
					"scrap": 5,
					"remaining_wip": 0,
					"status": "OK",
					"suggested_action": "",
					"confidence": "",
				}
			]
		)[0]
		self.assertAlmostEqual(flt(agg2["remaining_wip"]), 0)
		self.assertAlmostEqual(flt(agg2["scrap"]), 5)
		# Real canary: paired scrap on 08760 must not make remaining negative
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		scan = scan_golden_rule("PO-JOB08760")
		for r in scan["rows"]:
			if flt(r["scrap"]) > 0:
				self.assertGreaterEqual(flt(r["remaining_wip"]), -1e-6)

	def test_ga11_poj08760_13200544_balanced(self):
		res = run_golden_rule_audit(
			{**RANGE, "job_card": "PO-JOB08760", "show_balanced": 1, "item": "13200544"}
		)
		rows = [r for r in res["rows"] if r["component_item"] == "13200544"]
		self.assertEqual(len(rows), 1, "must be one Job Card × Item row")
		row = rows[0]
		self.assertIsNone(row.get("batch_no"))
		self.assertAlmostEqual(flt(row["issued_qty"]), 1160)
		self.assertAlmostEqual(flt(row["returned_qty"]), 12)
		self.assertAlmostEqual(flt(row["manufacture_consumed_qty"]), 1148)
		self.assertAlmostEqual(flt(row["remaining_wip"]), 0)
		self.assertEqual(row["golden_status"], ST_BALANCED)
		self.assertEqual(res["job_card_summaries"].get("PO-JOB08760"), ST_BALANCED)

	def test_ga12_repair_engine_batch_sensitive_unchanged(self):
		# Report-only patch: repair modules still key evidence by batch
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import golden_rule
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import manufacture_plan

		src = inspect.getsource(golden_rule.scan_golden_rule)
		self.assertIn("batch_no", src)
		mp_src = inspect.getsource(manufacture_plan)
		self.assertIn("batch_no", mp_src)
		# Report columns must not expose Batch
		cols = {c["fieldname"] for c in report_mod.get_columns()}
		self.assertNotIn("batch_no", cols)
		# Filters must not include batch
		# (JS checked separately / via file content)
		js_path = frappe.get_app_path(
			"erpnext_extensions",
			"erpnext_extensions",
			"report",
			"job_card_golden_rule_audit",
			"job_card_golden_rule_audit.js",
		)
		js = open(js_path).read()
		self.assertNotIn('fieldname: "batch"', js)
		self.assertNotIn("BATCH_MISMATCH", js)

	def test_ga_readonly_no_mutation(self):
		before = {
			"se": frappe.db.sql("select count(*) from `tabStock Entry` where docstatus=1")[0][0],
			"sed": frappe.db.sql("select count(*) from `tabStock Entry Detail`")[0][0],
			"sle": frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0],
			"gl": frappe.db.sql("select count(*) from `tabGL Entry`")[0][0],
			"jc": frappe.db.sql("select count(*) from `tabJob Card`")[0][0],
			"bin": frappe.db.sql("select count(*) from `tabBin`")[0][0],
			"batch": frappe.db.sql("select count(*) from `tabBatch`")[0][0],
		}
		run_golden_rule_audit({**RANGE, "job_card": "PO-JOB08760", "show_balanced": 1})
		after = {
			"se": frappe.db.sql("select count(*) from `tabStock Entry` where docstatus=1")[0][0],
			"sed": frappe.db.sql("select count(*) from `tabStock Entry Detail`")[0][0],
			"sle": frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0],
			"gl": frappe.db.sql("select count(*) from `tabGL Entry`")[0][0],
			"jc": frappe.db.sql("select count(*) from `tabJob Card`")[0][0],
			"bin": frappe.db.sql("select count(*) from `tabBin`")[0][0],
			"batch": frappe.db.sql("select count(*) from `tabBatch`")[0][0],
		}
		self.assertEqual(before, after)


def run():
	loader = unittest.TestLoader()
	suite = loader.loadTestsFromTestCase(TestGoldenRuleAuditV5510)
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
