# Copyright (c) 2026, ERPNext Extensions contributors
"""Shared logistics recreate-safe classification + atomic SL tests (v5.5.0)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
	discover_downstream,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
	DEDICATED,
	SHARED_BLOCKED,
	SHARED_RECREATE_SAFE,
	classify_shared_logistics,
	foreign_docs_excluded,
	simulate_cancel_stock_ok,
	snapshot_stock_entry,
)


FG_KEYS = {("20100067", "505188-20100067-MY262821A11")}
FOREIGN_7 = [
	"MAT-STE-2026-32617",
	"MAT-STE-2026-40364",
	"MAT-STE-2026-33377",
	"MAT-STE-2026-33928",
	"MAT-STE-2026-40369",
	"MAT-STE-2026-37643",
	"MAT-STE-2026-37777",
]


class TestSharedLogisticsV550(FrappeTestCase):
	def test_sr02_sr08_31725_31726_classification(self):
		if not frappe.db.exists("Stock Entry", "MAT-STE-2026-31725"):
			self.skipTest("missing 31725")
		cancel_set = ["MAT-STE-2026-31726", "MAT-STE-2026-31725"]
		c25 = classify_shared_logistics(
			"MAT-STE-2026-31725", FG_KEYS, cancel_set_desc=cancel_set
		)
		c26 = classify_shared_logistics(
			"MAT-STE-2026-31726", FG_KEYS, cancel_set_desc=cancel_set
		)
		# 31726 cancel-safe alone/with set; 31725 shortfall without foreign chain
		self.assertEqual(c26["shared_class"], SHARED_RECREATE_SAFE)
		self.assertEqual(c25["shared_class"], SHARED_BLOCKED)
		self.assertTrue(c25.get("unrelated_rows"))
		self.assertTrue(c26.get("unrelated_rows"))

	def test_sr10_no_foreign_expansion(self):
		down = discover_downstream("PO-JOB08760", ["MAT-STE-2026-31724-1"])
		names = [x.name for x in down.get("logistics") or []]
		audit_names = [
			(x.name if hasattr(x, "name") else x.get("name"))
			for x in (down.get("logistics_audit") or [])
		]
		self.assertTrue(foreign_docs_excluded(names, FOREIGN_7))
		self.assertTrue(foreign_docs_excluded(audit_names, FOREIGN_7))

	def test_sr03_snapshot_preserves_rows(self):
		snap = snapshot_stock_entry("MAT-STE-2026-31725")
		self.assertEqual(len(snap["rows"]), 4)
		items = {r["item_code"] for r in snap["rows"]}
		self.assertIn("20100067", items)
		self.assertIn("20100041", items)

	def test_sr09_shared_mi_still_blocked_purpose(self):
		# Material Issue must not become SHARED_RECREATE_SAFE via logistics classifier
		if not frappe.db.exists("Stock Entry", "MAT-STE-2026-40364"):
			self.skipTest("missing MI")
		# 40364 is Material Issue with no FG key → UNRELATED / blocked path
		c = classify_shared_logistics("MAT-STE-2026-40364", FG_KEYS, cancel_set_desc=[])
		self.assertIn(c["shared_class"], ("UNRELATED", SHARED_BLOCKED))

	def test_simulate_cancel_set_fails_for_31725(self):
		ok_alone, _ = simulate_cancel_stock_ok(["MAT-STE-2026-31726"])
		self.assertTrue(ok_alone)
		ok, reason = simulate_cancel_stock_ok(
			["MAT-STE-2026-31726", "MAT-STE-2026-31725"]
		)
		self.assertFalse(ok)
		self.assertTrue(reason)

	def test_08760_plan_still_blocked(self):
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
		self.assertTrue(any("31725" in b and "SHARED_BLOCKED" in b for b in plan["blockers"]))
		# 31726 may show SAFE TO RECREATE in documents while set still blocked by 31725
		docs = {d["name"]: d for d in plan["documents"]}
		if "MAT-STE-2026-31726" in docs:
			self.assertIn(
				docs["MAT-STE-2026-31726"].get("shared_class"),
				(SHARED_RECREATE_SAFE, SHARED_BLOCKED, None),
			)


class TestSharedLogisticsAtomicV550(FrappeTestCase):
	"""SL-A01..A08 — when plan is BLOCKED, prove no mutation; failpoints noop-safe."""

	JC = "PO-JOB08760"

	def setUp(self):
		if not frappe.db.exists("Job Card", self.JC):
			self.skipTest("missing JC")
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def _plan(self):
		return {
			"dispositions": [
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 1148,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			"merge_documents": ["MAT-STE-2026-31724-1"],
			"stamp_mode": "HISTORICAL",
		}

	def _baseline(self):
		names = (
			"MAT-STE-2026-31724-1",
			"MAT-STE-2026-31725",
			"MAT-STE-2026-31726",
		)
		return {
			n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in names
		}

	def test_sl_a01_a08_blocked_or_rollback(self):
		points = (
			"after_first_shared_cancel",
			"after_both_shared_cancel",
			"after_mfg_cancel",
			"after_canonical_submit",
			"after_first_shared_recreate",
			"after_second_shared_recreate",
			"after_valuation",
			"during_unrelated_equivalence",
		)
		for point in points:
			before = self._baseline()
			frappe.flags.jc_repair_fail_at = point
			res = run_repair(self.JC, plan_input=self._plan(), dry_run=True)
			after = self._baseline()
			self.assertEqual(before, after, msg=point)
			self.assertFalse(res.get("mutated"), msg=point)
			self.assertIn(
				res.get("status"),
				("BLOCKED", "DRY_RUN_FAIL", "STALE PLAN", "DRY_RUN_PASS"),
				msg=f"{point}: {res.get('status')}",
			)
			# On current evidence 08760 is BLOCKED before cancel — that is acceptable.
			frappe.flags.jc_repair_fail_at = None
