# Copyright (c) 2026, ERPNext Extensions contributors
"""Canonical Manufacture department + issued-rate preservation (v5.5.13)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
	_bind_preserve_rates,
	_reapply_snapshot_rates,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
	resolve_mtfm_department,
	stamp_canonical_departments,
)

JC = "PO-JOB09259"
EXPECTED_DEPT = "واحد بسته بندی - E"

CANARY_RATES = {
	("13200066", "5723-13200066-0503002-HP-168951161-0"): (3003.0, 14640.0),
	("13200144", "5640-13200144-PVX-1-1-10741"): (5213.0, 146083.0),
	("13200144", "866-13200144-pvx-1-1-600-1201-10741"): (72.0, 54629.0),
	("13200144", "867-13200144-pvx-1-1-600-1201-10741"): (861.0, 54629.0),
	("13200383", "9410-13200383-0505001-hp-168951265-0"): (3073.0, 172000.0),
}
EXPECTED_ADDED_VALUE = 1_385_019_456.0


def _active_mfg(job_card: str) -> str | None:
	names = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where job_card=%s and purpose='Manufacture' and docstatus=1
		order by modified desc
		""",
		job_card,
		pluck="name",
	)
	return names[0] if names else None


def _dispositions_from_scan(scan: dict) -> list[dict]:
	return [
		{
			"item_code": r["item_code"],
			"batch_no": r.get("batch_no") or "",
			"proposed_consumed": flt(r.get("proposed_consumed")),
			"proposed_scrap": flt(r.get("proposed_scrap")),
			"proposed_return": flt(r.get("proposed_return")),
			"proposed_still_in_wip": flt(r.get("proposed_still_in_wip")),
		}
		for r in scan.get("rows") or []
	]


def _plan(job_card: str, merge: str | None = None) -> dict:
	scan = scan_golden_rule(job_card)
	mfg = merge or _active_mfg(job_card)
	return build_manufacture_plan(
		job_card,
		dispositions=_dispositions_from_scan(scan),
		merge_documents=[mfg] if mfg else [],
		stamp_mode="HISTORICAL",
		merge_material_issues=[],
	)


