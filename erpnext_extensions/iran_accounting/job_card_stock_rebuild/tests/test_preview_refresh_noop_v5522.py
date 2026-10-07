# Copyright (c) 2026, ERPNext Extensions contributors
"""PX01–PX15 / PR01–PR10 — Manufacture Preview refresh + post-repair NO-OP scan (v5.5.22)."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
	rebuild_manufacture_preview,
	scan_manufacture_reconciliation,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
	simulate_cancel_stock_ok,
)


JC = "PO-JOB08631"
ITEM = "13200475"
B502 = "502-13200475-pn3121"
B503 = "503-13200475-pn3121"
CANON = "MAT-STE-2026-41001"
HIST = "MAT-STE-2026-29972-1"
QTY = 2961.0


def _consume_rows(plan):
	rows = ((plan or {}).get("canonical_manufacture") or {}).get("rows") or []
	return [
		r
		for r in rows
		if r.get("item_code") == ITEM
		and str(r.get("type") or "").upper() == "CONSUME"
	]


def _is_post_repair_balanced():
	if not frappe.db.exists("Job Card", JC):
		return False
	if not frappe.db.exists("Stock Entry", CANON):
		return False
	if flt(frappe.db.get_value("Stock Entry", CANON, "docstatus")) != 1:
		return False
	if not frappe.db.exists("Stock Entry", HIST):
		return False
	if flt(frappe.db.get_value("Stock Entry", HIST, "docstatus") or 0) != 2:
		return False
	return True


def _is_pre_repair_fixture():
	if not frappe.db.exists("Job Card", JC):
		return False
	if not frappe.db.exists("Stock Entry", HIST):
		return False
	if flt(frappe.db.get_value("Stock Entry", HIST, "docstatus")) != 1:
		return False
	q502 = flt(
		frappe.db.sql(
			"""
			select ifnull(sum(sed.qty),0) from `tabStock Entry Detail` sed
			join `tabStock Entry` se on se.name=sed.parent
			where se.docstatus=1 and se.purpose='Manufacture' and se.job_card=%s
			  and sed.item_code=%s and sed.batch_no=%s
			  and ifnull(sed.s_warehouse,'')!='' and ifnull(sed.is_finished_item,0)=0
			""",
			(JC, ITEM, B502),
		)[0][0]
	)
	q503 = flt(
		frappe.db.sql(
			"""
			select ifnull(sum(sed.qty),0) from `tabStock Entry Detail` sed
			join `tabStock Entry` se on se.name=sed.parent
			where se.docstatus=1 and se.purpose='Manufacture' and se.job_card=%s
			  and sed.item_code=%s and sed.batch_no=%s
			  and ifnull(sed.s_warehouse,'')!='' and ifnull(sed.is_finished_item,0)=0
			""",
			(JC, ITEM, B503),
		)[0][0]
	)
	return abs(q502 - QTY) < 1e-6 and abs(q503) < 1e-6


def _replace_approval(cand):
	return {
		"item_code": ITEM,
		"wrong_batch": B502,
		"correct_batch": B503,
		"qty": QTY,
		"evidence_fingerprint": cand["evidence_fingerprint"],
		"accepted": 1,
		"decision": "REPLACE_MANUFACTURE_BATCH",
	}


class TestPreviewRefreshNoopV5522(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.jc_shared_cancel_probe_cache = {}
		frappe.local.message_log = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.flags.jc_shared_cancel_probe_cache = {}
		frappe.local.message_log = []

	# --- PR: post-repair NO-OP ---
	def test_pr01_balanced_scan_no_repair_required(self):
		if not _is_post_repair_balanced():
			self.skipTest("need post-repair PO-JOB08631 fixture")
		plan = scan_manufacture_reconciliation(job_card=JC)["plan"]
		self.assertFalse(plan.get("repair_required"))
		self.assertFalse(plan.get("stock_repair_needed"))
		self.assertTrue(any("Nothing to repair" in str(b) for b in plan.get("blockers") or []))

	def test_pr02_no_destructive_dependency_when_noop(self):
		if not _is_post_repair_balanced():
			self.skipTest("need post-repair PO-JOB08631 fixture")
		plan = scan_manufacture_reconciliation(job_card=JC)["plan"]
		self.assertEqual(plan.get("minimal_cancel_set") or [], [])
		self.assertEqual(plan.get("downstream_blocked") or [], [])

	def test_pr03_no_temp_bridge_when_noop(self):
		if not _is_post_repair_balanced():
			self.skipTest("need post-repair PO-JOB08631 fixture")
		bridge = scan_manufacture_reconciliation(job_card=JC)["plan"].get("temporary_bridge") or {}
		self.assertFalse(bridge.get("required"))
		self.assertEqual(bridge.get("shortages") or [], [])

	def test_pr04_no_shared_blocked_warning_when_noop(self):
		if not _is_post_repair_balanced():
			self.skipTest("need post-repair PO-JOB08631 fixture")
		blockers = scan_manufacture_reconciliation(job_card=JC)["plan"].get("blockers") or []
		self.assertFalse(any("SHARED_BLOCKED" in str(b) for b in blockers))
		self.assertFalse(any("MERGE_BLOCKED" in str(b) for b in blockers))

	def test_pr05_no_cancel_probe_message_leak_when_noop(self):
		if not _is_post_repair_balanced():
			self.skipTest("need post-repair PO-JOB08631 fixture")
		frappe.local.message_log = []
		scan_manufacture_reconciliation(job_card=JC)
		self.assertEqual(getattr(frappe.local, "message_log", None) or [], [])

	def test_pr06_unresolved_row_still_plans_repair(self):
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		msg = scan_manufacture_reconciliation(job_card=JC)
		self.assertTrue(msg["plan"].get("repair_required") or msg["plan"].get("stock_repair_needed"))
		self.assertTrue(msg.get("manufacture_batch_replace_candidates"))

	def test_pr07_real_shared_blocked_string_not_filtered_when_repair(self):
		"""Hard SHARED_BLOCKED remains a real blocker string when present on a repair plan."""
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		plan = scan_manufacture_reconciliation(job_card=JC)["plan"]
		# Pre-repair may or may not be SHARED_BLOCKED depending on stock; if present keep it.
		blocked = [b for b in (plan.get("blockers") or []) if "SHARED_BLOCKED" in str(b)]
		if not blocked and not (plan.get("downstream_blocked") or []):
			self.skipTest("fixture has no SHARED_BLOCKED (bridge may absorb shortage)")
		# If SHARED_BLOCKED appears it must not be replaced by Nothing-to-repair NO-OP.
		self.assertTrue(plan.get("repair_required") or plan.get("stock_repair_needed"))
		self.assertFalse(any("Nothing to repair" in str(b) for b in plan.get("blockers") or []))

	def test_pr08_probe_shortage_does_not_leak_msgprint(self):
		probe = None
		for name in (
			"MAT-STE-2026-41002",
			"MAT-STE-2026-30487",
			"MAT-STE-2026-30490",
		):
			if (
				frappe.db.exists("Stock Entry", name)
				and flt(frappe.db.get_value("Stock Entry", name, "docstatus")) == 1
			):
				probe = name
				break
		if not probe:
			self.skipTest("no probe SE")
		frappe.local.message_log = [{"message": "pre-existing"}]
		ok, reason = simulate_cancel_stock_ok([probe])
		self.assertIsInstance(ok, bool)
		self.assertTrue(reason)
		msgs = getattr(frappe.local, "message_log", None) or []
		self.assertEqual(len(msgs), 1)
		self.assertEqual(msgs[0].get("message"), "pre-existing")

	def test_pr10_recreated_logistics_do_not_retrigger_repair(self):
		if not _is_post_repair_balanced():
			self.skipTest("need post-repair PO-JOB08631 fixture")
		plan = scan_manufacture_reconciliation(job_card=JC)["plan"]
		self.assertFalse(plan.get("repair_required"))
		for name in (
			"MAT-STE-2026-41002",
			"MAT-STE-2026-41005",
			"MAT-STE-2026-41006",
		):
			self.assertNotIn(name, plan.get("minimal_cancel_set") or [])

	# --- PX: authoritative preview with REPLACE ---
	def test_px01_scan_may_show_historical_and_suggested(self):
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		msg = scan_manufacture_reconciliation(job_card=JC)
		rows = _consume_rows(msg["plan"])
		batches = {r.get("batch_no") for r in rows}
		self.assertIn(B502, batches)
		self.assertIn(B503, batches)

	def test_px02_replace_defaults_unchecked_via_candidates(self):
		if not (_is_pre_repair_fixture() or _is_post_repair_balanced()):
			self.skipTest("missing JC fixture")
		msg = scan_manufacture_reconciliation(job_card=JC)
		self.assertEqual(msg["plan"].get("approved_manufacture_batch_replacements") or [], [])

	def test_px03_to_px06_replace_rebuild_omits_502_keeps_503_once(self):
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		msg = scan_manufacture_reconciliation(job_card=JC)
		cands = msg.get("manufacture_batch_replace_candidates") or []
		self.assertTrue(cands)
		c = cands[0]
		rebuilt = rebuild_manufacture_preview(
			job_card=JC,
			plan={
				"dispositions": [
					{
						"item_code": ITEM,
						"batch_no": B502,
						"proposed_consumed": 0,
						"proposed_scrap": 0,
						"proposed_return": 0,
						"proposed_still_in_wip": 0,
					},
					{
						"item_code": ITEM,
						"batch_no": B503,
						"proposed_consumed": 0,
						"proposed_scrap": 0,
						"proposed_return": 0,
						"proposed_still_in_wip": 0,
					},
				],
				"stamp_mode": "HISTORICAL",
				"manufacture_batch_replace_approvals": [_replace_approval(c)],
			},
		)
		self.assertTrue(rebuilt.get("ok"))
		rows = _consume_rows(rebuilt["plan"])
		self.assertFalse(any(r.get("batch_no") == B502 for r in rows))
		only503 = [r for r in rows if r.get("batch_no") == B503]
		self.assertEqual(len(only503), 1)
		self.assertAlmostEqual(flt(only503[0].get("qty")), QTY)
		self.assertAlmostEqual(sum(flt(r.get("qty")) for r in rows), QTY)
		self.assertTrue(rebuilt.get("fingerprint"))

	def test_px07_uncheck_replace_changes_fingerprint(self):
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		msg = scan_manufacture_reconciliation(job_card=JC)
		cands = msg.get("manufacture_batch_replace_candidates") or []
		if not cands:
			self.skipTest("no replace candidate")
		c = cands[0]
		with_app = rebuild_manufacture_preview(
			job_card=JC,
			plan={
				"dispositions": [],
				"stamp_mode": "HISTORICAL",
				"manufacture_batch_replace_approvals": [_replace_approval(c)],
			},
		)
		without = rebuild_manufacture_preview(
			job_card=JC,
			plan={
				"dispositions": [],
				"stamp_mode": "HISTORICAL",
				"manufacture_batch_replace_approvals": [],
			},
		)
		self.assertNotEqual(with_app.get("fingerprint"), without.get("fingerprint"))

	def test_px08_consumed_qty_change_rebuilds_fingerprint(self):
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		a = rebuild_manufacture_preview(
			job_card=JC,
			plan={
				"dispositions": [
					{
						"item_code": ITEM,
						"batch_no": B503,
						"proposed_consumed": QTY,
						"proposed_scrap": 0,
						"proposed_return": 0,
						"proposed_still_in_wip": 0,
					}
				],
				"stamp_mode": "HISTORICAL",
			},
		)
		b = rebuild_manufacture_preview(
			job_card=JC,
			plan={
				"dispositions": [
					{
						"item_code": ITEM,
						"batch_no": B503,
						"proposed_consumed": QTY - 1,
						"proposed_scrap": 0,
						"proposed_return": 0,
						"proposed_still_in_wip": 0,
					}
				],
				"stamp_mode": "HISTORICAL",
			},
		)
		self.assertNotEqual(a.get("fingerprint"), b.get("fingerprint"))

	def test_px09_scrap_return_still_invalidate_fingerprint(self):
		"""Valid disposition edits (consume/scrap/return/still) change authoritative fingerprint."""
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		cases = [
			{
				"item_code": ITEM,
				"batch_no": B503,
				"proposed_consumed": QTY,
				"proposed_scrap": 0,
				"proposed_return": 0,
				"proposed_still_in_wip": 0,
			},
			{
				"item_code": ITEM,
				"batch_no": B503,
				"proposed_consumed": QTY - 1,
				"proposed_scrap": 1,
				"proposed_return": 0,
				"proposed_still_in_wip": 0,
			},
			{
				"item_code": ITEM,
				"batch_no": B503,
				"proposed_consumed": QTY - 1,
				"proposed_scrap": 0,
				"proposed_return": 1,
				"proposed_still_in_wip": 0,
			},
			{
				"item_code": ITEM,
				"batch_no": B503,
				"proposed_consumed": QTY - 1,
				"proposed_scrap": 0,
				"proposed_return": 0,
				"proposed_still_in_wip": 1,
			},
		]
		fps = {
			rebuild_manufacture_preview(
				job_card=JC, plan={"dispositions": [d], "stamp_mode": "HISTORICAL"}
			).get("fingerprint")
			for d in cases
		}
		self.assertGreaterEqual(len(fps), 3)

	def test_px12_preview_fingerprint_matches_planner(self):
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		msg = scan_manufacture_reconciliation(job_card=JC)
		cands = msg.get("manufacture_batch_replace_candidates") or []
		if not cands:
			self.skipTest("no candidate")
		c = cands[0]
		approvals = [_replace_approval(c)]
		a = rebuild_manufacture_preview(
			job_card=JC,
			plan={
				"dispositions": [],
				"stamp_mode": "HISTORICAL",
				"manufacture_batch_replace_approvals": approvals,
			},
		)
		b = build_manufacture_plan(
			JC,
			dispositions=[],
			stamp_mode="HISTORICAL",
			manufacture_batch_replace_approvals=approvals,
		)
		self.assertEqual(a.get("fingerprint"), b.get("fingerprint"))

	def test_px13_dry_payload_fingerprint_matches_preview(self):
		"""Dry/Apply worker planner fingerprint must equal rebuild_manufacture_preview."""
		if not _is_pre_repair_fixture():
			self.skipTest("need pre-repair PO-JOB08631 fixture")
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
			_normalize_plan,
		)

		msg = scan_manufacture_reconciliation(job_card=JC)
		cands = msg.get("manufacture_batch_replace_candidates") or []
		if not cands:
			self.skipTest("no candidate")
		c = cands[0]
		plan_in = {
			"dispositions": [],
			"stamp_mode": "HISTORICAL",
			"manufacture_batch_replace_approvals": [_replace_approval(c)],
		}
		preview = rebuild_manufacture_preview(job_card=JC, plan=plan_in)
		norm = _normalize_plan(plan_in)
		worker = build_manufacture_plan(
			JC,
			dispositions=norm.get("dispositions"),
			merge_documents=norm.get("merge_documents"),
			stamp_mode=norm.get("stamp_mode") or "HISTORICAL",
			merge_material_issues=norm.get("merge_material_issues"),
			batch_offset_approvals=norm.get("batch_offset_approvals"),
			partial_batch_offset_approvals=norm.get("partial_batch_offset_approvals"),
			manufacture_batch_replace_approvals=norm.get(
				"manufacture_batch_replace_approvals"
			),
		)
		self.assertEqual(preview.get("fingerprint"), worker.get("fingerprint"))
	def test_px15_rebuild_api_server_authoritative(self):
		if not frappe.db.exists("Job Card", JC):
			self.skipTest("missing JC")
		out = rebuild_manufacture_preview(
			job_card=JC, plan={"dispositions": [], "stamp_mode": "HISTORICAL"}
		)
		self.assertIn("plan", out)
		self.assertIn("canonical_manufacture", out)
		self.assertIn("fingerprint", out)


if __name__ == "__main__":
	unittest.main()
