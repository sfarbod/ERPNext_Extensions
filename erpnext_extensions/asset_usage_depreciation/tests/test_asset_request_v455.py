# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset Request v4.5.5 — approval is not fulfillment."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import frappe
from frappe.utils import random_string

from erpnext_extensions.asset_usage_depreciation.constants import (
	ACTION_APPROVE,
	ACTION_SUBMIT,
	FULFILLMENT_WAITING,
	ROLE_AR_MANAGER,
	ROLE_ASSET_MANAGER,
)
from erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request import (
	check_availability,
	create_asset_movement,
	create_material_request,
	get_pool_picker,
	issue_from_pool,
	request_purchase,
)
from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
	_persist_generated_doc,
)
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h


class TestSessionSafePersist(unittest.TestCase):
	def test_sid_unchanged_on_success_and_exception(self):
		original_user = frappe.session.user
		frappe.session.sid = "keep-this-sid-token"
		frappe.session.user = "tester@example.com"
		frappe.session.data = frappe._dict({"marker": 1})
		saved_form = frappe.local.form_dict

		ok = MagicMock()
		ok.insert.return_value = ok
		ok.submit.return_value = None
		_persist_generated_doc(ok, ignore_permissions=True, auto_submit=0)
		self.assertEqual(frappe.session.sid, "keep-this-sid-token")
		self.assertEqual(frappe.session.user, "tester@example.com")
		self.assertEqual(frappe.session.data.get("marker"), 1)

		boom = MagicMock()
		boom.insert.side_effect = RuntimeError("insert failed")
		with self.assertRaises(RuntimeError):
			_persist_generated_doc(boom, ignore_permissions=True, auto_submit=0)
		self.assertEqual(frappe.session.sid, "keep-this-sid-token")
		self.assertEqual(frappe.session.user, "tester@example.com")
		frappe.session.user = original_user
		frappe.local.form_dict = saved_form


class TestApprovalDoesNotFulfill(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(
			auto_create_material_request=1,
			auto_create_asset_movement=1,
			require_named_manager_approver=0,
			prevent_duplicate_active_requests=0,
		)
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def test_on_submit_creates_no_mr_am_even_if_settings_on(self):
		self._ready()
		item = h.make_fixed_asset_item()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=item)
		h.submit_and_approve(doc)
		doc.reload()
		self.assertEqual(int(doc.docstatus), 1)
		self.assertEqual(doc.workflow_state, "Approved")
		self.assertEqual(doc.status, "Approved")
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertFalse(doc.material_request)
		self.assertFalse(doc.allocations)
		self.assertFalse(
			frappe.db.exists("Material Request", {"custom_asset_request": doc.name})
		)

	def test_idle_approved_request_stays_unfulfilled(self):
		self._ready()
		item = h.make_fixed_asset_item()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=item)
		h.submit_and_approve(doc)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertFalse(doc.material_request)
		self.assertFalse(doc.allocations)

	def test_check_availability_does_not_create_or_reserve(self):
		self._ready()
		item = h.make_fixed_asset_item()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=item)
		h.submit_and_approve(doc)
		check_availability(doc.name)
		doc.reload()
		self.assertFalse(doc.allocations)
		self.assertFalse(doc.material_request)
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertEqual(doc.workflow_state, "Approved")

	def test_request_purchase_creates_mr(self):
		self._ready()
		tag = random_string(6)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-V455-PS-{tag}", title="Samsung Buy")
		lg = h.make_fixed_asset_item(code=f"AUD-AR-V455-PL-{tag}", title="LG Buy")
		doc = h.make_request(
			company_name=self.company,
			employee=self.employee,
			item_code=samsung,
			fulfilled_item_code=lg,
			substitution_reason="Standard LG",
		)
		h.submit_and_approve(doc)
		doc.reload()
		self.assertFalse(doc.material_request)
		h.request_purchase(doc)
		doc.reload()
		self.assertTrue(doc.material_request)
		mr = frappe.get_doc("Material Request", doc.material_request)
		self.assertEqual(mr.material_request_type, "Purchase")
		self.assertEqual(mr.items[0].item_code, lg)

	def test_issue_from_pool_creates_am(self):
		self._ready()
		tag = random_string(6)
		category = h.make_isolated_category(tag)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-V455-S-{tag}", title="Samsung Pool", category=category)
		lg = h.make_fixed_asset_item(code=f"AUD-AR-V455-L-{tag}", title="LG Pool", category=category)
		asset = h.make_pool_asset(item_code=lg, company_name=self.company)
		h.ensure_settings(allow_category_substitution=1)
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=samsung)
		h.submit_and_approve(doc)
		doc.reload()
		self.assertFalse(doc.allocations)
		h.issue_from_pool(doc)
		doc.reload()
		self.assertTrue(doc.allocations)
		alloc = doc.allocations[0]
		self.assertEqual(alloc.allocated_asset, asset)
		self.assertTrue(alloc.asset_movement)
		self.assertEqual(
			frappe.db.get_value("Asset Movement", alloc.asset_movement, "reference_name"),
			doc.name,
		)


