# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.3 PM Clearance Return visibility + Remarks editability."""

from __future__ import annotations

import unittest

import frappe
from frappe.model.workflow import get_transitions
from frappe.utils import today

from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
	CLEARANCE_PENDING_TITLES,
	_has_return_from_pending_states,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct


class TestPMClearanceV483(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company on site")
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
		)

		_rebuild_pm_clearance_workflow()
		frappe.db.commit()

	def _configure_manager(self, emp: str, user: str | None = None) -> str:
		user = user or frappe.session.user
		frappe.db.set_value("Employee", emp, "expense_approver", user, update_modified=False)
		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("finance_manager", user, update_modified=False)
		u = frappe.get_doc("User", user)
		for role in (
			"Petty Management User",
			"Petty Management Clearance Reviewer",
		):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
			if role not in [r.role for r in u.roles]:
				u.append("roles", {"role": role})
		u.save(ignore_permissions=True)
		return user

	def _draft_clearance(self, emp: str):
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
		return cl

	def test_clearance_workflow_return_from_each_pending_state(self):
		self.assertTrue(
			_has_return_from_pending_states("PM Clearance Workflow", CLEARANCE_PENDING_TITLES)
		)
		wf = frappe.get_doc("Workflow", "PM Clearance Workflow")
		for title in CLEARANCE_PENDING_TITLES:
			state = resolve_workflow_state_link(title)
			actions = {
				t.action
				for t in wf.transitions
				if resolve_workflow_state_link(t.state) == state
			}
			self.assertIn("PM Return for Correction", actions, msg=title)
			self.assertNotIn("PM Reject", actions, msg=title)

	def test_return_visible_pending_manager_and_finance_review(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_manager(emp)
		cl = self._draft_clearance(emp)
		out = apply_pm_workflow(cl, "PM Submit Finance Review")
		self.assertEqual(out.docstatus, 0)
		frappe.db.set_value(
			"PM Clearance", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		actions = {t.get("action") for t in get_transitions(out)}
		self.assertIn("PM Return for Correction", actions)

		out = apply_pm_workflow(out, "PM Manager Approve")
		out.reload()
		actions = {t.get("action") for t in get_transitions(out)}
		self.assertIn("PM Return for Correction", actions)
		self.assertNotIn("PM Reject", actions)

	def test_return_absent_after_finance_approve(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_manager(emp)
		cl = self._draft_clearance(emp)
		out = apply_pm_workflow(cl, "PM Submit Finance Review")
		frappe.db.set_value(
			"PM Clearance", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		out = apply_pm_workflow(out, "PM Manager Approve")
		out.reload()
		out = apply_pm_workflow(out, "PM Finance Approve")
		out.reload()
		self.assertEqual(out.docstatus, 1)
		actions = {t.get("action") for t in get_transitions(out)}
		self.assertNotIn("PM Return for Correction", actions)

	def test_remark_editable_while_pending_docstatus_zero(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_manager(emp)
		cl = self._draft_clearance(emp)
		out = apply_pm_workflow(cl, "PM Submit Finance Review")
		out.reload()
		out.remark = "Pending correction note"
		out.save(ignore_permissions=True)
		out.reload()
		self.assertEqual(out.remark, "Pending correction note")

	def test_remark_read_only_after_finance_approve(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_manager(emp)
		cl = self._draft_clearance(emp)
		out = apply_pm_workflow(cl, "PM Submit Finance Review")
		frappe.db.set_value(
			"PM Clearance", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		out = apply_pm_workflow(out, "PM Manager Approve")
		out.reload()
		out = apply_pm_workflow(out, "PM Finance Approve")
		out.reload()
		out.remark = "Should not save"
		with self.assertRaises(frappe.ValidationError):
			out.save(ignore_permissions=True)

	def test_remark_list_and_search_metadata(self):
		meta = frappe.get_meta("PM Clearance")
		field = meta.get_field("remark")
		self.assertTrue(field.in_list_view)
		self.assertTrue(field.in_standard_filter)
		self.assertTrue(field.in_global_search)
		self.assertEqual(field.label, "Remarks")

	def test_same_document_return_from_finance_review(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_manager(emp)
		cl = self._draft_clearance(emp)
		name = cl.name
		out = apply_pm_workflow(cl, "PM Submit Finance Review")
		frappe.db.set_value(
			"PM Clearance", out.name, "manager_approver", frappe.session.user, update_modified=False
		)
		out.reload()
		out = apply_pm_workflow(out, "PM Manager Approve")
		out.reload()
		out = apply_pm_workflow(out, "PM Return for Correction")
		out.reload()
		self.assertEqual(out.name, name)
		self.assertEqual(out.docstatus, 0)
		title = frappe.db.get_value("Workflow State", out.workflow_state, "workflow_state_name")
		self.assertEqual(title, "Draft")
