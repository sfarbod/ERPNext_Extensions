# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Migration / schema verification for Asset Request (idempotent)."""

from __future__ import annotations

import unittest

import frappe

from erpnext_extensions.asset_usage_depreciation.constants import (
	ASSET_REQUEST_ALLOCATION_DOCTYPE,
	ASSET_REQUEST_DOCTYPE,
	ASSET_REQUEST_ITEM_DOCTYPE,
	ASSET_REQUEST_SETTINGS_DOCTYPE,
	COMPANY_FIELD_AR_CEO_MIN_QTY,
	COMPANY_FIELD_AR_DEFAULT_TARGET_LOCATION,
	COMPANY_FIELD_AR_POOL_LOCATION,
	COMPANY_FIELD_AR_REQUIRE_CEO,
	COMPANY_FIELD_AR_REQUIRE_PLANNING,
	ROLE_AR_EXECUTIVE,
	ROLE_AR_MANAGER,
	ROLE_AR_PLANNER,
	ROLE_ASSET_MANAGER,
	WF_ASSET_REQUEST,
)
from erpnext_extensions.asset_usage_depreciation.custom_fields import ensure_custom_fields
from erpnext_extensions.asset_usage_depreciation.services.dimension_service import (
	provision_asset_request_accounting_dimensions,
)
from erpnext_extensions.asset_usage_depreciation.workflow import ensure_asset_request_workflow


def _count(doctype: str, filters: dict) -> int:
	return len(frappe.get_all(doctype, filters=filters, pluck="name"))


