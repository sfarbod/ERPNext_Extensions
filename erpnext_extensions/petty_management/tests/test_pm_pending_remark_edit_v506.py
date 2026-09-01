# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.6 — remark-only save while Pending approval."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import today

from erpnext_extensions.petty_management.services.draft_approval_guards import (
	only_remark_changed_while_pending,
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


class TestPMPendingRemarkEditV506(unittest.TestCase):
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

		cls.mgr = "pm_v506_mgr@example.com"
		cls.ceo = "pm_v506_ceo@example.com"
		cls.fin = "pm_v506_fin@example.com"
		cls.holder = "pm_v506_holder@example.com"
		cls.reviewer = "pm_v506_rev@example.com"
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

	def _new_request(self) -> frappe.Document:
		emp = pm_ct._make_employee()
		frappe.db.set_value(
			"Employee", emp, {"expense_approver": self.mgr, "user_id": self.holder}, update_modified=False
		)
		pm_ct._make_holder(emp)
		req = frappe.new_doc("PM Request")
		req.company = pm_ct.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1500, "description": "v506"})
		req.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Request",
			req.name,
			{"workflow_state": resolve_workflow_state_link("Draft"), "owner": self.holder},
			update_modified=False,
		)
		return frappe.get_doc("PM Request", req.name)

	def _submit_request(self, doc: frappe.Document) -> frappe.Document:
		frappe.set_user(self.holder)
		out = apply_pm_workflow(doc, "PM Submit for Approval")
		frappe.set_user("Administrator")
		return out

	def _pending_request(self, title: str) -> frappe.Document:
		doc = self._submit_request(self._new_request())
		frappe.db.set_value("PM Request", doc.name, "manager_approver", self.mgr, update_modified=False)
		if title == "Pending Manager Approval":
			return doc.reload()
		frappe.set_user(self.mgr)
		doc = apply_pm_workflow(doc, "PM Manager Approve")
		frappe.set_user("Administrator")
		if title == "Pending CEO Approval":
			return doc.reload()
		frappe.set_user(self.ceo)
		doc = apply_pm_workflow(doc, "PM CEO Approve")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(doc.workflow_state), "Pending Finance Approval")
		return doc.reload()

	def _save_remark(self, doc: frappe.Document, text: str) -> frappe.Document:
		doc.remark = text
		doc.save()
		return doc.reload()

	def _assert_blocked(self, doc: frappe.Document, mutate) -> None:
		mutate(doc)
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save()
		self.assertIn("Only Remarks may be edited", str(ctx.exception))

	def test_request_remark_save_pending_manager(self):
		doc = self._pending_request("Pending Manager Approval")
		out = self._save_remark(doc, "Manager pending note")
		self.assertEqual(out.remark, "Manager pending note")

	def test_request_remark_save_pending_ceo(self):
		doc = self._pending_request("Pending CEO Approval")
		out = self._save_remark(doc, "CEO pending note")
		self.assertEqual(out.remark, "CEO pending note")

	def test_request_remark_save_pending_finance(self):
		doc = self._pending_request("Pending Finance Approval")
		out = self._save_remark(doc, "Finance pending note")
		self.assertEqual(out.remark, "Finance pending note")

	def test_request_blocks_amount_edit_pending(self):
		doc = self._pending_request("Pending Manager Approval")

		def mutate(d):
			d.details[0].advance_amount = 9999

		self._assert_blocked(doc, mutate)

	def test_request_blocks_employee_edit_pending(self):
		doc = self._pending_request("Pending Manager Approval")
		other = pm_ct._make_employee()

		def mutate(d):
			d.employee = other

		self._assert_blocked(doc, mutate)

	def test_request_blocks_child_row_add_pending(self):
		doc = self._pending_request("Pending Manager Approval")

		def mutate(d):
			d.append("details", {"advance_amount": 500, "description": "extra"})

		self._assert_blocked(doc, mutate)

	def _make_clearance_pending(self, title: str) -> str | None:
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
		frappe.set_user(self.holder)
		apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Submit Finance Review")
		frappe.set_user("Administrator")
		if title == "Pending Manager Approval":
			return cl.name
		frappe.set_user(self.mgr)
		apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Manager Approve")
		frappe.set_user("Administrator")
		return cl.name

	def test_clearance_remark_save_pending_manager(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)
		out = self._save_remark(doc, "Clearance manager note")
		self.assertEqual(out.remark, "Clearance manager note")

	def test_clearance_remark_save_pending_finance(self):
		name = self._make_clearance_pending("Pending Finance Review")
		doc = frappe.get_doc("PM Clearance", name)
		out = self._save_remark(doc, "Clearance finance note")
		self.assertEqual(out.remark, "Clearance finance note")

	def test_clearance_blocks_allocation_edit_pending(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)
		req, _pe = pm_ct._fund_pm_request(doc.employee, 5_000.0)

		def mutate(d):
			d.append(
				"request_allocations",
				{
					"funding_source_type": "PM Request",
					"pm_request": req,
					"allocated_amount": 100,
				},
			)

		self._assert_blocked(doc, mutate)

	def test_clearance_blocks_journal_entry_edit_pending(self):
		name = self._make_clearance_pending("Pending Finance Review")
		doc = frappe.get_doc("PM Clearance", name)

		def mutate(d):
			d.journal_entry = "ACC-JV-TEST-00001"

		self._assert_blocked(doc, mutate)

	def test_clearance_blocks_child_row_edit_pending(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)

		def mutate(d):
			d.details[0].allocated_amount = 50

		self._assert_blocked(doc, mutate)

	def test_only_remark_changed_helper(self):
		doc = self._pending_request("Pending Manager Approval")
		doc.remark = "helper note"
		doc.load_doc_before_save(raise_exception=True)
		self.assertTrue(only_remark_changed_while_pending(doc))
		doc.details[0].advance_amount = 2000
		self.assertFalse(only_remark_changed_while_pending(doc))
