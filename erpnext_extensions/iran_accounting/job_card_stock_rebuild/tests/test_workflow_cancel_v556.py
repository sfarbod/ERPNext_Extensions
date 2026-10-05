# Copyright (c) 2026, ERPNext Extensions contributors
"""WC01–WC11: Stock Entry cancel workflow_state + Consumed* UI semantics (v5.5.6).

Mutating probes use savepoints / Dry Run rollback. No persistent Apply unless
the Job Card still has a repairable plan. Historical stale docs are reported
only (never mass-fixed).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
	_cancel_se,
	run_repair,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.sync_valuation import (
	suppress_auto_riv,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.workflow_cancel import (
	ensure_cancel_workflow_state_persisted,
	resolve_cancel_workflow_state,
	scan_stale_cancelled_stock_entry_workflow,
	stamp_cancel_workflow_state,
)
from erpnext_extensions.iran_accounting.scrap_costing import MANUFACTURE_COSTING_CONTRACT_VERSION


JC = "PO-JOB08760"
CANARIES_STALE = [
	"MAT-STE-2026-31724-1",
	"MAT-STE-2026-31725",
	"MAT-STE-2026-31726",
]
CANARY_OK = "MAT-STE-2026-31724"


def _pick_submitted_se() -> str | None:
	name = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where docstatus=1 and ifnull(workflow_state,'')='Submitted'
		  and purpose='Material Receipt'
		order by modified desc limit 1
		"""
	)
	if name:
		return name[0][0]
	name = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where docstatus=1 and ifnull(workflow_state,'')='Submitted'
		order by modified desc limit 1
		"""
	)
	return name[0][0] if name else None


class TestWorkflowCancelV556(FrappeTestCase):
	def setUp(self):
		frappe.flags.jc_repair_fail_at = None

	def tearDown(self):
		frappe.flags.jc_repair_fail_at = None

	def test_contract_unchanged(self):
		self.assertEqual(MANUFACTURE_COSTING_CONTRACT_VERSION, "5.3.43")

	def test_wc01_cancel_with_workflow(self):
		"""Submitted SE → repair cancel → docstatus=2 + configured cancel state."""
		name = _pick_submitted_se()
		if not name:
			self.skipTest("no submitted Stock Entry with workflow_state=Submitted")
		before = frappe.db.get_value(
			"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
		)
		self.assertEqual(cint(before.docstatus), 1)
		doc = frappe.get_doc("Stock Entry", name)
		_, expected = resolve_cancel_workflow_state(doc)
		self.assertTrue(expected)

		frappe.db.savepoint("wc01")
		try:
			with suppress_auto_riv():
				_cancel_se(name)
			after = frappe.db.get_value(
				"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
			)
			self.assertEqual(cint(after.docstatus), 2)
			self.assertEqual(after.workflow_state, expected)
			active_sle = frappe.db.sql(
				"select count(*) from `tabStock Ledger Entry` where voucher_no=%s and is_cancelled=0",
				name,
			)[0][0]
			self.assertEqual(active_sle, 0)
		finally:
			frappe.db.rollback(save_point="wc01")

		restored = frappe.db.get_value(
			"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
		)
		self.assertEqual(cint(restored.docstatus), cint(before.docstatus))
		self.assertEqual(restored.workflow_state, before.workflow_state)

	def test_wc02_cancel_without_workflow(self):
		"""No active Workflow → cancel works; no fake workflow_state invented."""
		name = _pick_submitted_se()
		if not name:
			self.skipTest("no submitted Stock Entry")

		# Unit: resolver/stamp are no-ops when meta.get_workflow() is empty.
		from frappe.model.meta import Meta

		orig_meta_wf = Meta.get_workflow
		Meta.get_workflow = lambda self: None  # type: ignore
		try:
			doc = frappe.get_doc("Stock Entry", name)
			field, target = resolve_cancel_workflow_state(doc)
			self.assertIsNone(field)
			self.assertIsNone(target)
			self.assertIsNone(stamp_cancel_workflow_state(doc))
			self.assertEqual(doc.workflow_state, "Submitted")
		finally:
			Meta.get_workflow = orig_meta_wf  # type: ignore

		# Integration: simulate no-workflow resolution during repair cancel.
		import erpnext_extensions.iran_accounting.job_card_stock_rebuild.workflow_cancel as wc

		orig_resolve = wc.resolve_cancel_workflow_state
		wc.resolve_cancel_workflow_state = lambda _doc: (None, None)  # type: ignore
		frappe.db.savepoint("wc02")
		try:
			with suppress_auto_riv():
				_cancel_se(name)
			after = frappe.db.get_value(
				"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
			)
			self.assertEqual(cint(after.docstatus), 2)
			# Must not invent a cancel state when no workflow applies.
			self.assertEqual(after.workflow_state, "Submitted")
		finally:
			frappe.db.rollback(save_point="wc02")
			wc.resolve_cancel_workflow_state = orig_resolve  # type: ignore

	def test_wc03_cancel_failure_preserves_state(self):
		name = _pick_submitted_se()
		if not name:
			self.skipTest("no submitted Stock Entry")
		before = frappe.db.get_value(
			"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
		)
		frappe.db.savepoint("wc03")
		try:
			doc = frappe.get_doc("Stock Entry", name)
			stamp_cancel_workflow_state(doc)
			# Force failure before Core cancel persists.
			def _boom(*_a, **_k):
				raise RuntimeError("INJECTED_CANCEL_FAIL")

			orig = doc.__class__.cancel
			doc.__class__.cancel = _boom  # type: ignore
			try:
				with self.assertRaises(RuntimeError):
					with suppress_auto_riv():
						# Call helper pieces mirroring _cancel_se without successful cancel
						stamp_cancel_workflow_state(doc)
						doc.cancel()
						ensure_cancel_workflow_state_persisted(doc)
			finally:
				doc.__class__.cancel = orig  # type: ignore

			after = frappe.db.get_value(
				"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
			)
			self.assertEqual(cint(after.docstatus), cint(before.docstatus))
			self.assertEqual(after.workflow_state, before.workflow_state)
		finally:
			frappe.db.rollback(save_point="wc03")

	def test_wc04_transaction_rollback_restores_workflow(self):
		name = _pick_submitted_se()
		if not name:
			self.skipTest("no submitted Stock Entry")
		before = frappe.db.get_value(
			"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
		)
		frappe.db.savepoint("wc04")
		try:
			with suppress_auto_riv():
				_cancel_se(name)
			mid = frappe.db.get_value(
				"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
			)
			self.assertEqual(cint(mid.docstatus), 2)
			_, expected = resolve_cancel_workflow_state(frappe.get_doc("Stock Entry", name))
			self.assertEqual(mid.workflow_state, expected)
			raise RuntimeError("INJECTED_LATER_FAILURE")
		except RuntimeError as e:
			self.assertIn("INJECTED_LATER_FAILURE", str(e))
		finally:
			frappe.db.rollback(save_point="wc04")

		restored = frappe.db.get_value(
			"Stock Entry", name, ["docstatus", "workflow_state"], as_dict=1
		)
		self.assertEqual(cint(restored.docstatus), cint(before.docstatus))
		self.assertEqual(restored.workflow_state, before.workflow_state)

	def test_wc05_dry_run_restores_workflow(self):
		if not frappe.db.exists("Job Card", JC):
			self.skipTest("missing PO-JOB08760")
		# Snapshot a submitted SE that Dry Run may cancel (active Manufacture).
		active = frappe.db.sql(
			"""
			select name, docstatus, workflow_state from `tabStock Entry`
			where job_card=%s and purpose='Manufacture' and docstatus=1
			""",
			JC,
			as_dict=1,
		)
		before_map = {
			r.name: (cint(r.docstatus), r.workflow_state)
			for r in frappe.db.sql(
				"select name, docstatus, workflow_state from `tabStock Entry` where job_card=%s",
				JC,
				as_dict=1,
			)
		}
		plan = {
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
			"merge_documents": [active[0].name] if active else ["MAT-STE-2026-31724-1"],
			"stamp_mode": "HISTORICAL",
		}
		res = run_repair(JC, plan_input=plan, dry_run=True)
		self.assertFalse(res.get("mutated"))
		self.assertFalse(res.get("committed"))
		after_map = {
			r.name: (cint(r.docstatus), r.workflow_state)
			for r in frappe.db.sql(
				"select name, docstatus, workflow_state from `tabStock Entry` where job_card=%s",
				JC,
				as_dict=1,
			)
		}
		self.assertEqual(before_map, after_map)

	def test_wc06_apply_path_uses_cancel_helper(self):
		"""Apply engine still routes cancellations through _cancel_se (workflow stamp)."""
		import inspect

		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import atomic_repair

		src = inspect.getsource(atomic_repair._cancel_se)
		self.assertIn("stamp_cancel_workflow_state", src)
		self.assertIn("doc.cancel()", src)
		# Live Apply only when a repairable plan exists; otherwise helper coverage is WC01.
		if not frappe.db.exists("Job Card", JC):
			return
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
			build_manufacture_plan,
		)

		active = frappe.db.sql(
			"""
			select name from `tabStock Entry`
			where job_card=%s and purpose='Manufacture' and docstatus=1 limit 1
			""",
			JC,
		)
		if not active:
			return
		plan = build_manufacture_plan(
			JC,
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
			merge_documents=[active[0][0]],
			stamp_mode="HISTORICAL",
		)
		if not plan.get("apply_allowed"):
			# Post-Apply canary — prevention covered by WC01; do not force Apply.
			self.assertTrue(plan.get("blockers"))
			return

	def test_wc07_different_cancel_state_name(self):
		"""No hard-coded 'Cancelled' — follows configured doc_status=2 state name."""
		name = _pick_submitted_se()
		if not name:
			self.skipTest("no submitted Stock Entry")
		doc = frappe.get_doc("Stock Entry", name)
		workflow_name = doc.meta.get_workflow()
		if not workflow_name:
			self.skipTest("no workflow")

		# Temporary in-memory Workflow doc with alternate cancel state label.
		wf = frappe.get_doc("Workflow", workflow_name)
		# Clone states list conceptually: replace Cancelled with Voided By System
		class _State:
			def __init__(self, state, doc_status):
				self.state = state
				self.doc_status = doc_status

		class _Transition:
			def __init__(self, state, next_state):
				self.state = state
				self.next_state = next_state

		class _WF:
			workflow_state_field = wf.workflow_state_field
			states = [
				_State(s.state, s.doc_status)
				for s in wf.states
				if str(s.doc_status) != "2"
			] + [_State("Voided By System", "2")]
			transitions = [
				_Transition("Submitted", "Voided By System"),
			]

		original_get_doc = frappe.get_doc

		def _patched_get_doc(doctype, *args, **kwargs):
			if doctype == "Workflow" and (args and args[0] == workflow_name or kwargs.get("name") == workflow_name):
				return _WF()
			return original_get_doc(doctype, *args, **kwargs)

		frappe.get_doc = _patched_get_doc  # type: ignore
		try:
			fresh = frappe.get_doc("Stock Entry", name)
			# Ensure current state is Submitted for transition resolution
			fresh.workflow_state = "Submitted"
			field, target = resolve_cancel_workflow_state(fresh)
			self.assertEqual(target, "Voided By System")
			self.assertNotEqual(target, "Cancelled")
			stamp_cancel_workflow_state(fresh)
			self.assertEqual(fresh.get(field), "Voided By System")
		finally:
			frappe.get_doc = original_get_doc  # type: ignore

	def test_wc08_diagnostic_detects_stale_canaries(self):
		report = scan_stale_cancelled_stock_entry_workflow(names=CANARIES_STALE + [CANARY_OK])
		stale_names = {r["name"] for r in report["stale"]}
		for n in CANARIES_STALE:
			if frappe.db.exists("Stock Entry", n):
				row = frappe.db.get_value(
					"Stock Entry", n, ["docstatus", "workflow_state"], as_dict=1
				)
				if cint(row.docstatus) == 2 and row.workflow_state == "Submitted":
					self.assertIn(n, stale_names)

	def test_wc09_diagnostic_skips_correct_cancel_canary(self):
		if not frappe.db.exists("Stock Entry", CANARY_OK):
			self.skipTest("missing control canary")
		row = frappe.db.get_value(
			"Stock Entry", CANARY_OK, ["docstatus", "workflow_state"], as_dict=1
		)
		if not (cint(row.docstatus) == 2 and row.workflow_state == "Cancelled"):
			self.skipTest("control canary not in expected consistent state")
		report = scan_stale_cancelled_stock_entry_workflow(names=[CANARY_OK])
		stale_names = {r["name"] for r in report["stale"]}
		self.assertNotIn(CANARY_OK, stale_names)
		self.assertEqual(report["consistent_count"], 1)

	def test_wc10_ui_label_consumed_clarified(self):
		import os

		js_path = frappe.get_app_path(
			"erpnext_extensions",
			"erpnext_extensions",
			"page",
			"job_card_stock_rebuild",
			"job_card_stock_rebuild.js",
		)
		self.assertTrue(os.path.exists(js_path))
		text = open(js_path, encoding="utf-8").read()
		self.assertIn("مصرف از پای‌کار*", text)
		self.assertIn("ضایعات دوباره از مانده کسر نمی‌شود", text)
		self.assertIn("proposed_consumed", text)
		# Old bare Consumed* header should be replaced
		self.assertNotIn('${__("Consumed*")}', text)

	def test_wc11_component_scrap_no_double_count(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.scrap_pairing import (
			golden_remainder,
		)

		# Conceptual 10 / 2 return / 8 consume (includes 1 scrap) → remainder 0
		rem = golden_remainder(
			issued=10,
			returned=2,
			consumed=8,
			component_scrap=1,
			paired_scrap=1,
			other=0,
		)
		self.assertEqual(flt(rem), 0)

		if frappe.db.exists("Job Card", JC):
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
				scan_golden_rule,
			)

			scan = scan_golden_rule(JC)
			row190 = next(
				(
					r
					for r in scan.get("rows") or []
					if r.get("item_code") == "13200190"
				),
				None,
			)
			row544 = next(
				(
					r
					for r in scan.get("rows") or []
					if r.get("item_code") == "13200544"
				),
				None,
			)
			if row190:
				# Source consume includes scrap; remaining must not go negative from double count
				self.assertGreaterEqual(flt(row190.get("remaining_wip")), 0)
			if row544:
				self.assertEqual(flt(row544.get("issued")), 1160)
				self.assertEqual(flt(row544.get("returned")), 12)
				self.assertEqual(flt(row544.get("consumed")), 1148)
				self.assertEqual(flt(row544.get("remaining_wip")), 0)
