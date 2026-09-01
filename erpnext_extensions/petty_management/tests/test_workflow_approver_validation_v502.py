# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.2 — fail-fast workflow approver validation."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import today

from erpnext_extensions.petty_management.services.approver_stamp_service import (
	stamp_pm_clearance_approvers,
	stamp_pm_request_approvers,
)
from erpnext_extensions.petty_management.services.clearance_finance_review import (
	get_clearance_finance_review_role,
)
from erpnext_extensions.petty_management.services.workflow_approver_validation_service import (
	get_workflow_roles_for_approver_field,
	validate_acting_approver_can_read,
	validate_workflow_approvers,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct
from erpnext_extensions.petty_management.workflow_hooks import apply_workflow as hooked_apply_workflow

REVIEWER_ROLE = "Petty Management Clearance Reviewer"


def _ensure_role(role: str) -> None:
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)


def _ensure_user(email: str, roles: list[str]) -> str:
	for role in roles:
		_ensure_role(role)
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"enabled": 1,
				"send_welcome_email": 0,
			}
		)
		for role in roles:
			doc.append("roles", {"role": role})
		doc.insert(ignore_permissions=True)
		return email

	doc = frappe.get_doc("User", email)
	existing = {row.role for row in doc.roles}
	changed = False
	for role in roles:
		if role not in existing:
			doc.append("roles", {"role": role})
			changed = True
	for row in list(doc.roles):
		if row.role not in roles and row.role not in ("All", "Guest", "Desk User"):
			doc.remove(row)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)
	return email


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