class TestCanonicalDeptRateV5513(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Job Card", JC):
			raise frappe.SkipTest(f"missing {JC}")
		cls.active = _active_mfg(JC)
		if not cls.active:
			raise frappe.SkipTest(f"no active Manufacture for {JC}")

	def test_dpr01_dpr06_active_canonical_department_complete(self):
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": self.active},
			fields=["item_code", "batch_no", "department"],
		)
		self.assertEqual(len(rows), 28)
		for r in rows:
			self.assertEqual((r.department or "").strip(), EXPECTED_DEPT)

	def test_dpr02_dpr03_mtfm_department_resolver(self):
		resolved = resolve_mtfm_department(
			JC, "13200066", "5723-13200066-0503002-HP-168951161-0"
		)
		self.assertTrue(resolved.get("ok"))
		self.assertEqual(resolved.get("department"), EXPECTED_DEPT)

	def test_dpr04_conflicting_departments_block(self):
		rows = [
			{
				"type": "CONSUME",
				"item_code": "X",
				"batch_no": "B",
				"qty": 1,
				"s_warehouse": "W",
				"t_warehouse": None,
				"is_finished_item": 0,
				"_department_ambiguous": True,
				"_dept_candidates": ["Dept A", "Dept B"],
				"department_evidence": ["SE-1", "SE-2"],
			}
		]
		_, blockers = stamp_canonical_departments(JC, rows, merge_documents=[])
		self.assertTrue(any("AMBIGUOUS_DEPARTMENT" in b for b in blockers))

	def test_dpr05_missing_department_blocks(self):
		rows = [
			{
				"type": "CONSUME",
				"item_code": "__no_such_item__",
				"batch_no": "__no_batch__",
				"qty": 1,
				"s_warehouse": "W",
				"t_warehouse": None,
				"is_finished_item": 0,
				"basic_rate": 1,
			}
		]
		_, blockers = stamp_canonical_departments(JC, rows, merge_documents=[])
		self.assertTrue(any("MISSING_DEPARTMENT" in b for b in blockers))

	def test_dpr07_plan_stamps_department_without_custom8(self):
		plan = _plan(JC, self.active)
		# Already repaired → Nothing to repair is OK; department stamp still runs on rows
		rows = (plan.get("canonical_manufacture") or {}).get("rows") or []
		if not rows:
			self.skipTest("no canonical rows in nothing-to-repair plan")
		for r in rows:
			self.assertTrue((r.get("department") or "").strip(), msg=r)

	def test_rate01_to_rate07_applied_canaries(self):
		for (item, batch), (qty, rate) in CANARY_RATES.items():
			row = frappe.db.sql(
				"""
				select qty, basic_rate, valuation_rate, department, s_warehouse
				from `tabStock Entry Detail`
				where parent=%s and item_code=%s and ifnull(batch_no,'')=%s
				  and ifnull(s_warehouse,'')!=''
				""",
				(self.active, item, batch),
				as_dict=1,
			)
			self.assertEqual(len(row), 1, (item, batch))
			self.assertAlmostEqual(flt(row[0].qty), qty, places=6)
			self.assertAlmostEqual(flt(row[0].basic_rate), rate, places=6)
			self.assertEqual((row[0].department or "").strip(), EXPECTED_DEPT)

	def test_rate02_rate12_bind_preserves_snapshot(self):
		"""Bind + reapply keeps plan rates through Core reset_outgoing_rate=True."""
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": self.active},
			fields=[
				"idx",
				"item_code",
				"batch_no",
				"qty",
				"basic_rate",
				"valuation_rate",
				"s_warehouse",
				"t_warehouse",
				"is_finished_item",
				"custom_output_class",
				"job_card_item",
				"department",
			],
			order_by="idx",
		)
		doc = frappe.copy_doc(frappe.get_doc("Stock Entry", self.active))
		doc.name = None
		doc.docstatus = 0
		snap = []
		for r in rows:
			snap.append(
				{
					"idx": r.idx,
					"item_code": r.item_code,
					"batch_no": r.batch_no or "",
					"s_warehouse": r.s_warehouse,
					"t_warehouse": r.t_warehouse,
					"is_finished_item": r.is_finished_item,
					"custom_output_class": r.custom_output_class,
					"job_card_item": r.job_card_item,
					"basic_rate": flt(r.basic_rate),
					"valuation_rate": flt(r.valuation_rate),
				}
			)
		# Zero outgoing rates to force overwrite attempt
		for row in doc.items:
			if row.s_warehouse:
				row.basic_rate = 0
				row.valuation_rate = 0
		_bind_preserve_rates(doc, batch_snapshot=snap)
		doc.calculate_rate_and_amount(reset_outgoing_rate=True, raise_error_if_no_rate=False)
		_reapply_snapshot_rates(doc, batch_snapshot=snap)
		for (item, batch), (_qty, rate) in CANARY_RATES.items():
			match = [
				r
				for r in doc.items
				if r.item_code == item and (r.batch_no or "") == batch and r.s_warehouse
			]
			self.assertEqual(len(match), 1, (item, batch))
			self.assertAlmostEqual(flt(match[0].basic_rate), rate, places=6)

	def test_rate08_historical_and_issue_rows_present(self):
		# Post-repair: all consume rows exist with positive rates
		consume = frappe.db.sql(
			"""
			select item_code, batch_no, basic_rate from `tabStock Entry Detail`
			where parent=%s and ifnull(s_warehouse,'')!='' and ifnull(is_finished_item,0)=0
			""",
			self.active,
			as_dict=1,
		)
		self.assertGreaterEqual(len(consume), 16)
		for r in consume:
			self.assertGreater(flt(r.basic_rate), 0)

	def test_rate09_zero_evidence_blocks_before_submit(self):
		"""Unknown batch is rejected; synthetic CONSUME with rate 0 is blocked at plan stamp."""
		scan = scan_golden_rule(JC)
		dispositions = _dispositions_from_scan(scan)
		dispositions.append(
			{
				"item_code": "13200066",
				"batch_no": "__missing_batch_for_rate__",
				"proposed_consumed": 1,
				"proposed_scrap": 0,
				"proposed_return": 0,
				"proposed_still_in_wip": 0,
			}
		)
		plan = build_manufacture_plan(
			JC,
			dispositions=dispositions,
			merge_documents=[self.active],
			stamp_mode="HISTORICAL",
			merge_material_issues=[],
		)
		blockers = " ".join(plan.get("blockers") or [])
		self.assertTrue(
			"Unknown Item×Batch" in blockers or "ZERO_RATE" in blockers,
			blockers,
		)
		# Direct pre-submit invariant on a zero-rate CONSUME row
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
			_build_canonical_se,
		)

		bad_plan = {
			"job_card": JC,
			"work_order": plan.get("work_order"),
			"canonical_manufacture": {
				"supersedes": [self.active],
				"fg_completed_qty": 1,
				"posting_date": "2026-07-22",
				"posting_time": "18:05:50",
				"historical_stamp": "5.3.34",
				"rows": [
					{
						"type": "CONSUME",
						"item_code": "13200066",
						"batch_no": "5723-13200066-0503002-HP-168951161-0",
						"qty": 1,
						"s_warehouse": "انبار پایکار خط تولید اسپاد فارمد",
						"t_warehouse": None,
						"is_finished_item": 0,
						"basic_rate": 0,
						"valuation_rate": 0,
						"rate_source": "issue_transfer",
						"department": EXPECTED_DEPT,
						"job_card_item": "1qqcgbh6hf",
					}
				],
			},
		}
		with self.assertRaises(Exception) as ctx:
			_build_canonical_se(bad_plan)
		self.assertIn("ZERO_RATE", str(ctx.exception))
		frappe.db.rollback()

	def test_rate10_zero_rate_server_script_enabled(self):
		name = "نرخ صفر برای ردیف ها مجاز نیست"
		if not frappe.db.exists("Server Script", name):
			self.skipTest("zero-rate Server Script missing")
		self.assertEqual(int(frappe.db.get_value("Server Script", name, "disabled") or 0), 0)

	def test_rate11_no_bin_valuation_source_label(self):
		plan = _plan(JC, self.active)
		for r in (plan.get("canonical_manufacture") or {}).get("rows") or []:
			self.assertNotEqual(r.get("rate_source"), "bin_valuation")

	def test_acc01_applied_canary_economics(self):
		total = 0.0
		for (item, batch), (qty, rate) in CANARY_RATES.items():
			row = frappe.db.sql(
				"""
				select qty, basic_rate from `tabStock Entry Detail`
				where parent=%s and item_code=%s and ifnull(batch_no,'')=%s
				  and ifnull(s_warehouse,'')!=''
				""",
				(self.active, item, batch),
				as_dict=1,
			)[0]
			total += flt(row.qty) * flt(row.basic_rate)
		self.assertAlmostEqual(total, EXPECTED_ADDED_VALUE, places=3)

	def test_acc02_acc03_no_additional_cost(self):
		se = frappe.get_doc("Stock Entry", self.active)
		self.assertEqual(len(se.additional_costs or []), 0)
		self.assertAlmostEqual(flt(se.total_additional_costs), 0)

	def test_acc04_gl_balanced(self):
		gl = frappe.db.sql(
			"""
			select sum(debit) d, sum(credit) c from `tabGL Entry`
			where voucher_no=%s and is_cancelled=0
			""",
			self.active,
			as_dict=1,
		)[0]
		self.assertAlmostEqual(flt(gl.d), flt(gl.c), places=3)
		self.assertGreater(flt(gl.d), 0)

	def test_reg01_08760_golden_rule(self):
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("missing PO-JOB08760")
		scan = scan_golden_rule("PO-JOB08760")
		row = next(r for r in scan["rows"] if r["item_code"] == "13200544")
		self.assertAlmostEqual(flt(row["issued"]), 1160)
		self.assertAlmostEqual(flt(row["returned"]), 12)
		self.assertAlmostEqual(flt(row["consumed"]), 1148)
		self.assertAlmostEqual(flt(row["remaining_wip"]), 0)
		self.assertEqual(row["status"], "OK")

	def test_reg02_component_scrap_present(self):
		scrap = frappe.db.sql(
			"""
			select item_code, batch_no, qty, basic_rate, department
			from `tabStock Entry Detail`
			where parent=%s and ifnull(custom_output_class,'')='COMPONENT_SCRAP'
			""",
			self.active,
			as_dict=1,
		)
		self.assertTrue(scrap)
		for r in scrap:
			self.assertGreater(flt(r.basic_rate), 0)
			self.assertEqual((r.department or "").strip(), EXPECTED_DEPT)

	def test_reg03_workstation_isolation_importable(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.workstation_isolation import (
			SKIP_WORKSTATION_WRITES_FLAG,
			is_workstation_write_suppressed,
		)

		self.assertTrue(SKIP_WORKSTATION_WRITES_FLAG)
		self.assertFalse(is_workstation_write_suppressed())

	def test_reg04_audit_unchanged(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			run_golden_rule_audit,
		)

		self.assertTrue(callable(run_golden_rule_audit))

	def test_golden_rule_all_ok_after_apply(self):
		scan = scan_golden_rule(JC)
		bad = [r for r in scan["rows"] if r.get("status") != "OK"]
		self.assertEqual(bad, [])
