# Copyright (c) 2026, ERPNext Extensions contributors
"""Atomic Dry Run rollback / failure-injection tests (v5.5.0).

These run against Development canaries but MUST leave zero persistent
Stock Entry / SLE / GL mutation (Dry Run always rolls back).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.sync_valuation import suppress_auto_riv


JC = "PO-JOB08760"
ITEM = "13200544"
BATCH = "5648-13200544-PR-10741"


def _baseline(job_card: str) -> dict:
	ses = frappe.db.sql(
		"""
		select name, docstatus from `tabStock Entry`
		where job_card=%s order by name
		""",
		job_card,
		as_dict=1,
	)
	return {
		"se": {(r.name, r.docstatus) for r in ses},
		"jc_mod": str(frappe.db.get_value("Job Card", job_card, "modified")),
		"active_mfg": frappe.db.sql(
			"""
			select name from `tabStock Entry`
			where job_card=%s and purpose='Manufacture' and docstatus=1
			""",
			job_card,
			pluck="name",
		),
	}


class TestAtomicRepairV550(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Job Card", JC):
			self.skipTest("missing PO-JOB08760")
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def _plan(self):
		return {
			"dispositions": [
				{
					"item_code": ITEM,
					"batch_no": BATCH,
					"proposed_consumed": 1148,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			"merge_documents": ["MAT-STE-2026-31724-1"],
			"stamp_mode": "HISTORICAL",
		}

	def test_suppress_auto_riv_context(self):
		from erpnext.controllers.stock_controller import StockController

		with suppress_auto_riv():
			self.assertIsNone(StockController.repost_future_sle_and_gle(object(), force=True))

	def test_a01_failure_after_first_cancel_rollback(self):
		before = _baseline(JC)
		frappe.flags.jc_repair_fail_at = "after_first_cancel"
		res = run_repair(JC, plan_input=self._plan(), dry_run=True)
		after = _baseline(JC)
		self.assertEqual(before, after)
		self.assertFalse(res.get("mutated"))
		self.assertFalse(res.get("committed"))
		# Either injected failure or earlier blocker — must not mutate
		self.assertIn(res.get("status"), ("DRY_RUN_FAIL", "BLOCKED", "STALE PLAN", "DRY_RUN_PASS"))

	def test_a02_failure_after_mfg_cancel_rollback(self):
		before = _baseline(JC)
		frappe.flags.jc_repair_fail_at = "after_mfg_cancel"
		res = run_repair(JC, plan_input=self._plan(), dry_run=True)
		after = _baseline(JC)
		self.assertEqual(before["se"], after["se"])
		self.assertEqual(before["active_mfg"], after["active_mfg"])
		self.assertFalse(res.get("mutated"))

	def test_dry_run_never_commits_business(self):
		before = _baseline(JC)
		frappe.flags.jc_repair_fail_at = None
		res = run_repair(JC, plan_input=self._plan(), dry_run=True)
		after = _baseline(JC)
		self.assertEqual(before["se"], after["se"])
		self.assertEqual(before["active_mfg"], after["active_mfg"])
		self.assertFalse(res.get("committed"))
		self.assertFalse(res.get("mutated"))

	def test_apply_requires_confirm(self):
		with self.assertRaises(Exception):
			run_repair(JC, plan_input=self._plan(), dry_run=False, confirm=0)

	def test_a03_a08_injected_failures_rollback(self):
		"""A03–A08: late-stage injected failures must roll back business state.

		Full logistics Dry Run is expensive (~minutes). Cover representative
		late points; A01/A02 already cover early cancel failures.
		"""
		points = (
			"after_canonical_insert",
			"after_canonical_submit",
			"during_gl_verify",
		)
		for point in points:
			before = _baseline(JC)
			frappe.flags.jc_repair_fail_at = point
			res = run_repair(JC, plan_input=self._plan(), dry_run=True)
			after = _baseline(JC)
			self.assertEqual(before["se"], after["se"], msg=point)
			self.assertEqual(before["active_mfg"], after["active_mfg"], msg=point)
			self.assertFalse(res.get("mutated"), msg=point)
			self.assertFalse(res.get("committed"), msg=point)
			self.assertIn(
				res.get("status"),
				("DRY_RUN_FAIL", "BLOCKED", "STALE PLAN"),
				msg=f"{point}: {res.get('status')} {res.get('error')}",
			)
			# If plan blocked before fail-point, still must not mutate.
			if res.get("status") == "DRY_RUN_FAIL":
				self.assertIn("INJECTED_FAILURE", res.get("error") or "", msg=point)
			frappe.flags.jc_repair_fail_at = None
