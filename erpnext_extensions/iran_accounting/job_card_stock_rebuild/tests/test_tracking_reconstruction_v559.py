# Copyright (c) 2026, ERPNext Extensions contributors
"""Job Card tracking reconstruction + canonical JCI (v5.5.9)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.disposition_prefill import DecisionState
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tracking_reconstruction import (
	MAP_UNIQUE,
	SED_SAFE,
	build_jci_resolution,
	classify_sed_linkage,
	propose_tracking_reconstruction,
)
from erpnext_extensions.iran_accounting.scrap_costing import MANUFACTURE_COSTING_CONTRACT_VERSION


JC = "PO-JOB08760"
ITEM = "13200544"
JCI = "39o0p9p4ep"
BATCH = "5648-13200544-PR-10741"
SCRAP_ITEM = "13200190"


def _full_plan_input():
	scan = scan_golden_rule(JC)
	ds = DecisionState()
	ds.init_from_scan(scan["rows"])
	dispositions = [
		ds.dry_run_payload_row(r["item_code"], r.get("batch_no") or "") for r in scan["rows"]
	]
	return {
		"dispositions": dispositions,
		"merge_documents": [m.name for m in scan["manufactures"]],
		"stamp_mode": "HISTORICAL",
	}


class TestTrackingReconstructionV559(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Job Card", JC):
			self.skipTest("missing PO-JOB08760")
		frappe.flags.jc_repair_fail_at = None
		self.assertEqual(MANUFACTURE_COSTING_CONTRACT_VERSION, "5.3.43")
		disabled = frappe.db.get_value(
			"Server Script", "Custom 6 - Manufacturing Integrity Validator", "disabled"
		)
		self.assertEqual(cint_safe(disabled), 0, "Custom 6 must remain enabled")

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None
		frappe.db.rollback()

	def test_jci_map_all_unique(self):
		res = build_jci_resolution(JC)
		self.assertEqual(res["counts"][MAP_UNIQUE], 17)
		self.assertEqual(res["counts"]["AMBIGUOUS"], 0)
		self.assertEqual(res["by_item"][ITEM]["job_card_item"], JCI)

	def test_sed_safe_backfill_classification(self):
		sed = classify_sed_linkage(JC)
		self.assertGreater(sed["counts"][SED_SAFE], 0)
		self.assertEqual(sed["counts"]["AMBIGUOUS"], 0)
		for row in sed["safe_backfills"]:
			self.assertTrue(row["write_required"])
			self.assertEqual(row["derived_job_card_item"], res_jci(row["item_code"]))

	def test_13200544_tracking_proposal(self):
		plan = build_manufacture_plan(JC, dispositions=_full_plan_input()["dispositions"])
		trk = plan["tracking_repair"]
		row = next(c for c in trk["components"] if c["item_code"] == ITEM)
		self.assertEqual(row["job_card_item"], JCI)
		self.assertEqual(flt(row["issued"]), 1160)
		self.assertEqual(flt(row["returned"]), 12)
		self.assertEqual(flt(row["current_transferred"]), 0)
		self.assertEqual(flt(row["proposed_transferred"]), 1148)
		self.assertEqual(flt(row["proposed_consumed_after_apply"]), 1148)

	def test_canonical_rows_all_jci(self):
		plan = build_manufacture_plan(JC, dispositions=_full_plan_input()["dispositions"])
		src = [
			r
			for r in plan["canonical_manufacture"]["rows"]
			if r.get("type") == "CONSUME"
			or (r.get("s_warehouse") and not r.get("t_warehouse"))
		]
		self.assertGreaterEqual(len(src), 17)
		for r in src:
			self.assertTrue(r.get("job_card_item"), r.get("item_code"))
		row = next(r for r in src if r["item_code"] == ITEM)
		self.assertEqual(row["job_card_item"], JCI)
		self.assertEqual(flt(row["qty"]), 1148)

	def test_scrap_canary_no_double_count(self):
		scan = scan_golden_rule(JC)
		gr = next(r for r in scan["rows"] if r["item_code"] == SCRAP_ITEM)
		self.assertEqual(flt(gr["consumed"]), 580)
		self.assertEqual(flt(gr["scrap"]), 5)
		self.assertEqual(flt(gr["remaining_wip"]), 0)

	def test_dry_run_pass_custom6_enabled(self):
		before_xfer = flt(frappe.db.get_value("Job Card Item", JCI, "transferred_qty"))
		before_jci = frappe.db.sql(
			"""
			select count(*) from `tabStock Entry Detail` sed
			join `tabStock Entry` se on se.name=sed.parent
			where se.job_card=%s and ifnull(sed.job_card_item,'')!=''
			""",
			JC,
		)[0][0]
		se_n = frappe.db.count("Stock Entry")
		res = run_repair(JC, plan_input=_full_plan_input(), dry_run=True)
		self.assertTrue(res.get("ok"), res.get("error"))
		self.assertEqual(res.get("status"), "DRY_RUN_PASS")
		self.assertFalse(res.get("mutated"))
		self.assertFalse(res.get("committed"))
		self.assertNotIn("has no Job Card Item", res.get("error") or "")
		self.assertEqual(flt(frappe.db.get_value("Job Card Item", JCI, "transferred_qty")), before_xfer)
		after_jci = frappe.db.sql(
			"""
			select count(*) from `tabStock Entry Detail` sed
			join `tabStock Entry` se on se.name=sed.parent
			where se.job_card=%s and ifnull(sed.job_card_item,'')!=''
			""",
			JC,
		)[0][0]
		self.assertEqual(after_jci, before_jci)
		self.assertEqual(frappe.db.count("Stock Entry"), se_n)

	def test_failure_after_sed_backfill_rolls_back(self):
		before = flt(frappe.db.get_value("Job Card Item", JCI, "transferred_qty"))
		frappe.flags.jc_repair_fail_at = "after_sed_backfill"
		res = run_repair(JC, plan_input=_full_plan_input(), dry_run=True)
		self.assertFalse(res.get("ok"))
		self.assertFalse(res.get("mutated"))
		self.assertEqual(flt(frappe.db.get_value("Job Card Item", JCI, "transferred_qty")), before)

	def test_failure_after_transferred_update_rolls_back(self):
		before = flt(frappe.db.get_value("Job Card Item", JCI, "transferred_qty"))
		frappe.flags.jc_repair_fail_at = "after_transferred_update"
		res = run_repair(JC, plan_input=_full_plan_input(), dry_run=True)
		self.assertFalse(res.get("ok"))
		self.assertEqual(flt(frappe.db.get_value("Job Card Item", JCI, "transferred_qty")), before)

	def test_failure_after_consumed_update_rolls_back(self):
		before = flt(frappe.db.get_value("Job Card Item", JCI, "consumed_qty"))
		frappe.flags.jc_repair_fail_at = "after_consumed_update"
		res = run_repair(JC, plan_input=_full_plan_input(), dry_run=True)
		self.assertFalse(res.get("ok"))
		self.assertEqual(flt(frappe.db.get_value("Job Card Item", JCI, "consumed_qty")), before)


def cint_safe(v):
	from frappe.utils import cint

	return cint(v)


def res_jci(item_code):
	return build_jci_resolution(JC)["by_item"][item_code]["job_card_item"]
