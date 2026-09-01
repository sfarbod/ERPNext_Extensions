# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.4 — repeated Return for Correction / resubmit cycles."""

from __future__ import annotations

import unittest

import frappe
from frappe.model.workflow import get_transitions
from frappe.utils import today

from erpnext_extensions.petty_management.services.approver_stamp_service import (
	stamp_pm_clearance_approvers,
)
from erpnext_extensions.petty_management.services.return_for_correction_service import (
	count_return_timeline_comments,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct

REVIEWER_ROLE = "Petty Management Clearance Reviewer"


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _ensure_user(email: str, roles: list[str]) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0][:30],
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.roles = []
	for role in roles:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		user.append("roles", {"role": role})
	user.enabled = 1
	user.save(ignore_permissions=True)


class TestPMRepeatedReturnV504(unittest.TestCase):
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

		cls.mgr = "pm_v504_mgr@example.com"
		cls.ceo = "pm_v504_ceo@example.com"
		cls.fin = "pm_v504_fin@example.com"
		cls.holder = "pm_v504_holder@example.com"
		cls.reviewer = "pm_v504_rev@example.com"
		desk = ["Accounts User", "Employee", "Desk User"]
		_ensure_user(cls.mgr, ["Petty Management User", "Expense Approver", *desk])
		_ensure_user(cls.ceo, ["Petty Management User", *desk])
		_ensure_user(cls.fin, ["Petty Management Accountant", "Petty Management User", *desk])
		_ensure_user(cls.holder, ["Petty Management User", *desk])
		_ensure_user(cls.reviewer, [REVIEWER_ROLE, *desk])

		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("ceo_approver", cls.ceo, update_modified=False)
		settings.db_set("finance_manager", cls.fin, update_modified=False)
		settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)

	def _new_request(self) -> str:
		emp = pm_ct._make_employee()
		frappe.db.set_value(
			"Employee", emp, {"expense_approver": self.mgr, "user_id": self.holder}, update_modified=False
		)
		pm_ct._make_holder(emp)
		req = frappe.new_doc("PM Request")
		req.company = pm_ct.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1500, "description": "v504"})
		req.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Request",
			req.name,
			{"workflow_state": resolve_workflow_state_link("Draft"), "owner": self.holder},
			update_modified=False,
		)
		return req.name

	def _submit(self, name: str) -> None:
		frappe.set_user(self.holder)
		apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Submit for Approval")
		frappe.set_user("Administrator")

	def _return_as(self, name: str, user: str) -> None:
		frappe.set_user(user)
		apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Return for Correction")
		frappe.set_user("Administrator")

	def _approve_as(self, name: str, user: str, action: str) -> None:
		frappe.set_user(user)
		apply_pm_workflow(frappe.get_doc("PM Request", name), action)
		frappe.set_user("Administrator")

	def _cycle_submit_return(self, name: str, actor: str, cycles: int = 1) -> None:
		for _ in range(cycles):
			self._submit(name)
			doc = frappe.get_doc("PM Request", name)
			self.assertTrue(doc.manager_approver)
			self._return_as(name, actor)
			doc.reload()
			self.assertEqual(_wf_title(doc.workflow_state), "Draft")
			self.assertFalse(doc.manager_approver)

	def test_request_manager_return_three_cycles(self):
		name = self._new_request()
		self._cycle_submit_return(name, self.mgr, cycles=3)
		self.assertEqual(count_return_timeline_comments("PM Request", name), 3)

	def test_request_ceo_return_two_cycles(self):
		name = self._new_request()
		for _ in range(2):
			self._submit(name)
			self._approve_as(name, self.mgr, "PM Manager Approve")
			doc = frappe.get_doc("PM Request", name)
			self.assertEqual(_wf_title(doc.workflow_state), "Pending CEO Approval")
			self._return_as(name, self.ceo)
			doc.reload()
			self.assertEqual(_wf_title(doc.workflow_state), "Draft")
		self.assertEqual(count_return_timeline_comments("PM Request", name), 2)

	def test_request_finance_return_two_cycles(self):
		name = self._new_request()
		for _ in range(2):
			self._submit(name)
			self._approve_as(name, self.mgr, "PM Manager Approve")
			self._approve_as(name, self.ceo, "PM CEO Approve")
			doc = frappe.get_doc("PM Request", name)
			self.assertEqual(_wf_title(doc.workflow_state), "Pending Finance Approval")
			self._return_as(name, self.fin)
			doc.reload()
			self.assertEqual(_wf_title(doc.workflow_state), "Draft")
		self.assertEqual(count_return_timeline_comments("PM Request", name), 2)

	def test_resubmit_restamps_approvers_each_cycle(self):
		name = self._new_request()
		for cycle in range(1, 4):
			self._submit(name)
			doc = frappe.get_doc("PM Request", name)
			self.assertEqual(doc.manager_approver, self.mgr)
			self.assertEqual(doc.ceo_approver, self.ceo)
			self.assertEqual(doc.finance_approver, self.fin)
			frappe.set_user(self.mgr)
			actions = [t.get("action") for t in get_transitions(doc)]
			frappe.set_user("Administrator")
			self.assertIn("PM Return for Correction", actions)
			self._return_as(name, self.mgr)
			doc.reload()
			self.assertIsNone(doc.manager_approver)

	def test_todo_lifecycle_across_two_cycles(self):
		name = self._new_request()
		self._submit(name)
		frappe.set_user(self.mgr)
		doc = frappe.get_doc("PM Request", name)
		first_actions = [t.get("action") for t in get_transitions(doc)]
		frappe.set_user("Administrator")
		self.assertTrue(
			frappe.db.exists(
				"ToDo",
				{
					"reference_type": "PM Request",
					"reference_name": name,
					"allocated_to": self.mgr,
					"status": "Open",
				},
			)
			or "PM Return for Correction" in first_actions
		)
		self._return_as(name, self.mgr)
		self.assertFalse(
			frappe.db.exists(
				"ToDo",
				{
					"reference_type": "PM Request",
					"reference_name": name,
					"allocated_to": self.mgr,
					"status": "Open",
				},
			)
		)
		self.assertTrue(
			frappe.db.exists(
				"ToDo",
				{
					"reference_type": "PM Request",
					"reference_name": name,
					"allocated_to": self.holder,
					"status": "Open",
				},
			)
		)
		self._submit(name)
		frappe.set_user(self.mgr)
		doc = frappe.get_doc("PM Request", name)
		self.assertIn("PM Return for Correction", [t.get("action") for t in get_transitions(doc)])
		frappe.set_user("Administrator")

	def test_auto_skip_same_user_does_not_block_second_return(self):
		settings = frappe.get_single("PM Settings")
		settings.db_set("ceo_approver", self.mgr, update_modified=False)
		try:
			name = self._new_request()
			for _ in range(2):
				self._submit(name)
				doc = frappe.get_doc("PM Request", name)
				title = _wf_title(doc.workflow_state)
				self.assertIn(title, ("Pending Manager Approval", "Pending Finance Approval"))
				frappe.set_user(self.mgr)
				self.assertIn(
					"PM Return for Correction",
					[t.get("action") for t in get_transitions(doc)],
				)
				frappe.set_user("Administrator")
				self._return_as(name, self.mgr)
		finally:
			settings.db_set("ceo_approver", self.ceo, update_modified=False)

	def _return_clearance_as(self, name: str, user: str) -> None:
		frappe.set_user(user)
		apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")
		frappe.set_user("Administrator")

	def _submit_clearance_as(self, name: str, user: str) -> None:
		frappe.set_user(user)
		apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Submit Finance Review")
		frappe.set_user("Administrator")

	def _advance_clearance_to_finance(self, name: str) -> None:
		doc = frappe.get_doc("PM Clearance", name)
		if _wf_title(doc.workflow_state) in ("", "Draft"):
			self._submit_clearance_as(name, self.holder)
		frappe.set_user(self.mgr)
		doc = frappe.get_doc("PM Clearance", name)
		if _wf_title(doc.workflow_state) == "Pending Manager Approval":
			apply_pm_workflow(doc, "PM Manager Approve")
		frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(_wf_title(doc.workflow_state), "Pending Finance Review")

	def _make_clearance_pending_manager(self) -> str | None:
		try:
			pi = pm_ct._make_pi_outstanding(1_000.0)
			pi.insert(ignore_permissions=True)
		except frappe.ValidationError as exc:
			raise unittest.SkipTest(f"PI insert unavailable: {exc}") from exc
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		pm_ct._make_holder(emp)
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 1_000.0,
			},
		)
		cl.flags.ignore_mandatory = True
		cl.flags.ignore_validate = True
		cl.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			{"workflow_state": resolve_workflow_state_link("Draft"), "owner": self.holder},
			update_modified=False,
		)
		stamp_pm_clearance_approvers(cl)
		apply_pm_workflow(cl, "PM Submit Finance Review")
		return cl.name

	def test_clearance_manager_return_three_cycles(self):
		name = self._make_clearance_pending_manager()
		for _ in range(3):
			self._return_clearance_as(name, self.mgr)
			doc = frappe.get_doc("PM Clearance", name)
			self.assertEqual(_wf_title(doc.workflow_state), "Draft")
			self._submit_clearance_as(name, self.holder)
			doc.reload()
			self.assertEqual(_wf_title(doc.workflow_state), "Pending Manager Approval")
		self.assertEqual(count_return_timeline_comments("PM Clearance", name), 3)

	def test_clearance_finance_return_two_cycles(self):
		name = self._make_clearance_pending_manager()
		self._advance_clearance_to_finance(name)
		for _ in range(2):
			self._return_clearance_as(name, self.reviewer)
			doc = frappe.get_doc("PM Clearance", name)
			self.assertEqual(_wf_title(doc.workflow_state), "Draft")
			self._advance_clearance_to_finance(name)
		self.assertEqual(count_return_timeline_comments("PM Clearance", name), 2)
