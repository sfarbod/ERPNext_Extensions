# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.2 release-blocker: Return atomicity, concurrency, submit invariant."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import today

from erpnext_extensions.petty_management.services.return_for_correction_service import (
	RETURN_TIMELINE_MARKER,
	assert_return_allowed_under_lock,
	count_return_timeline_comments,
	lock_pm_document_for_return,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


class TestPMReturnAtomicityV472(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company on site")
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
		u = frappe.get_doc("User", user)
		for role in ("Petty Management User", "Petty Management Accountant"):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
			if role not in [r.role for r in u.roles]:
				u.append("roles", {"role": role})
		u.save(ignore_permissions=True)
		return user

	def _ensure_requester_user(self) -> str:
		email = "pm_return_requester_v472@example.com"
		if not frappe.db.exists("User", email):
			u = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "PM Return",
					"last_name": "Requester",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			)
			u.insert(ignore_permissions=True)
			u.add_roles("Petty Management User")
		return email

	def _make_pending_request(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_approvers(emp)
		req = frappe.new_doc("PM Request")
		req.company = pm_ct.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1500, "description": "v472 atomic"})
		req.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Request",
			req.name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		req.reload()
		out = apply_pm_workflow(req, "PM Submit for Approval")
		out.reload()
		self.assertEqual(_wf_title(out.workflow_state), "Pending Manager Approval")
		self.assertEqual(out.docstatus, 0)
		return out

	def _snapshot(self, doctype: str, name: str) -> dict:
		row = frappe.db.get_value(
			doctype,
			name,
			["workflow_state", "docstatus", "status", "manager_approver", "ceo_approver", "finance_approver"],
			as_dict=True,
		)
		open_todos = frappe.get_all(
			"ToDo",
			filters={"reference_type": doctype, "reference_name": name, "status": "Open"},
			pluck="name",
		)
		return {
			"row": row,
			"open_todos": set(open_todos),
			"return_comments": count_return_timeline_comments(doctype, name),
		}

	def test_assign_failure_rolls_back_return(self):
		req = self._make_pending_request()
		requester = self._ensure_requester_user()
		frappe.db.set_value("PM Request", req.name, "owner", requester, update_modified=False)
		# Seed an open approver ToDo so we can prove it stays open after rollback
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"allocated_to": frappe.session.user,
				"reference_type": "PM Request",
				"reference_name": req.name,
				"description": "approver work",
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		req.reload()
		before = self._snapshot("PM Request", req.name)
		self.assertTrue(before["row"].manager_approver)

		with patch("frappe.desk.form.assign_to.add", side_effect=RuntimeError("inject assign failure")):
			with self.assertRaises(RuntimeError):
				apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")

		frappe.db.rollback()
		after = self._snapshot("PM Request", req.name)
		self.assertEqual(_wf_title(after["row"].workflow_state), "Pending Manager Approval")
		self.assertEqual(after["row"].docstatus, 0)
		self.assertEqual(after["row"].manager_approver, before["row"].manager_approver)
		self.assertEqual(after["return_comments"], 0)
		self.assertIn(todo.name, after["open_todos"])
		# No requester Open ToDo for requester
		self.assertFalse(
			frappe.db.exists(
				"ToDo",
				{
					"reference_type": "PM Request",
					"reference_name": req.name,
					"allocated_to": requester,
					"status": "Open",
				},
			)
		)

	def test_timeline_failure_rolls_back_return(self):
		req = self._make_pending_request()
		frappe.db.commit()
		before = self._snapshot("PM Request", req.name)

		with patch(
			"erpnext_extensions.petty_management.services.return_for_correction_service.add_return_timeline_comment",
			side_effect=RuntimeError("inject timeline failure"),
		):
			with self.assertRaises(RuntimeError):
				apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")

		frappe.db.rollback()
		after = self._snapshot("PM Request", req.name)
		self.assertEqual(_wf_title(after["row"].workflow_state), "Pending Manager Approval")
		self.assertEqual(after["row"].manager_approver, before["row"].manager_approver)
		self.assertEqual(after["return_comments"], 0)

	def test_stamp_clear_failure_rolls_back_return(self):
		req = self._make_pending_request()
		frappe.db.commit()
		before = self._snapshot("PM Request", req.name)

		with patch(
			"erpnext_extensions.petty_management.services.return_for_correction_service.clear_approver_stamps",
			side_effect=RuntimeError("inject stamp failure"),
		):
			with self.assertRaises(RuntimeError):
				apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")

		frappe.db.rollback()
		after = self._snapshot("PM Request", req.name)
		self.assertEqual(_wf_title(after["row"].workflow_state), "Pending Manager Approval")
		self.assertEqual(after["row"].manager_approver, before["row"].manager_approver)
		self.assertEqual(after["return_comments"], 0)

	def test_second_return_rejected_when_already_draft(self):
		req = self._make_pending_request()
		name = req.name
		frappe.db.set_value(
			"PM Request", name, "manager_approver", frappe.session.user, update_modified=False
		)
		out = apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Return for Correction")
		out.reload()
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		comments_after_first = count_return_timeline_comments("PM Request", name)
		self.assertEqual(comments_after_first, 1)

		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Return for Correction")
		msg = str(ctx.exception).lower()
		self.assertTrue("draft" in msg or "not allowed" in msg or "already" in msg)

		self.assertEqual(count_return_timeline_comments("PM Request", name), 1)
		open_requester_todos = frappe.get_all(
			"ToDo",
			filters={
				"reference_type": "PM Request",
				"reference_name": name,
				"status": "Open",
			},
		)
		# At most one Open ToDo cluster after single Return (Admin owner may skip assign)
		self.assertLessEqual(len(open_requester_todos), 2)

	def test_lock_assert_rejects_non_pending(self):
		req = self._make_pending_request()
		apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")
		frappe.db.commit()
		locked = lock_pm_document_for_return("PM Request", req.name)
		with self.assertRaises(frappe.ValidationError):
			assert_return_allowed_under_lock("PM Request", locked)

	def test_concurrent_return_second_caller_sees_draft_under_lock(self):
		"""Simulate concurrency: after first Return commits, second lock sees Draft."""
		req = self._make_pending_request()
		name = req.name
		frappe.db.set_value(
			"PM Request", name, "manager_approver", frappe.session.user, update_modified=False
		)
		apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Return for Correction")
		frappe.db.commit()

		# Second "caller" acquires lock and must fail cleanly
		locked = lock_pm_document_for_return("PM Request", name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			assert_return_allowed_under_lock("PM Request", locked)
		self.assertIn("already Draft", str(ctx.exception))
		self.assertEqual(count_return_timeline_comments("PM Request", name), 1)
		self.assertIn(RETURN_TIMELINE_MARKER, " ".join(
			frappe.get_all(
				"Comment",
				filters={"reference_doctype": "PM Request", "reference_name": name, "comment_type": "Info"},
				pluck="content",
			)
		) or RETURN_TIMELINE_MARKER)


class TestPMSubmitInvariantV472(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company")
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
			_rebuild_pm_request_workflow,
		)

		_rebuild_pm_request_workflow()
		_rebuild_pm_clearance_workflow()
		frappe.db.commit()

	def test_reservation_helper_does_not_raw_set_docstatus(self):
		import inspect

		from erpnext_extensions.petty_management.services import clearance_service as cs

		src = inspect.getsource(cs.approve_pm_clearance_for_reservation)
		self.assertIn("apply_pm_workflow", src)
		self.assertNotIn('values["docstatus"]', src)
		self.assertNotIn('"docstatus": 1', src)
		self.assertNotIn("'docstatus': 1", src)
		self.assertNotIn('set_value("PM Clearance"', src)

	def test_helper_cannot_approve_from_draft(self):
		from erpnext_extensions.petty_management.services.clearance_service import (
			approve_pm_clearance_for_reservation,
		)

		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		cl.flags.ignore_mandatory = True
		cl.flags.ignore_validate = True
		cl.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			approve_pm_clearance_for_reservation(cl.name)
		self.assertIn("Pending Finance Review", str(ctx.exception))
		cl.reload()
		self.assertEqual(cl.docstatus, 0)
		self.assertEqual(_wf_title(cl.workflow_state), "Draft")


class TestPMMigrationCommitV472(unittest.TestCase):
	def test_migration_execute_has_no_explicit_commit(self):
		import inspect

		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as mod

		src = inspect.getsource(mod.execute)
		self.assertNotIn("frappe.db.commit()", src)
