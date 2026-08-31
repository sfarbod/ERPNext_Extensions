# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.4 — Legacy Pending* docstatus rewind + Return for Correction on migrated docs."""

from __future__ import annotations

import json
import unittest

import frappe
from frappe.utils import today

from erpnext_extensions.petty_management.services.legacy_pending_lifecycle_service import (
	assert_post_migration_lifecycle_invariants,
	convert_legacy_pending_doc_to_draft_lifecycle,
	find_legacy_pending_submitted_docs,
	migrate_all_legacy_pending_submitted_docs,
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


class TestPMLegacyPendingLifecycleV484(unittest.TestCase):
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
		for role in (
			"Petty Management User",
			"Petty Management Accountant",
			"Petty Management Clearance Reviewer",
		):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
			if role not in [r.role for r in u.roles]:
				u.append("roles", {"role": role})
		u.save(ignore_permissions=True)
		return user

	def _make_legacy_pending_clearance_pfr(self):
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
		pfr = resolve_workflow_state_link("Pending Finance Review")
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			{
				"workflow_state": pfr,
				"status": "Pending Approval",
				"docstatus": 1,
				"manager_approver": frappe.session.user,
			},
			update_modified=False,
		)
		cl.reload()
		return cl

	def _make_legacy_pending_request(self):
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		self._configure_approvers(emp)
		req = frappe.new_doc("PM Request")
		req.company = pm_ct.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1500, "description": "v484 legacy"})
		req.insert(ignore_permissions=True)
		pma = resolve_workflow_state_link("Pending Manager Approval")
		frappe.db.set_value(
			"PM Request",
			req.name,
			{
				"workflow_state": pma,
				"status": "Pending Manager Approval",
				"docstatus": 1,
				"manager_approver": frappe.session.user,
				"finance_approver": frappe.session.user,
			},
			update_modified=False,
		)
		req.reload()
		return req

	def test_find_legacy_pending_submitted_docs(self):
		cl = self._make_legacy_pending_clearance_pfr()
		stats = find_legacy_pending_submitted_docs()
		self.assertIn(cl.name, stats["clearance_names"])

	def test_legacy_clearance_pfr_rewind_preserves_identity(self):
		cl = self._make_legacy_pending_clearance_pfr()
		name = cl.name
		ws = cl.workflow_state
		owner = cl.owner
		mgr = cl.manager_approver
		comment_before = frappe.db.count(
			"Comment", {"reference_doctype": "PM Clearance", "reference_name": name}
		)
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": "PM Clearance",
				"reference_name": name,
				"content": "preserve me v484",
			}
		).insert(ignore_permissions=True)

		row = convert_legacy_pending_doc_to_draft_lifecycle(cl)
		cl.reload()

		self.assertEqual(cl.name, name)
		self.assertEqual(cl.workflow_state, ws)
		self.assertEqual(cl.owner, owner)
		self.assertEqual(cl.manager_approver, mgr)
		self.assertEqual(cl.docstatus, 0)
		self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")
		self.assertEqual((cl.status or "").strip(), "Pending Approval")
		self.assertEqual(row["before"]["comment_count"], row["after"]["comment_count"])
		self.assertEqual(row["before"]["version_count"], row["after"]["version_count"])
		self.assertEqual(row["before"]["open_todo_count"], row["after"]["open_todo_count"])

	def test_legacy_clearance_return_for_correction_after_rewind(self):
		cl = self._make_legacy_pending_clearance_pfr()
		name = cl.name
		convert_legacy_pending_doc_to_draft_lifecycle(cl)
		cl.reload()
		self.assertEqual(cl.docstatus, 0)

		out = apply_pm_workflow(cl, "PM Return for Correction")
		out.reload()
		self.assertEqual(out.name, name)
		self.assertEqual(out.docstatus, 0)
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		self.assertFalse((out.manager_approver or "").strip())

	def test_legacy_request_rewind_and_return(self):
		req = self._make_legacy_pending_request()
		name = req.name
		ws = req.workflow_state
		convert_legacy_pending_doc_to_draft_lifecycle(req)
		req.reload()
		self.assertEqual(req.name, name)
		self.assertEqual(req.workflow_state, ws)
		self.assertEqual(req.docstatus, 0)

		out = apply_pm_workflow(req, "PM Return for Correction")
		out.reload()
		self.assertEqual(out.name, name)
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	def test_migrate_all_legacy_is_idempotent_for_docstatus_zero(self):
		cl = self._make_legacy_pending_clearance_pfr()
		report = migrate_all_legacy_pending_submitted_docs()
		self.assertTrue(any(r["name"] == cl.name for r in report["converted_clearances"]))
		cl.reload()
		self.assertEqual(cl.docstatus, 0)

		report2 = migrate_all_legacy_pending_submitted_docs()
		self.assertEqual(report2["request_count"], 0)
		self.assertEqual(report2["clearance_count"], 0)
		assert_post_migration_lifecycle_invariants()

	def test_patch_execute_applies_v472_cutover_when_legacy_converted(self):
		from unittest.mock import patch

		from erpnext_extensions.patches.post_model_sync import migrate_pm_legacy_pending_lifecycle_v484 as v484
		from erpnext_extensions.patches.post_model_sync import migrate_pm_draft_approval_v472 as v472

		cl = self._make_legacy_pending_clearance_pfr()
		frappe.db.set_default(v472.DEFERRED_FLAG_KEY, json.dumps({"request_count": 1, "clearance_count": 1}))
		frappe.db.set_default(v472.APPLIED_FLAG_KEY, "")

		def _fake_cutover(report: dict) -> dict:
			report["path"] = "applied"
			v472._set_site_flag(v472.APPLIED_FLAG_KEY, "1")
			v472._clear_site_flag(v472.DEFERRED_FLAG_KEY)
			return report

		with (
			patch.object(v484, "is_pm_draft_approval_v472_cutover_complete", return_value=False),
			patch.object(v484, "_apply_draft_approval_cutover", side_effect=_fake_cutover) as cutover,
		):
			v484.execute()

		cutover.assert_called_once()
		cl.reload()
		self.assertEqual(cl.docstatus, 0)
		self.assertTrue(v472.is_pm_draft_approval_v472_cutover_complete())

	def test_rewind_aborts_when_submitted_pe_exists(self):
		from unittest.mock import patch

		req = self._make_legacy_pending_request()
		with patch(
			"erpnext_extensions.petty_management.services.funding_queries.count_payment_entries_for_pm_request",
			return_value={"submitted_payment_entry_count": 1},
		):
			with self.assertRaises(frappe.ValidationError):
				convert_legacy_pending_doc_to_draft_lifecycle(req)