class TestAssetRequestMigration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_doctypes_exist(self):
		for dt in (
			ASSET_REQUEST_DOCTYPE,
			ASSET_REQUEST_ITEM_DOCTYPE,
			ASSET_REQUEST_ALLOCATION_DOCTYPE,
			ASSET_REQUEST_SETTINGS_DOCTYPE,
		):
			self.assertTrue(frappe.db.exists("DocType", dt), dt)

	def test_company_and_material_request_custom_fields(self):
		for field in (
			COMPANY_FIELD_AR_REQUIRE_PLANNING,
			COMPANY_FIELD_AR_REQUIRE_CEO,
			COMPANY_FIELD_AR_CEO_MIN_QTY,
			COMPANY_FIELD_AR_POOL_LOCATION,
			COMPANY_FIELD_AR_DEFAULT_TARGET_LOCATION,
		):
			self.assertTrue(frappe.db.has_column("Company", field), field)
		self.assertTrue(frappe.db.has_column("Material Request", "custom_asset_request"))
		self.assertTrue(frappe.db.has_column("Material Request", "custom_created_from_asset_request"))
		self.assertTrue(frappe.db.has_column("Material Request Item", "custom_asset_request_item"))

	def test_roles_and_workflow(self):
		for role in (ROLE_AR_MANAGER, ROLE_AR_PLANNER, ROLE_AR_EXECUTIVE, ROLE_ASSET_MANAGER):
			self.assertTrue(frappe.db.exists("Role", role), role)
		self.assertTrue(frappe.db.exists("Workflow", WF_ASSET_REQUEST))
		wf = frappe.get_doc("Workflow", WF_ASSET_REQUEST)
		self.assertEqual(wf.document_type, ASSET_REQUEST_DOCTYPE)
		self.assertEqual(cint_active(wf), 1)
		states = {s.state for s in wf.states}
		self.assertIn("Pending Manager Approval", states)
		self.assertIn("Approved", states)
		self.assertFalse(frappe.get_meta(ASSET_REQUEST_DOCTYPE).has_field("request_type"))
		pending_mgr = [
			t
			for t in wf.transitions
			if t.state == "Pending Manager Approval"
		]
		self.assertTrue(pending_mgr)
		self.assertFalse(
			any(t.allowed == ROLE_AR_MANAGER for t in pending_mgr),
			"Asset Request Manager must not bypass stamped manager_approver",
		)
		employee_mgr = [t for t in pending_mgr if t.allowed == "Employee"]
		self.assertTrue(employee_mgr)
		for t in employee_mgr:
			self.assertIn("doc.manager_approver == frappe.session.user", t.condition or "")
		self.assertTrue(
			any(t.allowed == "System Manager" and t.action == "AR Approve" for t in pending_mgr)
		)

	def test_asset_manager_has_fulfillment_permlevel(self):
		meta = frappe.get_meta(ASSET_REQUEST_DOCTYPE)
		pl1 = [p for p in meta.permissions if p.role == ROLE_ASSET_MANAGER and int(p.permlevel or 0) == 1]
		self.assertTrue(pl1, "Asset Manager must have permlevel 1 write for fulfillment")
		self.assertTrue(any(int(p.read) and int(p.write) for p in pl1))

	def test_ensure_hooks_are_idempotent(self):
		before_cf = _count("Custom Field", {"fieldname": "custom_asset_request"})
		before_wf = _count("Workflow", {"workflow_name": WF_ASSET_REQUEST})
		before_roles = {r: _count("Role", {"role_name": r}) for r in (ROLE_AR_MANAGER, ROLE_ASSET_MANAGER)}
		ensure_custom_fields()
		ensure_asset_request_workflow()
		ensure_custom_fields()
		ensure_asset_request_workflow()
		self.assertEqual(_count("Custom Field", {"fieldname": "custom_asset_request"}), before_cf)
		self.assertEqual(_count("Workflow", {"workflow_name": WF_ASSET_REQUEST}), before_wf)
		for role, n in before_roles.items():
			self.assertEqual(_count("Role", {"role_name": role}), n, role)
		self.assertEqual(before_wf, 1)
		self.assertEqual(before_cf, 1)

	def test_m1_migrate_without_custom_dimensions_keeps_ar_usable(self):
		provision_asset_request_accounting_dimensions()
		self.assertTrue(frappe.get_meta("Asset Request").has_field("accounting_dimensions_section"))
		self.assertTrue(frappe.get_meta("Asset Request Item").has_field("cost_center"))
		self.assertTrue(frappe.get_meta("Asset Request Item").has_field("project"))

	def test_m2_m3_existing_dimensions_get_fields(self):
		from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

		dim = h.ensure_test_dimension("AR QA Region")
		provision_asset_request_accounting_dimensions()
		fn = dim["fieldname"]
		self.assertTrue(frappe.get_meta("Asset Request").has_field(fn), fn)
		self.assertTrue(frappe.get_meta("Asset Request Item").has_field(fn), fn)
		dim2 = h.ensure_test_dimension("AR QA Channel")
		provision_asset_request_accounting_dimensions()
		fn2 = dim2["fieldname"]
		self.assertTrue(frappe.get_meta("Asset Request").has_field(fn2), fn2)
		self.assertTrue(frappe.get_meta("Asset Request Item").has_field(fn2), fn2)

	def test_m4_second_provision_does_not_duplicate_fields(self):
		from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

		dim = h.ensure_test_dimension("AR QA Region")
		provision_asset_request_accounting_dimensions()
		provision_asset_request_accounting_dimensions()
		rows = frappe.get_all(
			"Custom Field",
			filters={"dt": "Asset Request", "fieldname": dim["fieldname"]},
			pluck="name",
		)
		self.assertEqual(len(rows), 1, rows)
		rows_item = frappe.get_all(
			"Custom Field",
			filters={"dt": "Asset Request Item", "fieldname": dim["fieldname"]},
			pluck="name",
		)
		self.assertEqual(len(rows_item), 1, rows_item)

	def test_m5_new_dimension_after_deploy_needs_no_ar_patch(self):
		"""Creating an Accounting Dimension after install must add AR fields via native on_update."""
		from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
			get_doctypes_with_dimensions,
		)
		from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

		dim = h.ensure_test_dimension("AR QA After Deploy")
		fn = dim["fieldname"]
		self.assertTrue(frappe.get_meta("Asset Request").has_field(fn), fn)
		self.assertTrue(frappe.get_meta("Asset Request Item").has_field(fn), fn)
		self.assertIn("Asset Request", get_doctypes_with_dimensions())
		self.assertIn("Asset Request Item", get_doctypes_with_dimensions())


