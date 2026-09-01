# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v4.8.9: stamped direct line manager authorization for Asset Request."""

from __future__ import annotations

import unittest

import frappe
from frappe.model.workflow import (
	WorkflowPermissionError,
	WorkflowTransitionError,
	apply_workflow,
	get_transitions,
)
from frappe.utils import random_string

from erpnext_extensions.asset_usage_depreciation.constants import (
	ACTION_APPROVE,
	ACTION_REJECT,
	ACTION_SEND_BACK,
	ACTION_SUBMIT,
	ROLE_AR_MANAGER,
	WF_STATE_APPROVED,
	WF_STATE_DRAFT,
	WF_STATE_PENDING_CEO,
	WF_STATE_PENDING_MANAGER,
	WF_STATE_PENDING_PLANNING,
	WF_STATE_REJECTED,
)
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h


def _actions(doc) -> set[str]:
	return {t.action for t in get_transitions(doc)}


def _share(name: str, user: str):
	return frappe.db.get_value(
		"DocShare",
		{"share_doctype": "Asset Request", "share_name": name, "user": user},
		["name", "read", "write", "submit", "share"],
		as_dict=True,
	)


class TestAssetRequestV489ManagerAuth(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(
			prevent_duplicate_active_requests=0,
			require_named_manager_approver=0,
			auto_create_asset_movement=0,
			auto_create_material_request=0,
		)
		from erpnext_extensions.asset_usage_depreciation.workflow import ensure_asset_request_workflow

		ensure_asset_request_workflow()
		h.ensure_employee_asset_request_perms()
		cls.company = h.company()
		suffix = random_string(6).lower()
		cls.emp_user = h.make_user(email=f"ar.v489.emp.{suffix}@example.com", roles=["Employee"])
		cls.line_mgr_user = h.make_user(
			email=f"ar.v489.line.{suffix}@example.com",
			roles=["Employee"],
		)
		h.strip_asset_request_privileged_roles(cls.line_mgr_user)
		cls.other_emp_user = h.make_user(
			email=f"ar.v489.oth.{suffix}@example.com",
			roles=["Employee"],
		)
		cls.other_mgr_user = h.make_user(
			email=f"ar.v489.omgr.{suffix}@example.com",
			roles=["Employee"],
		)
		h.strip_asset_request_privileged_roles(cls.other_mgr_user)
		cls.unstamped_ar_mgr = h.make_user(
			email=f"ar.v489.armgr.{suffix}@example.com",
			roles=["Employee", ROLE_AR_MANAGER],
		)
		cls.stamped_ar_mgr = h.make_user(
			email=f"ar.v489.both.{suffix}@example.com",
			roles=["Employee", ROLE_AR_MANAGER],
		)
		cls.planner_user = h.make_user(
			email=f"ar.v489.pln.{suffix}@example.com",
			roles=["Employee", "Asset Request Planner"],
		)
		cls.ceo_user = h.make_user(
			email=f"ar.v489.ceo.{suffix}@example.com",
			roles=["Employee", "Asset Request Executive"],
		)
		cls.line_mgr_emp = h.make_employee(company_name=cls.company, user_id=cls.line_mgr_user)
		cls.other_mgr_emp = h.make_employee(company_name=cls.company, user_id=cls.other_mgr_user)
		cls.stamped_ar_mgr_emp = h.make_employee(company_name=cls.company, user_id=cls.stamped_ar_mgr)
		cls.employee = h.make_employee(
			company_name=cls.company, user_id=cls.emp_user, reports_to=cls.line_mgr_emp
		)
		cls.other_employee = h.make_employee(
			company_name=cls.company, user_id=cls.other_emp_user, reports_to=cls.other_mgr_emp
		)
		cls.item = h.make_fixed_asset_item(title="V489 Auth Item")

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)
		frappe.set_user("Administrator")

	def _pending(self, *, manager: str, employee=None, planning=0, ceo=0):
		doc = h.make_request(
			company_name=self.company,
			employee=employee or self.employee,
			item_code=self.item,
			purpose=f"V489 {random_string(4)}",
		)
		doc.manager_approver = manager
		doc.require_planning_approval = planning
		doc.require_ceo_approval = ceo
		if planning:
			doc.planning_approver = self.planner_user
		if ceo:
			doc.ceo_approver = self.ceo_user
		doc.save(ignore_permissions=True)
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SUBMIT)
		doc.reload()
		return doc

	def test_a_direct_line_manager_employee_only_can_approve(self):
		self._ready()
		roles = set(frappe.get_roles(self.line_mgr_user))
		self.assertIn("Employee", roles)
		self.assertNotIn(ROLE_AR_MANAGER, roles)

		doc = self._pending(manager=self.line_mgr_user)
		frappe.set_user(self.line_mgr_user)
		try:
			as_mgr = frappe.get_doc("Asset Request", doc.name)
			self.assertTrue(frappe.has_permission("Asset Request", "read", doc=as_mgr))
			self.assertTrue(frappe.has_permission("Asset Request", "write", doc=as_mgr))
			actions = _actions(as_mgr)
			self.assertIn(ACTION_APPROVE, actions)
			self.assertIn(ACTION_REJECT, actions)
			self.assertIn(ACTION_SEND_BACK, actions)
			apply_workflow(as_mgr, ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(int(doc.docstatus), 1)

	def test_b_unrelated_employee_cannot_apply_manager_actions(self):
		self._ready()
		doc = self._pending(manager=self.line_mgr_user)
		frappe.set_user(self.other_emp_user)
		try:
			try:
				as_other = frappe.get_doc("Asset Request", doc.name)
				actions = _actions(as_other)
			except frappe.PermissionError:
				actions = set()
			self.assertNotIn(ACTION_APPROVE, actions)
			self.assertNotIn(ACTION_REJECT, actions)
			self.assertNotIn(ACTION_SEND_BACK, actions)
			with self.assertRaises(
				(WorkflowPermissionError, WorkflowTransitionError, frappe.PermissionError, frappe.ValidationError)
			):
				apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_PENDING_MANAGER)
		self.assertEqual(int(doc.docstatus or 0), 0)

	def test_c_unrelated_line_manager_cannot_act(self):
		self._ready()
		doc = self._pending(manager=self.line_mgr_user)
		frappe.set_user(self.other_mgr_user)
		try:
			try:
				as_other = frappe.get_doc("Asset Request", doc.name)
				actions = _actions(as_other)
			except frappe.PermissionError:
				actions = set()
			self.assertNotIn(ACTION_APPROVE, actions)
			self.assertNotIn(ACTION_REJECT, actions)
			self.assertNotIn(ACTION_SEND_BACK, actions)
		finally:
			frappe.set_user("Administrator")

	def test_d_unstamped_asset_request_manager_cannot_act(self):
		self._ready()
		doc = self._pending(manager=self.line_mgr_user)
		frappe.set_user(self.unstamped_ar_mgr)
		try:
			try:
				as_role = frappe.get_doc("Asset Request", doc.name)
				actions = _actions(as_role)
			except frappe.PermissionError:
				actions = set()
			self.assertNotIn(ACTION_APPROVE, actions)
			self.assertNotIn(ACTION_REJECT, actions)
			self.assertNotIn(ACTION_SEND_BACK, actions)
			with self.assertRaises(
				(WorkflowPermissionError, WorkflowTransitionError, frappe.PermissionError, frappe.ValidationError)
			):
				apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
			with self.assertRaises(
				(WorkflowPermissionError, WorkflowTransitionError, frappe.PermissionError, frappe.ValidationError)
			):
				pending = frappe.get_doc("Asset Request", doc.name)
				pending.rejection_reason = "no"
				apply_workflow(pending, ACTION_REJECT)
			with self.assertRaises(
				(WorkflowPermissionError, WorkflowTransitionError, frappe.PermissionError, frappe.ValidationError)
			):
				apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SEND_BACK)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_PENDING_MANAGER)

	def test_e_stamped_manager_with_ar_manager_role_can_approve(self):
		self._ready()
		doc = self._pending(manager=self.stamped_ar_mgr)
		frappe.set_user(self.stamped_ar_mgr)
		try:
			as_mgr = frappe.get_doc("Asset Request", doc.name)
			self.assertIn(ACTION_APPROVE, _actions(as_mgr))
			apply_workflow(as_mgr, ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(int(doc.docstatus), 1)

	def test_f_system_manager_break_glass_remains(self):
		self._ready()
		doc = self._pending(manager=self.line_mgr_user)
		frappe.set_user("Administrator")
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(int(doc.docstatus), 1)

	def test_g_share_grants_submit_only_to_stamped_manager(self):
		self._ready()
		doc = self._pending(manager=self.line_mgr_user)
		share = _share(doc.name, self.line_mgr_user)
		self.assertTrue(share)
		self.assertEqual(int(share.read), 1)
		self.assertEqual(int(share.write), 1)
		self.assertEqual(int(share.submit), 1)
		self.assertFalse(frappe.get_meta("DocShare").has_field("cancel"))

		frappe.set_user(self.line_mgr_user)
		try:
			self.assertTrue(
				frappe.has_permission("Asset Request", "submit", doc=frappe.get_doc("Asset Request", doc.name))
			)
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(self.other_emp_user)
		try:
			self.assertFalse(
				frappe.has_permission(
					"Asset Request", "submit", doc=frappe.get_doc("Asset Request", doc.name)
				)
			)
		except frappe.PermissionError:
			pass
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(int(doc.docstatus), 1)

	def test_h_optional_stages_are_not_skipped(self):
		self._ready()
		planning = self._pending(manager=self.line_mgr_user, planning=1, ceo=0)
		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", planning.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		planning.reload()
		self.assertEqual(planning.workflow_state, WF_STATE_PENDING_PLANNING)
		self.assertEqual(int(planning.docstatus or 0), 0)

		ceo = self._pending(manager=self.line_mgr_user, planning=0, ceo=1)
		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", ceo.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		ceo.reload()
		self.assertEqual(ceo.workflow_state, WF_STATE_PENDING_CEO)
		self.assertEqual(int(ceo.docstatus or 0), 0)

	def test_share_not_duplicated_and_stale_replaced(self):
		self._ready()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		doc.manager_approver = self.other_mgr_user
		doc.save(ignore_permissions=True)
		first = _share(doc.name, self.other_mgr_user)
		self.assertTrue(first)
		doc.manager_approver = self.line_mgr_user
		doc.save(ignore_permissions=True)
		self.assertFalse(_share(doc.name, self.other_mgr_user))
		self.assertTrue(_share(doc.name, self.line_mgr_user))
		doc.require_planning_approval = 0
		doc.require_ceo_approval = 0
		doc.save(ignore_permissions=True)
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SUBMIT)
		doc.reload()
		doc.save(ignore_permissions=True)
		doc.save(ignore_permissions=True)
		shares = frappe.get_all(
			"DocShare",
			filters={"share_doctype": "Asset Request", "share_name": doc.name, "user": self.line_mgr_user},
		)
		self.assertEqual(len(shares), 1)
		unrelated = h.make_request(
			company_name=self.company, employee=self.other_employee, item_code=self.item
		)
		self.assertFalse(_share(unrelated.name, self.line_mgr_user))

	def test_submit_blocked_without_valid_manager(self):
		self._ready()
		orphan_user = h.make_user(
			email=f"ar.v489.orphan.{random_string(4).lower()}@example.com",
			roles=["Employee"],
		)
		orphan_emp = h.make_employee(company_name=self.company, user_id=orphan_user)
		frappe.db.set_value("Employee", orphan_emp, "reports_to", "")
		doc = h.make_request(company_name=self.company, employee=orphan_emp, item_code=self.item)
		with self.assertRaises(frappe.ValidationError):
			apply_workflow(doc, ACTION_SUBMIT)
		doc.reload()
		self.assertEqual(doc.workflow_state or WF_STATE_DRAFT, WF_STATE_DRAFT)

	def test_one_assignment_closed_after_action(self):
		self._ready()
		doc = self._pending(manager=self.line_mgr_user)
		todos = frappe.get_all(
			"ToDo",
			filters={
				"reference_type": "Asset Request",
				"reference_name": doc.name,
				"status": "Open",
				"allocated_to": self.line_mgr_user,
			},
		)
		self.assertEqual(len(todos), 1)
		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		open_after = frappe.get_all(
			"ToDo",
			filters={
				"reference_type": "Asset Request",
				"reference_name": doc.name,
				"status": "Open",
				"allocated_to": self.line_mgr_user,
			},
		)
		self.assertFalse(open_after)


