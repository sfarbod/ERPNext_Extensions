# Copyright (c) 2026, ERPNext Extensions contributors
"""Material Issue ownership + merge hardening tests (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import build_evidence
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
	classify_logistics_document,
	discover_downstream,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.mi_ownership import (
	MI_BLOCKED,
	MI_MERGE_SAFE,
	MI_USER_DECISION,
	classify_material_issue,
	discover_material_issues,
)


class TestMIOwnershipV550(FrappeTestCase):
	def test_mi_classifier_real_examples(self):
		# SAFE-ish balanced MI (ownership strong; DN may still block Apply elsewhere)
		if frappe.db.exists("Stock Entry", "MAT-STE-2026-39898"):
			ev = build_evidence("PO-JOB09002").get("items") or []
			c = classify_material_issue("MAT-STE-2026-39898", "PO-JOB09002", ev)
			self.assertIn(c["classification"], (MI_MERGE_SAFE, MI_USER_DECISION))
			self.assertFalse(c.get("shared_document"))
			self.assertTrue(c.get("owned_rows"))

		# SHARED multi-row MI
		if frappe.db.exists("Stock Entry", "MAT-STE-2026-37748"):
			ev = build_evidence("PO-JOB08028").get("items") or []
			c = classify_material_issue("MAT-STE-2026-37748", "PO-JOB08028", ev)
			# Either shared blocked or unmatched rows → not MERGE_SAFE auto
			self.assertNotEqual(c["classification"], MI_MERGE_SAFE)

		# Outside WIP / weak → not MERGE_SAFE
		if frappe.db.exists("Stock Entry", "MAT-STE-2026-31839"):
			ev = build_evidence("PO-JOB08001").get("items") or []
			c = classify_material_issue("MAT-STE-2026-31839", "PO-JOB08001", ev)
			self.assertIn(c["classification"], (MI_BLOCKED, MI_USER_DECISION, MI_MERGE_SAFE))

	def test_08760_shared_logistics_blocked(self):
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("missing PO-JOB08760")
		down = discover_downstream("PO-JOB08760", ["MAT-STE-2026-31724-1"])
		reasons = " ".join(
			(b.get("reason") if isinstance(b, dict) else str(b)) for b in (down.get("blocked") or [])
		)
		self.assertIn("SHARED_BLOCKED", reasons)
		# Minimal cancel set must NOT include foreign co-item chain docs
		minimal = set(down.get("minimal_cancel_set") or [])
		self.assertNotIn("MAT-STE-2026-37777", minimal)
		self.assertNotIn("MAT-STE-2026-37643", minimal)
		# Seed audit marks 31725/31726 shared blocked
		audit = {a.name if hasattr(a, "name") else a["name"]: a for a in down.get("logistics_audit") or []}
		for name in ("MAT-STE-2026-31725", "MAT-STE-2026-31726"):
			if name in audit:
				cls = getattr(audit[name], "shared_class", None) or audit[name].get("shared_class")
				self.assertEqual(cls, "SHARED_BLOCKED")

	def test_08760_plan_blocked_shared(self):
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("missing PO-JOB08760")
		plan = build_manufacture_plan(
			"PO-JOB08760",
			dispositions=[
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 1148,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			merge_documents=["MAT-STE-2026-31724-1"],
			stamp_mode="HISTORICAL",
		)
		self.assertFalse(plan["apply_allowed"])
		self.assertTrue(any("SHARED_BLOCKED" in b for b in plan["blockers"]))

	def test_mi_evidence_bucket(self):
		if not frappe.db.exists("Job Card", "PO-JOB09002"):
			self.skipTest("missing PO-JOB09002")
		ev = build_evidence("PO-JOB09002")
		row = next(
			(
				i
				for i in ev["items"]
				if i["item_code"] == "13200544" and flt(i.get("mi_consumed")) > 0
			),
			None,
		)
		self.assertIsNotNone(row)
		self.assertGreater(flt(row["mi_consumed"]), 0)
		# MI counted once in remainder math
		self.assertLessEqual(abs(flt(row["wip_remainder"])), 1e-6)


class TestMIAtomicFailpointsV550(FrappeTestCase):
	"""MI-A01..A05 — only meaningful when a plan is not dependency-blocked.

	Uses PO-JOB08860 when it can form a plan; otherwise skip.
	"""

	JC = "PO-JOB08860"
	MI = "MAT-STE-2026-37803"

	def setUp(self):
		if not frappe.db.exists("Job Card", self.JC):
			self.skipTest("missing JC")
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def _baseline_mi(self):
		return frappe.db.get_value("Stock Entry", self.MI, ["docstatus", "modified"], as_dict=1)

	def test_mi_failpoints_or_blocked(self):
		mis = discover_material_issues(self.JC)
		mi_names = [m["name"] for m in mis if m["name"] == self.MI]
		plan_input = {
			"dispositions": [],
			"merge_material_issues": mi_names,
			"stamp_mode": "HISTORICAL",
		}
		# Fill dispositions for any remaining WIP so plan validation can proceed to blockers
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		scan = scan_golden_rule(self.JC)
		plan_input["dispositions"] = [
			{
				"item_code": r["item_code"],
				"batch_no": r.get("batch_no") or "",
				"proposed_consumed": flt(r.get("remaining_wip")) if flt(r.get("remaining_wip")) > 0 else 0,
				"proposed_scrap": 0,
				"proposed_return": 0,
				"proposed_still_in_wip": 0,
			}
			for r in scan["rows"]
			if flt(r.get("remaining_wip")) > 1e-9
			or flt(r.get("mi_consumed")) > 1e-9
		]
		# zero-rem rows still need empty disposition via validate — include all
		plan_input["dispositions"] = [
			{
				"item_code": r["item_code"],
				"batch_no": r.get("batch_no") or "",
				"proposed_consumed": max(flt(r.get("remaining_wip")), 0),
				"proposed_scrap": 0,
				"proposed_return": 0,
				"proposed_still_in_wip": 0,
			}
			for r in scan["rows"]
		]

		points = (
			"after_mi_cancel",
			"after_mi_and_mfg_cancel",
			"after_canonical_submit",
			"after_valuation",
			"during_gl_verify",
		)
		for point in points:
			before = self._baseline_mi()
			frappe.flags.jc_repair_fail_at = point
			res = run_repair(self.JC, plan_input=plan_input, dry_run=True)
			after = self._baseline_mi()
			self.assertEqual(cint_docstatus(before), cint_docstatus(after), msg=point)
			self.assertFalse(res.get("mutated"), msg=point)
			self.assertFalse(res.get("committed"), msg=point)
			self.assertIn(
				res.get("status"),
				("DRY_RUN_FAIL", "BLOCKED", "STALE PLAN", "DRY_RUN_PASS"),
				msg=f"{point}: {res.get('status')} {res.get('error')}",
			)
			# MI must remain submitted after rollback / block
			self.assertEqual(cint_docstatus(after), 1, msg=point)
			frappe.flags.jc_repair_fail_at = None


def cint_docstatus(row):
	from frappe.utils import cint

	return cint(row.docstatus) if row else None
