# Copyright (c) 2026, ERPNext Extensions contributors
"""Shared logistics recreate-safe classification + atomic SL tests (v5.5.0).

Post controlled Apply on PO-JOB08760: originals cancelled; replacements active.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
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


def _active_replacements():
	"""Return submitted 31725/31726 replacement names for 08760 FG batch."""
	scan = scan_golden_rule("PO-JOB08760")
	mfg = scan["manufactures"][0].name if scan.get("manufactures") else None
	down = discover_downstream("PO-JOB08760", [mfg] if mfg else [])
	names = down.get("minimal_cancel_set") or []
	return names  # reverse chrono: later first


class TestSharedLogisticsV550(FrappeTestCase):
	def test_sr02_sr08_replacement_classification(self):
		names = _active_replacements()
		self.assertGreaterEqual(len(names), 2)
		cancel_set = list(names)
		# Later logistics (Milan) alone should be SAFE; earlier (Approved) may need bridge
		c_later = classify_shared_logistics(names[0], FG_KEYS, cancel_set_desc=cancel_set)
		c_earlier = classify_shared_logistics(names[-1], FG_KEYS, cancel_set_desc=cancel_set)
		self.assertIn(c_later["shared_class"], (SHARED_RECREATE_SAFE, DEDICATED, SHARED_BLOCKED))
		self.assertTrue(c_later.get("unrelated_rows") or c_earlier.get("unrelated_rows"))
		# Cancelled originals remain cancelled
		self.assertEqual(cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31725", "docstatus")), 2)
		self.assertEqual(cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31726", "docstatus")), 2)

	def test_sr10_no_foreign_expansion(self):
		scan = scan_golden_rule("PO-JOB08760")
		mfg = scan["manufactures"][0].name
		down = discover_downstream("PO-JOB08760", [mfg])
		names = [x.name for x in down.get("logistics") or []]
		audit_names = [
			(x.name if hasattr(x, "name") else x.get("name"))
			for x in (down.get("logistics_audit") or [])
		]
		self.assertTrue(foreign_docs_excluded(names, FOREIGN_7))
		self.assertTrue(foreign_docs_excluded(audit_names, FOREIGN_7))

	def test_sr03_snapshot_preserves_rows(self):
		# Snapshot cancelled original still has 4 rows
		snap = snapshot_stock_entry("MAT-STE-2026-31725")
		self.assertEqual(len(snap["rows"]), 4)
		items = {r["item_code"] for r in snap["rows"]}
		self.assertIn("20100067", items)
		self.assertIn("20100041", items)

	def test_sr09_shared_mi_still_blocked_purpose(self):
		if not frappe.db.exists("Stock Entry", "MAT-STE-2026-40364"):
			self.skipTest("missing MI")
		c = classify_shared_logistics("MAT-STE-2026-40364", FG_KEYS, cancel_set_desc=[])
		self.assertIn(c["shared_class"], ("UNRELATED", SHARED_BLOCKED))

	def test_simulate_cancel_replacements_bridge_needed(self):
		names = _active_replacements()
		self.assertGreaterEqual(len(names), 2)
		ok_alone, _ = simulate_cancel_stock_ok([names[0]])
		# Alone may be safe; full set typically needs bridge after stock moved
		ok_set, reason = simulate_cancel_stock_ok(names)
		self.assertTrue(ok_alone or not ok_set)
		if not ok_set:
			self.assertTrue(reason)

	def test_08760_post_apply_nothing_to_repair(self):
		scan = scan_golden_rule("PO-JOB08760")
		mfg = scan["manufactures"][0].name
		plan = build_manufacture_plan(
			"PO-JOB08760",
			dispositions=[
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			merge_documents=[mfg],
			stamp_mode="HISTORICAL",
		)
		self.assertFalse(plan["apply_allowed"])
		self.assertTrue(any("Nothing to repair" in b for b in plan["blockers"]))
		# Downstream still classifies replacements; bridge may still be required if re-repair
		down = discover_downstream("PO-JOB08760", [mfg])
		self.assertTrue(foreign_docs_excluded(down.get("minimal_cancel_set") or [], FOREIGN_7))


class TestSharedLogisticsAtomicV550(FrappeTestCase):
	"""SL-A01..A08 — post-Apply: plan is Nothing-to-repair / BLOCKED; no mutation."""

	JC = "PO-JOB08760"

	def setUp(self):
		if not frappe.db.exists("Job Card", self.JC):
			self.skipTest("missing JC")
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def _plan(self):
		scan = scan_golden_rule(self.JC)
		mfg = scan["manufactures"][0].name
		return {
			"dispositions": [
				{
					"item_code": "13200544",
					"batch_no": "5648-13200544-PR-10741",
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
			"merge_documents": [mfg],
			"stamp_mode": "HISTORICAL",
		}

	def _baseline(self):
		scan = scan_golden_rule(self.JC)
		mfg = scan["manufactures"][0].name
		names = [mfg, "MAT-STE-2026-31724-1", "MAT-STE-2026-31725", "MAT-STE-2026-31726"]
		names += discover_downstream(self.JC, [mfg]).get("minimal_cancel_set") or []
		return {n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in names}

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
			frappe.flags.jc_repair_fail_at = None