class TestAssetRequestV489Integration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(
			prevent_duplicate_active_requests=0,
			require_named_manager_approver=0,
			auto_create_asset_movement=0,
			auto_create_material_request=0,
		)
		from erpnext_extensions.asset_usage_depreciation.workflow import ensure_asset_request_workflow

		ensure_asset_request_workflow()
		h.ensure_employee_asset_request_perms()
		cls.company = h.company()
		suffix = random_string(6).lower()
		cls.emp_user = h.make_user(email=f"ar.v489f.emp.{suffix}@example.com", roles=["Employee"])
		cls.line_mgr_user = h.make_user(
			email=f"ar.v489f.line.{suffix}@example.com",
			roles=["Employee"],
		)
		h.strip_asset_request_privileged_roles(cls.line_mgr_user)
		cls.planner_user = h.make_user(
			email=f"ar.v489f.pln.{suffix}@example.com",
			roles=["Employee", "Asset Request Planner"],
		)
		cls.ceo_user = h.make_user(
			email=f"ar.v489f.ceo.{suffix}@example.com",
			roles=["Employee", "Asset Request Executive"],
		)
		cls.line_mgr_emp = h.make_employee(company_name=cls.company, user_id=cls.line_mgr_user)
		cls.employee = h.make_employee(
			company_name=cls.company, user_id=cls.emp_user, reports_to=cls.line_mgr_emp
		)
		cls.item = h.make_fixed_asset_item(title="V489 Flow Item")

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)
		frappe.set_user("Administrator")

	def test_flow1_employee_only_manager_to_approved(self):
		self._ready()
		self.assertNotIn(ROLE_AR_MANAGER, frappe.get_roles(self.line_mgr_user))
		frappe.set_user(self.emp_user)
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Asset Request",
					"company": self.company,
					"employee": self.employee,
					"transaction_date": frappe.utils.nowdate(),
					"required_date": frappe.utils.nowdate(),
					"purpose": "V489 flow 1",
					"items": [{"requested_item_code": self.item, "qty": 1}],
				}
			)
			doc.insert()
			apply_workflow(doc, ACTION_SUBMIT)
			doc.reload()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(doc.workflow_state, WF_STATE_PENDING_MANAGER)
		self.assertEqual(doc.manager_approver, self.line_mgr_user)
		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(int(doc.docstatus), 1)

	def test_flow2_manager_planning_ceo(self):
		self._ready()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		doc.manager_approver = self.line_mgr_user
		doc.planning_approver = self.planner_user
		doc.ceo_approver = self.ceo_user
		doc.require_planning_approval = 1
		doc.require_ceo_approval = 1
		doc.save(ignore_permissions=True)
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SUBMIT)
		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_PENDING_PLANNING)
		frappe.set_user(self.planner_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_PENDING_CEO)
		frappe.set_user(self.ceo_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(int(doc.docstatus), 1)

	def test_flow3_manager_reject(self):
		self._ready()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		doc.manager_approver = self.line_mgr_user
		doc.rejection_reason = "Not justified"
		doc.save(ignore_permissions=True)
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SUBMIT)
		frappe.set_user(self.line_mgr_user)
		try:
			pending = frappe.get_doc("Asset Request", doc.name)
			pending.rejection_reason = "Not justified"
			apply_workflow(pending, ACTION_REJECT)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_REJECTED)
		self.assertEqual(int(doc.docstatus or 0), 0)

	def test_flow4_manager_send_back(self):
		self._ready()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		doc.manager_approver = self.line_mgr_user
		doc.save(ignore_permissions=True)
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SUBMIT)
		frappe.set_user(self.line_mgr_user)
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SEND_BACK)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.workflow_state, WF_STATE_DRAFT)
		self.assertEqual(int(doc.docstatus or 0), 0)