class TestAssetRequestPermPatchV488(unittest.TestCase):
	def test_patch_strips_invalid_cancel_and_is_idempotent(self):
		from erpnext_extensions.patches.post_model_sync.fix_asset_request_permlevel_permissions_v488 import (
			execute,
		)

		if not frappe.db.exists("DocType", "Asset Request"):
			self.skipTest("Asset Request DocType not migrated")

		row_name = frappe.db.get_value(
			"DocPerm",
			{"parent": "Asset Request", "role": "Asset Manager", "permlevel": 1},
			"name",
		)
		self.assertTrue(row_name)
		before_count = frappe.db.count("DocPerm", {"parent": "Asset Request"})
		frappe.db.set_value("DocPerm", row_name, {"cancel": 1, "submit": 0}, update_modified=False)
		execute()
		after = frappe.db.get_value(
			"DocPerm",
			row_name,
			["cancel", "submit", "create", "amend", "delete", "read", "write"],
			as_dict=True,
		)
		self.assertEqual(int(after.cancel or 0), 0)
		self.assertEqual(int(after.submit or 0), 0)
		self.assertEqual(int(after.create or 0), 0)
		self.assertEqual(int(after.amend or 0), 0)
		self.assertEqual(int(after.delete or 0), 0)
		self.assertEqual(int(after.read or 0), 1)
		self.assertEqual(int(after.write or 0), 1)
		execute()
		self.assertEqual(frappe.db.count("DocPerm", {"parent": "Asset Request"}), before_count)
		self.assertEqual(
			int(frappe.db.get_value("DocPerm", row_name, "cancel") or 0),
			0,
		)


class TestAssetRequestManagerWorkflowPatchV489(unittest.TestCase):
	def test_patch_is_idempotent_and_removes_ar_manager_bypass(self):
		from erpnext_extensions.asset_usage_depreciation.constants import (
			ROLE_AR_MANAGER,
			WF_ASSET_REQUEST,
		)
		from erpnext_extensions.patches.post_model_sync.fix_asset_request_manager_workflow_v489 import (
			execute,
		)

		if not frappe.db.exists("Workflow", WF_ASSET_REQUEST):
			self.skipTest("Asset Request Workflow not migrated")

		execute()
		count1 = frappe.db.count("Workflow Transition", {"parent": WF_ASSET_REQUEST})
		pending1 = frappe.db.count(
			"Workflow Transition",
			{"parent": WF_ASSET_REQUEST, "state": "Pending Manager Approval"},
		)
		execute()
		self.assertEqual(
			frappe.db.count("Workflow Transition", {"parent": WF_ASSET_REQUEST}),
			count1,
		)
		self.assertEqual(
			frappe.db.count(
				"Workflow Transition",
				{"parent": WF_ASSET_REQUEST, "state": "Pending Manager Approval"},
			),
			pending1,
		)
		self.assertEqual(
			frappe.db.count(
				"Workflow Transition",
				{
					"parent": WF_ASSET_REQUEST,
					"state": "Pending Manager Approval",
					"allowed": ROLE_AR_MANAGER,
				},
			),
			0,
		)
		self.assertEqual(count1, pending1 + frappe.db.count(
			"Workflow Transition",
			{"parent": WF_ASSET_REQUEST, "state": ["!=", "Pending Manager Approval"]},
		))
		wf = frappe.get_doc("Workflow", WF_ASSET_REQUEST)
		self.assertEqual(cint_active(wf), 1)


def cint_active(wf) -> int:
	from frappe.utils import cint

	return cint(wf.is_active)