class TestWorkflowApproverValidationV502(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		cls.company = pm_ct.COMPANY
		if not cls.company:
			raise unittest.SkipTest("No Company on site")
		cls.employee = pm_ct._make_employee()
		pm_ct._make_holder(cls.employee)

		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
			_rebuild_pm_request_workflow,
			_seed_assignment_rules,
		)

		_rebuild_pm_request_workflow()
		_rebuild_pm_clearance_workflow()
		_seed_assignment_rules()
		frappe.db.commit()

		cls.mgr_bad = "pm_v502_mgr_bad@example.com"
		cls.ceo_bad = "pm_v502_ceo_bad@example.com"
		cls.fin_bad = "pm_v502_fin_bad@example.com"
		cls.mgr_good = "pm_v502_mgr_good@example.com"
		cls.fin_good = "pm_v502_fin_good@example.com"
		cls.reviewer = "pm_v502_clr_rev@example.com"
		cls.requester = "pm_v502_requester@example.com"

		_ensure_user(cls.mgr_bad, ["Expense Approver", "Desk User", "All"])
		_ensure_user(cls.ceo_bad, ["Expense Approver", "Desk User", "All"])
		_ensure_user(cls.fin_bad, ["Petty Management User", "Desk User", "All"])
		_ensure_user(
			cls.mgr_good,
			["Petty Management User", "Expense Approver", "Accounts User", "Desk User", "All"],
		)
		_ensure_user(
			cls.fin_good,
			["Petty Management Accountant", "Petty Management User", "Accounts User", "Desk User", "All"],
		)
		_ensure_user(cls.reviewer, [REVIEWER_ROLE, "Accounts User", "Desk User", "All"])
		_ensure_user(cls.requester, ["Petty Management User", "Accounts User", "Desk User", "All"])

		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("ceo_approver", "Administrator", update_modified=False)
		settings.db_set("finance_manager", cls.fin_good, update_modified=False)
		if frappe.get_meta("PM Settings").has_field("clearance_finance_review_role"):
			settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)
		cls._settings = settings

	def _request_doc(self, **overrides):
		doc = frappe._dict(
			doctype="PM Request",
			employee=self.employee,
			company=self.company,
			manager_approver=overrides.get("manager_approver"),
			ceo_approver=overrides.get("ceo_approver", "Administrator"),
			finance_approver=overrides.get("finance_approver", self.fin_good),
		)
		doc.update(overrides)
		return doc

	def _clearance_doc(self, **overrides):
		doc = frappe._dict(
			doctype="PM Clearance",
			employee=self.employee,
			company=self.company,
			manager_approver=overrides.get("manager_approver"),
		)
		doc.update(overrides)
		return doc

	def _configure_request_approvers(self, manager: str) -> None:
		frappe.db.set_value("Employee", self.employee, "expense_approver", manager, update_modified=False)
		self._settings.db_set("ceo_approver", manager, update_modified=False)
		self._settings.db_set("finance_manager", self.fin_good, update_modified=False)

	def _make_pending_request(self, manager: str):
		self._configure_request_approvers(manager)
		req = frappe.new_doc("PM Request")
		req.company = self.company
		req.employee = self.employee
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1500, "description": "v502"})
		req.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Request",
			req.name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		req.reload()
		frappe.set_user(manager)
		out = apply_pm_workflow(req, "PM Submit for Approval")
		out.reload()
		frappe.set_user("Administrator")
		return out

	def _make_clearance_pending_finance(self, owner: str | None = None) -> str:
		"""Lightweight Pending Finance Review clearance (draft PI, no submit)."""
		frappe.db.set_value(
			"Employee", self.employee, "expense_approver", self.mgr_good, update_modified=False
		)
		pi = pm_ct._make_pi_outstanding(1_000.0)
		try:
			pi.insert(ignore_permissions=True)
		except frappe.ValidationError as exc:
			raise unittest.SkipTest(f"Purchase Invoice insert unavailable: {exc}") from exc
		cl = frappe.new_doc("PM Clearance")
		cl.company = self.company
		cl.employee = self.employee
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
		try:
			cl.insert(ignore_permissions=True)
		except frappe.ValidationError as exc:
			raise unittest.SkipTest(f"PM Clearance insert unavailable: {exc}") from exc
		values = {
			"workflow_state": resolve_workflow_state_link("Pending Finance Review"),
			"manager_approver": self.mgr_good,
			"finance_approver": self.reviewer,
			"status": "Pending Approval",
		}
		if owner:
			values["owner"] = owner
		frappe.db.set_value("PM Clearance", cl.name, values, update_modified=False)
		cl.reload()
		self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")
		return cl.name

	def test_manager_missing_pm_user_raises(self):
		doc = self._request_doc(manager_approver=self.mgr_bad)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_workflow_approvers(doc)
		msg = str(ctx.exception)
		self.assertIn("Manager Approver", msg)
		self.assertIn(self.mgr_bad, msg)
		self.assertIn("Petty Management User", msg)

	def test_ceo_missing_required_role_raises(self):
		self._settings.db_set("ceo_approver", self.ceo_bad, update_modified=False)
		doc = self._request_doc(
			manager_approver="Administrator",
			ceo_approver=self.ceo_bad,
			finance_approver="Administrator",
		)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				validate_workflow_approvers(doc)
			msg = str(ctx.exception)
			self.assertIn("CEO Approver", msg)
			self.assertIn(self.ceo_bad, msg)
			self.assertIn("Petty Management User", msg)
		finally:
			self._settings.db_set("ceo_approver", "Administrator", update_modified=False)

	def test_finance_missing_required_role_raises(self):
		self._settings.db_set("finance_manager", self.fin_bad, update_modified=False)
		doc = self._request_doc(
			manager_approver="Administrator",
			ceo_approver="Administrator",
			finance_approver=self.fin_bad,
		)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				validate_workflow_approvers(doc)
			msg = str(ctx.exception)
			self.assertIn("Finance Approver", msg)
			self.assertIn(self.fin_bad, msg)
			self.assertIn("Petty Management Accountant", msg)
		finally:
			self._settings.db_set("finance_manager", self.fin_good, update_modified=False)

	def test_finance_role_inference_excludes_terminal_reject(self):
		roles = get_workflow_roles_for_approver_field("PM Request", "finance_approver")
		self.assertIn("Petty Management Accountant", roles)
		self.assertNotIn("Petty Management User", roles)

	def test_all_valid_request_stamp_succeeds(self):
		frappe.db.set_value(
			"Employee", self.employee, "expense_approver", self.mgr_good, update_modified=False
		)
		doc = frappe._dict(employee=self.employee, company=self.company)
		try:
			stamp_pm_request_approvers(doc)
			self.assertEqual(doc.manager_approver, self.mgr_good)
			self.assertTrue(doc.ceo_approver)
			self.assertTrue(doc.finance_approver)
		finally:
			frappe.db.set_value(
				"Employee", self.employee, "expense_approver", None, update_modified=False
			)

	def test_clearance_manager_missing_pm_user_raises(self):
		doc = self._clearance_doc(manager_approver=self.mgr_bad)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_workflow_approvers(doc)
		self.assertIn("PM Clearance", str(ctx.exception))

	def test_clearance_valid_manager_stamp_succeeds(self):
		frappe.db.set_value(
			"Employee", self.employee, "expense_approver", self.mgr_good, update_modified=False
		)
		doc = frappe._dict(employee=self.employee, company=self.company)
		try:
			stamp_pm_clearance_approvers(doc)
			self.assertEqual(doc.manager_approver, self.mgr_good)
		finally:
			frappe.db.set_value(
				"Employee", self.employee, "expense_approver", None, update_modified=False
			)

	def test_stamp_request_blocks_expense_only_manager(self):
		frappe.db.set_value(
			"Employee", self.employee, "expense_approver", self.mgr_bad, update_modified=False
		)
		doc = frappe._dict(employee=self.employee, company=self.company)
		try:
			with self.assertRaises(frappe.ValidationError):
				stamp_pm_request_approvers(doc)
		finally:
			frappe.db.set_value(
				"Employee", self.employee, "expense_approver", None, update_modified=False
			)

	def test_manager_valid_submit_and_return_works(self):
		req = self._make_pending_request(self.mgr_good)
		frappe.db.set_value("PM Request", req.name, "owner", self.requester, update_modified=False)
		frappe.set_user(self.mgr_good)
		out = apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")
		out.reload()
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		self.assertFalse(out.manager_approver)
		self.assertTrue(
			frappe.db.exists(
				"ToDo",
				{
					"reference_type": "PM Request",
					"reference_name": req.name,
					"allocated_to": self.requester,
					"status": "Open",
				},
			)
		)

	def test_finance_valid_return_works(self):
		req = self._make_pending_request(self.mgr_good)
		frappe.db.set_value(
			"PM Request",
			req.name,
			{
				"workflow_state": resolve_workflow_state_link("Pending Finance Approval"),
				"manager_approver": self.mgr_good,
				"ceo_approver": self.mgr_good,
				"finance_approver": self.fin_good,
			},
			update_modified=False,
		)
		frappe.db.set_value("PM Request", req.name, "owner", self.requester, update_modified=False)
		frappe.set_user(self.fin_good)
		out = apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")
		out.reload()
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		self.assertFalse(out.finance_approver)

	def test_role_removed_after_submit_workflow_precheck_fails(self):
		req = self._make_pending_request(self.mgr_good)
		user_doc = frappe.get_doc("User", self.mgr_good)
		user_doc.roles = []
		user_doc.append("roles", {"role": "Expense Approver"})
		user_doc.append("roles", {"role": "Desk User"})
		user_doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(user=self.mgr_good)
		try:
			frappe.set_user(self.mgr_good)
			self.assertFalse(
				frappe.has_permission(
					"PM Request", "read", doc=frappe.get_doc("PM Request", req.name)
				)
			)
			with self.assertRaises(frappe.ValidationError) as ctx:
				hooked_apply_workflow(
					frappe.get_doc("PM Request", req.name).as_dict(),
					"PM Return for Correction",
				)
			self.assertIn("no longer have permission", str(ctx.exception).lower())
		finally:
			frappe.set_user("Administrator")
			_ensure_user(
				self.mgr_good,
				["Petty Management User", "Expense Approver", "Accounts User", "Desk User", "All"],
			)
			frappe.db.commit()
			frappe.clear_cache(user=self.mgr_good)

	def test_acting_approver_read_guard_unit(self):
		req = self._make_pending_request(self.mgr_good)
		frappe.set_user(self.mgr_bad)
		with self.assertRaises(frappe.ValidationError):
			validate_acting_approver_can_read(
				frappe.get_doc("PM Request", req.name), "PM Return for Correction"
			)
		frappe.set_user("Administrator")

	def test_clearance_no_enabled_finance_reviewer_fails_fast(self):
		if not frappe.get_meta("PM Settings").has_field("clearance_finance_review_role"):
			self.skipTest("clearance_finance_review_role not on site")
		empty_role = "PM V502 Empty Reviewer Role"
		_ensure_role(empty_role)
		frappe.db.set_value(
			"Employee", self.employee, "expense_approver", self.mgr_good, update_modified=False
		)
		doc = frappe._dict(employee=self.employee, company=self.company)
		prev_role = get_clearance_finance_review_role()
		try:
			self._settings.db_set("clearance_finance_review_role", empty_role, update_modified=False)
			with self.assertRaises(frappe.ValidationError) as ctx:
				stamp_pm_clearance_approvers(doc)
			self.assertIn("no enabled User", str(ctx.exception))
		finally:
			self._settings.db_set("clearance_finance_review_role", prev_role, update_modified=False)
			frappe.db.set_value(
				"Employee", self.employee, "expense_approver", None, update_modified=False
			)

	def test_clearance_finance_reviewer_exists_pending_finance_works(self):
		if not frappe.get_meta("PM Settings").has_field("clearance_finance_review_role"):
			self.skipTest("clearance_finance_review_role not on site")
		cl_name = self._make_clearance_pending_finance()
		try:
			frappe.set_user(self.reviewer)
			self.assertTrue(
				frappe.has_permission("PM Clearance", "read", doc=frappe.get_doc("PM Clearance", cl_name))
			)
		finally:
			frappe.set_user("Administrator")

	def test_clearance_finance_return_assigns_requester(self):
		if not frappe.get_meta("PM Settings").has_field("clearance_finance_review_role"):
			self.skipTest("clearance_finance_review_role not on site")
		cl_name = self._make_clearance_pending_finance(owner=self.requester)
		try:
			frappe.set_user(self.reviewer)
			out = apply_pm_workflow(
				frappe.get_doc("PM Clearance", cl_name), "PM Return for Correction"
			)
			out.reload()
			frappe.set_user("Administrator")
			self.assertEqual(_wf_title(out.workflow_state), "Draft")
			self.assertFalse(out.manager_approver)
			self.assertFalse(out.finance_approver)
			self.assertTrue(
				frappe.db.exists(
					"ToDo",
					{
						"reference_type": "PM Clearance",
						"reference_name": cl_name,
						"allocated_to": self.requester,
						"status": "Open",
					},
				)
			)
		finally:
			frappe.set_user("Administrator")

	def test_request_return_stamps_cleared_after_assignment(self):
		call_order: list[str] = []
		req = self._make_pending_request(self.mgr_good)
		frappe.db.set_value("PM Request", req.name, "owner", self.requester, update_modified=False)

		def _track_clear(doc):
			call_order.append("clear")

		frappe.set_user(self.mgr_good)
		try:
			with (
				patch(
					"erpnext_extensions.petty_management.services.return_for_correction_service.assign_requester",
					side_effect=lambda doc: call_order.append("assign"),
				),
				patch(
					"erpnext_extensions.petty_management.services.return_for_correction_service.clear_approver_stamps",
					side_effect=_track_clear,
				),
			):
				apply_pm_workflow(
					frappe.get_doc("PM Request", req.name), "PM Return for Correction"
				)
			self.assertEqual(call_order, ["assign", "clear"])
		finally:
			frappe.set_user("Administrator")

	def test_handle_return_source_assigns_before_clear(self):
		import inspect

		from erpnext_extensions.petty_management.services.return_for_correction_service import (
			handle_return_for_correction,
		)

		src = inspect.getsource(handle_return_for_correction)
		self.assertLess(src.index("assign_requester"), src.index("clear_approver_stamps"))

	def test_clearance_return_stamps_cleared_after_assignment(self):
		call_order: list[str] = []

		def _track_clear(doc):
			call_order.append("clear")

		if not frappe.get_meta("PM Settings").has_field("clearance_finance_review_role"):
			self.skipTest("clearance_finance_review_role not on site")
		cl_name = self._make_clearance_pending_finance()
		try:
			frappe.set_user(self.reviewer)
			with (
				patch(
					"erpnext_extensions.petty_management.services.return_for_correction_service.assign_requester",
					side_effect=lambda doc: call_order.append("assign"),
				),
				patch(
					"erpnext_extensions.petty_management.services.return_for_correction_service.clear_approver_stamps",
					side_effect=_track_clear,
				),
			):
				apply_pm_workflow(
					frappe.get_doc("PM Clearance", cl_name), "PM Return for Correction"
				)
			self.assertEqual(call_order, ["assign", "clear"])
		finally:
			frappe.set_user("Administrator")

	def test_clearance_return_assignment_failure_rolls_back(self):
		if not frappe.get_meta("PM Settings").has_field("clearance_finance_review_role"):
			self.skipTest("clearance_finance_review_role not on site")
		cl_name = self._make_clearance_pending_finance()
		cl = frappe.get_doc("PM Clearance", cl_name)
		before_state = cl.workflow_state
		before_mgr = cl.manager_approver
		try:
			frappe.set_user(self.reviewer)
			with patch("frappe.desk.form.assign_to.add", side_effect=RuntimeError("inject assign failure")):
				with self.assertRaises(RuntimeError):
					apply_pm_workflow(
						frappe.get_doc("PM Clearance", cl_name), "PM Return for Correction"
					)
			frappe.db.rollback()
			cl.reload()
			frappe.set_user("Administrator")
			self.assertEqual(cl.workflow_state, before_state)
			self.assertEqual(cl.manager_approver, before_mgr)
			self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")
		finally:
			frappe.set_user("Administrator")