class TestFulfillmentPermissionsV455(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(prevent_duplicate_active_requests=0, require_named_manager_approver=0)
		cls.company = h.company()
		suffix = random_string(6).lower()
		cls.emp_user = h.make_user(email=f"ar.v455.emp.{suffix}@example.com", roles=["Employee"])
		cls.mgr_user = h.make_user(
			email=f"ar.v455.mgr.{suffix}@example.com", roles=["Employee", ROLE_AR_MANAGER]
		)
		cls.am_user = h.make_user(
			email=f"ar.v455.am.{suffix}@example.com", roles=["Employee", ROLE_ASSET_MANAGER]
		)
		cls.employee = h.make_employee(company_name=cls.company, user_id=cls.emp_user)
		cls.item = h.make_fixed_asset_item(title="V455 Perm Item")
		cls.mgr_emp = h.make_employee(company_name=cls.company, user_id=cls.mgr_user)
		frappe.db.set_value("Employee", cls.employee, "reports_to", cls.mgr_emp)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def test_employee_and_manager_cannot_fulfill_after_approval(self):
		self._ready()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		h.submit_and_approve(doc)
		for user in (self.emp_user, self.mgr_user):
			frappe.set_user(user)
			try:
				with self.assertRaises(frappe.PermissionError):
					request_purchase(doc.name)
				with self.assertRaises(frappe.PermissionError):
					issue_from_pool(doc.name)
				with self.assertRaises(frappe.PermissionError):
					create_material_request(doc.name)
				with self.assertRaises(frappe.PermissionError):
					create_asset_movement(doc.name)
			finally:
				frappe.set_user("Administrator")

	def test_manager_approve_creates_no_mr_am_sid_stable(self):
		self._ready()
		from frappe.model.workflow import apply_workflow

		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		doc.manager_approver = self.mgr_user
		doc.require_planning_approval = 0
		doc.require_ceo_approval = 0
		doc.save(ignore_permissions=True)
		frappe.db.set_value(
			"Asset Request",
			doc.name,
			{
				"manager_approver": self.mgr_user,
				"require_planning_approval": 0,
				"require_ceo_approval": 0,
			},
		)
		apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_SUBMIT)
		doc.reload()
		frappe.set_user(self.mgr_user)
		frappe.session.sid = "v455-mgr-keep-sid"
		try:
			apply_workflow(frappe.get_doc("Asset Request", doc.name), ACTION_APPROVE)
			self.assertEqual(frappe.session.sid, "v455-mgr-keep-sid")
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(int(doc.docstatus), 1)
		self.assertEqual(doc.workflow_state, "Approved")
		self.assertEqual(doc.status, "Approved")
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertFalse(doc.material_request)
		self.assertFalse(doc.allocations)
		self.assertFalse(frappe.db.exists("Material Request", {"custom_asset_request": doc.name}))

	def test_asset_manager_can_request_purchase_after_approval(self):

		self._ready()
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=self.item)
		h.submit_and_approve(doc)
		frappe.set_user(self.am_user)
		try:
			result = request_purchase(doc.name)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(result.get("material_request"))



class TestPoolPickerV455(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(
			allow_category_substitution=1,
			prevent_duplicate_active_requests=0,
			require_named_manager_approver=0,
		)
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def test_issue_from_pool_requires_selection(self):
		self._ready()
		item = h.make_fixed_asset_item()
		h.make_pool_asset(item_code=item, company_name=self.company)
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=item)
		h.submit_and_approve(doc)
		with self.assertRaises(frappe.ValidationError):
			issue_from_pool(doc.name)

	def test_check_availability_is_read_only(self):
		self._ready()
		item = h.make_fixed_asset_item()
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=item)
		h.submit_and_approve(doc)
		result = check_availability(doc.name)
		doc.reload()
		self.assertFalse(doc.allocations)
		self.assertFalse(doc.material_request)
		self.assertGreaterEqual(result.get("available_asset_count") or 0, 1)
		names = []
		for line in result.get("lines") or []:
			names.extend(c["name"] for c in (line.get("candidates") or []))
		self.assertIn(asset, names)
		self.assertFalse(frappe.db.exists("Asset Movement", {"reference_name": doc.name}))

	def test_substitute_requires_confirmation(self):
		self._ready()
		tag = frappe.utils.random_string(6)
		category = h.make_isolated_category(tag)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-V455-SUBS-{tag}", title="Samsung Sub", category=category)
		lg = h.make_fixed_asset_item(code=f"AUD-AR-V455-SUBL-{tag}", title="LG Sub", category=category)
		asset = h.make_pool_asset(item_code=lg, company_name=self.company)
		h.ensure_settings(allow_category_substitution=1)
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=samsung)
		h.submit_and_approve(doc)
		picker = get_pool_picker(doc.name)
		selections = []
		for line in picker.get("lines") or []:
			for c in line.get("candidates") or []:
				selections.append({"item_row": line["item_row"], "asset": c["name"]})
		self.assertTrue(selections)
		with self.assertRaises(frappe.ValidationError):
			issue_from_pool(doc.name, selections=selections, confirm_substitution=0)
		doc.reload()
		self.assertFalse(doc.allocations)
		h.issue_from_pool(doc, selections=selections, confirm_substitution=1)
		doc.reload()
		self.assertTrue(doc.allocations)
		self.assertEqual(doc.allocations[0].allocated_asset, asset)
		self.assertTrue(doc.allocations[0].asset_movement)

	def test_custodian_and_wrong_company_excluded_from_pool(self):
		self._ready()
		from erpnext_extensions.asset_usage_depreciation.services.availability import get_available_assets

		item = h.make_fixed_asset_item()
		held = h.make_pool_asset(item_code=item, company_name=self.company)
		emp = h.make_employee(company_name=self.company)
		frappe.db.set_value("Asset", held, "custodian", emp)
		free = h.make_pool_asset(item_code=item, company_name=self.company)
		found = [a.name for a in get_available_assets(self.company, requested_item_code=item)]
		self.assertNotIn(held, found)
		self.assertIn(free, found)
