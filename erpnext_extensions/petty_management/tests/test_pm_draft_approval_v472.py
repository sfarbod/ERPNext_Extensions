# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.2 Draft Approval Until Final Finance Submit."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import today

from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
	assert_no_in_flight_pending_pm_docs,
	count_in_flight_pending_pm_docs,
)
from erpnext_extensions.petty_management.services.draft_approval_guards import (
	assert_pending_not_deletable,
)
from erpnext_extensions.petty_management.services.request_service import request_ready_for_payment_entry
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct


class TestPMDraftApprovalV472(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company on site")
		# Rebuild workflows with Pending*=0 (same as after_migrate / v472)
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
			_rebuild_pm_request_workflow,
			_seed_assignment_rules,
		)

		_rebuild_pm_request_workflow()
		_rebuild_pm_clearance_workflow()
		_seed_assignment_rules()
		frappe.db.commit()

	def _configure_approvers(self, emp: str, user: str | None = None) -> str:
		user = user or frappe.session.user
		frappe.db.set_value("Employee", emp, "expense_approver", user, update_modified=False)
		settings = frappe.get_single("PM Settings")
		settings.db_set("ceo_approver", user, update_modified=False)
		settings.db_set("finance_manager", user, update_modified=False)
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		# Ensure roles for approve path
		u = frappe.get_doc("User", user)
		for role in ("Petty Management User", "Petty Management Accountant"):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
			if role not in [r.role for r in u.roles]:
				u.append("roles", {"role": role})
		u.save(ignore_permissions=True)
		return user

	def _make_draft_request(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_approvers(emp)
		req = frappe.new_doc("PM Request")
		req.company = pm_ct.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1500, "description": "v472"})
		req.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Request",
			req.name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		req.reload()
		return req

	def test_migration_validator_empty_queue(self):
		with patch(
			"erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472.count_in_flight_pending_pm_docs",
			return_value={
				"request_count": 0,
				"clearance_count": 0,
				"request_names": [],
				"clearance_names": [],
			},
		):
			assert_no_in_flight_pending_pm_docs()  # should not throw

	def test_migration_validator_aborts_with_counts(self):
		with patch(
			"erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472.count_in_flight_pending_pm_docs",
			return_value={
				"request_count": 2,
				"clearance_count": 1,
				"request_names": ["REQ-A", "REQ-B"],
				"clearance_names": ["CLR-A"],
			},
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_no_in_flight_pending_pm_docs()
			msg = str(ctx.exception)
			self.assertIn("2", msg)
			self.assertIn("1", msg)
			self.assertIn("REQ-A", msg)

	def test_draft_approval_workflow_detected_after_rebuild(self):
		from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
			is_draft_approval_workflow_applied,
		)

		self.assertTrue(is_draft_approval_workflow_applied())

	def test_execute_already_applied_skips_in_flight_gate(self):
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as mod

		with (
			patch.object(mod, "is_draft_approval_workflow_applied", return_value=True),
			patch.object(mod, "assert_no_in_flight_pending_pm_docs") as assert_gate,
			patch.object(mod, "_rebuild_pm_request_workflow") as rebuild_req,
			patch.object(mod, "_rebuild_pm_clearance_workflow") as rebuild_clr,
			patch.object(mod, "_seed_assignment_rules", return_value=[]),
			patch.object(mod, "_set_site_flag"),
			patch.object(mod, "_clear_site_flag"),
		):
			mod.execute()
			assert_gate.assert_not_called()
			rebuild_req.assert_not_called()
			rebuild_clr.assert_not_called()

	def test_execute_defers_when_in_flight_and_not_applied(self):
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as mod

		with (
			patch.object(mod, "is_draft_approval_workflow_applied", return_value=False),
			patch.object(
				mod,
				"count_in_flight_pending_pm_docs",
				return_value={
					"request_count": 2,
					"clearance_count": 1,
					"request_names": ["REQ-A", "REQ-B"],
					"clearance_names": ["CLR-A"],
				},
			),
			patch.object(mod, "_rebuild_pm_request_workflow") as rebuild_req,
			patch.object(mod, "_rebuild_pm_clearance_workflow") as rebuild_clr,
			patch.object(mod, "_set_site_flag") as set_flag,
			patch.object(mod, "_clear_site_flag"),
		):
			mod.execute()  # must not throw
			rebuild_req.assert_not_called()
			rebuild_clr.assert_not_called()
			self.assertTrue(set_flag.called)
			self.assertEqual(set_flag.call_args[0][0], mod.DEFERRED_FLAG_KEY)

	def test_count_in_flight_returns_dict(self):
		stats = count_in_flight_pending_pm_docs()
		self.assertIn("request_count", stats)
		self.assertIn("clearance_count", stats)
		self.assertIsInstance(stats["request_names"], list)

	def test_submit_for_approval_stays_docstatus_0(self):
		req = self._make_draft_request()
		out = apply_pm_workflow(req, "PM Submit for Approval")
		self.assertEqual(out.docstatus, 0)
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Pending Manager Approval")
		self.assertTrue(out.manager_approver)
		self.assertTrue(out.finance_approver)

	def test_finance_approve_submits_docstatus_1(self):
		req = self._make_draft_request()
		user = frappe.session.user
		out = apply_pm_workflow(req, "PM Submit for Approval")
		# Same user stamped for all stages → auto-skip may hop; force states if needed
		out.reload()
		# Walk approvals until Finance Approved / submitted
		from frappe.model.workflow import get_transitions

		for _ in range(6):
			out.reload()
			title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
			if title == "Finance Approved" and out.docstatus == 1:
				break
			actions = [t.get("action") for t in get_transitions(out)]
			nxt = None
			for cand in ("PM Manager Approve", "PM CEO Approve", "PM Finance Approve"):
				if cand in actions:
					nxt = cand
					break
			if not nxt:
				break
			# Ensure stamp matches session for identity gate
			if nxt == "PM Manager Approve":
				frappe.db.set_value("PM Request", out.name, "manager_approver", user, update_modified=False)
			elif nxt == "PM CEO Approve":
				frappe.db.set_value("PM Request", out.name, "ceo_approver", user, update_modified=False)
			elif nxt == "PM Finance Approve":
				frappe.db.set_value("PM Request", out.name, "finance_approver", user, update_modified=False)
			out.reload()
			out = apply_pm_workflow(out, nxt)

		out.reload()
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Finance Approved")
		self.assertEqual(out.docstatus, 1)

	def test_return_for_correction_same_name_draft(self):
		req = self._make_draft_request()
		name = req.name
		out = apply_pm_workflow(req, "PM Submit for Approval")
		out.reload()
		self.assertEqual(out.docstatus, 0)
		frappe.db.set_value(
			"PM Request", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		out = apply_pm_workflow(out, "PM Return for Correction")
		out.reload()
		self.assertEqual(out.name, name)
		self.assertEqual(out.docstatus, 0)
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Draft")
		self.assertFalse((out.manager_approver or "").strip())
		self.assertEqual((out.status or "").strip(), "Draft")

	def test_edit_lock_while_pending(self):
		req = self._make_draft_request()
		out = apply_pm_workflow(req, "PM Submit for Approval")
		out.reload()
		out.close_reason_detail = "should not save while pending"
		# Ordinary Desk save (no ignore_permissions) must be blocked
		with self.assertRaises(frappe.ValidationError):
			out.save()

	def test_delete_lock_while_pending(self):
		req = self._make_draft_request()
		out = apply_pm_workflow(req, "PM Submit for Approval")
		out.reload()
		with self.assertRaises(frappe.ValidationError):
			assert_pending_not_deletable(out)
		with self.assertRaises(frappe.ValidationError):
			out.delete(ignore_permissions=True)

	def test_pe_blocked_while_pending(self):
		req = self._make_draft_request()
		out = apply_pm_workflow(req, "PM Submit for Approval")
		out.reload()
		ok, reason = request_ready_for_payment_entry(out)
		self.assertFalse(ok)
		self.assertIn("finance approval", (reason or "").lower())

	def test_workflow_states_pending_doc_status_zero(self):
		wf = frappe.get_doc("Workflow", "PM Request Workflow")
		pending_states = [
			s
			for s in wf.states
			if "Pending" in (frappe.db.get_value("Workflow State", s.state, "workflow_state_name") or s.state or "")
		]
		self.assertTrue(pending_states)
		for s in pending_states:
			self.assertEqual(str(s.doc_status), "0")
		actions = {t.action for t in wf.transitions}
		self.assertIn("PM Return for Correction", actions)

	def test_return_timeline_comment_and_resubmit(self):
		req = self._make_draft_request()
		name = req.name
		out = apply_pm_workflow(req, "PM Submit for Approval")
		frappe.db.set_value(
			"PM Request", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		frappe.flags.pm_return_reason = "fix amount"
		try:
			out = apply_pm_workflow(out, "PM Return for Correction")
		finally:
			frappe.flags.pm_return_reason = None
		out.reload()
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "PM Request", "reference_name": name, "comment_type": "Info"},
			pluck="content",
			order_by="creation desc",
			limit=5,
		)
		joined = " ".join(comments)
		self.assertIn("Returned for correction by", joined)
		self.assertIn("Pending Manager Approval", joined)
		self.assertIn("fix amount", joined)

		# Resubmit restarts at Manager with fresh stamps
		out = apply_pm_workflow(out, "PM Submit for Approval")
		out.reload()
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Pending Manager Approval")
		self.assertEqual(out.name, name)
		self.assertTrue(out.manager_approver)

	def test_clearance_pending_stays_draft_and_return(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_approvers(emp)
		pm_request, _pe = pm_ct._fund_pm_request(emp, 10_000.0)
		fa = resolve_workflow_state_link("Finance Approved")
		frappe.db.set_value(
			"PM Request",
			pm_request,
			{"workflow_state": fa, "status": "Paid", "payment_status": "Paid"},
			update_modified=False,
		)
		pi = pm_ct._make_pi_outstanding(1_000)
		pi.insert(ignore_permissions=True)
		pi.submit()
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 1000,
			},
		)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Request",
				"pm_request": pm_request,
				"allocated_amount": 1000,
			},
		)
		cl.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		cl.reload()
		out = apply_pm_workflow(cl, "PM Submit Finance Review")
		self.assertEqual(out.docstatus, 0)
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Pending Manager Approval")
		frappe.db.set_value(
			"PM Clearance", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		name = out.name
		out = apply_pm_workflow(out, "PM Return for Correction")
		out.reload()
		self.assertEqual(out.name, name)
		self.assertEqual(out.docstatus, 0)
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Draft")
		self.assertFalse((out.manager_approver or "").strip())

	def test_clearance_workflow_pending_doc_status_zero(self):
		wf = frappe.get_doc("Workflow", "PM Clearance Workflow")
		for s in wf.states:
			title = frappe.db.get_value("Workflow State", s.state, "workflow_state_name") or s.state
			if title and "Pending" in title:
				self.assertEqual(str(s.doc_status), "0")
			if title == "Approved":
				self.assertEqual(str(s.doc_status), "1")
		self.assertIn("PM Return for Correction", {t.action for t in wf.transitions})

	def test_cutover_complete_on_site(self):
		from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
			is_pm_draft_approval_v472_cutover_complete,
		)

		self.assertTrue(is_pm_draft_approval_v472_cutover_complete())

	def test_cutover_not_complete_when_deferred_flag_set(self):
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as mod

		with patch.object(
			mod,
			"_get_site_flag",
			side_effect=lambda k: {"pm_draft_approval_v472_deferred": {"x": 1}}.get(k),
		):
			self.assertFalse(mod.is_pm_draft_approval_v472_cutover_complete())

	def test_v483_patch_aborts_without_v472_cutover(self):
		from erpnext_extensions.patches.post_model_sync import (
			migrate_pm_clearance_return_remarks_v483 as v483,
		)
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as v472

		with patch.object(v472, "is_pm_draft_approval_v472_cutover_complete", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				v483.execute()

	def test_v483_patch_runs_when_v472_cutover_complete(self):
		from erpnext_extensions.patches.post_model_sync import (
			migrate_pm_clearance_return_remarks_v483 as v483,
		)
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as v472

		with patch.object(v472, "is_pm_draft_approval_v472_cutover_complete", return_value=True):
			v483.execute()

	def test_cutover_complete_on_site(self):
		from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
			is_pm_draft_approval_v472_cutover_complete,
		)

		self.assertTrue(is_pm_draft_approval_v472_cutover_complete())

	def test_cutover_not_complete_when_deferred_flag_set(self):
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as mod

		with (
			patch.object(mod, "_get_site_flag", side_effect=lambda k: {"pm_draft_approval_v472_deferred": {"x": 1}}.get(k)),
			patch.object(mod, "is_pm_draft_approval_v472_cutover_complete", wraps=mod.is_pm_draft_approval_v472_cutover_complete),
		):
			self.assertFalse(mod.is_pm_draft_approval_v472_cutover_complete())

	def test_v483_patch_aborts_without_v472_cutover(self):
		from erpnext_extensions.patches.post_model_sync import (
			migrate_pm_clearance_return_remarks_v483 as v483,
		)
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as v472

		with patch.object(v472, "is_pm_draft_approval_v472_cutover_complete", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				v483.execute()

	def test_v483_patch_runs_when_v472_cutover_complete(self):
		from erpnext_extensions.patches.post_model_sync import (
			migrate_pm_clearance_return_remarks_v483 as v483,
		)
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as v472

		with patch.object(v472, "is_pm_draft_approval_v472_cutover_complete", return_value=True):
			v483.execute()
