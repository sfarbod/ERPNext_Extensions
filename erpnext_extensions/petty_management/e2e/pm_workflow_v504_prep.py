# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep for v5.0.4 repeated Return / resubmit Playwright E2E."""

from __future__ import annotations

import frappe

from erpnext_extensions.petty_management.e2e.pm_workflow_v502_prep import (
	HOLDER,
	MANAGER_GOOD,
	PASSWORD,
	REVIEWER,
	_advance_clearance_pending_finance,
	_advance_clearance_pending_manager,
	_new_clearance_draft,
	_new_request_draft,
	_submit_request_pending_manager,
	_wf_title,
	apply_return_as_user,
	get_clearance_snapshot,
	get_request_snapshot,
	prepare_v502_fixtures,
	workflow_actions,
)
from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm


@frappe.whitelist()
def prepare_v504_fixtures() -> dict:
	"""Fresh Request + Clearance fixtures for repeated Return cycles."""
	base = prepare_v502_fixtures()
	frappe.set_user("Administrator")
	tpm._ensure_company_context()

	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", MANAGER_GOOD, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", HOLDER, update_modified=False)
	tpm._make_holder(emp)

	request_repeat = _new_request_draft(emp, owner=HOLDER)
	request_repeat = _submit_request_pending_manager(request_repeat, MANAGER_GOOD)

	clearance_repeat_manager = None
	clearance_repeat_finance = None
	clearance_skipped = base.get("clearance_skipped")
	try:
		from erpnext_extensions.petty_management.e2e.pm_workflow_v502_prep import _insert_draft_pi

		pi_name = _insert_draft_pi()
		clearance_repeat_manager = _new_clearance_draft(emp, pi_name, owner=HOLDER)
		_advance_clearance_pending_manager(clearance_repeat_manager, MANAGER_GOOD)
		clearance_repeat_finance = _new_clearance_draft(emp, pi_name, owner=HOLDER)
		_advance_clearance_pending_finance(clearance_repeat_finance, MANAGER_GOOD, REVIEWER)
	except Exception as exc:
		clearance_skipped = str(exc)

	frappe.db.commit()
	return {
		**base,
		"request_repeat": request_repeat,
		"clearance_repeat_manager": clearance_repeat_manager,
		"clearance_repeat_finance": clearance_repeat_finance,
		"clearance_skipped": clearance_skipped,
	}


@frappe.whitelist()
def submit_request_as_holder(pm_request: str, holder_email: str) -> dict:
	frappe.set_user(holder_email)
	try:
		out = apply_pm_workflow(frappe.get_doc("PM Request", pm_request), "PM Submit for Approval")
		frappe.db.commit()
		return {"name": out.name, "workflow_title": _wf_title(out.workflow_state)}
	finally:
		frappe.set_user("Administrator")


@frappe.whitelist()
def submit_clearance_as_holder(pm_clearance: str, holder_email: str) -> dict:
	frappe.set_user(holder_email)
	try:
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", pm_clearance), "PM Submit Finance Review")
		frappe.db.commit()
		return {"name": out.name, "workflow_title": _wf_title(out.workflow_state)}
	finally:
		frappe.set_user("Administrator")


@frappe.whitelist()
def manager_approve_clearance(pm_clearance: str, manager_email: str) -> dict:
	frappe.set_user(manager_email)
	try:
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", pm_clearance), "PM Manager Approve")
		frappe.db.commit()
		return {"name": out.name, "workflow_title": _wf_title(out.workflow_state)}
	finally:
		frappe.set_user("Administrator")


@frappe.whitelist()
def run_request_repeat_cycle(pm_request: str, manager_email: str, holder_email: str) -> dict:
	"""One Return + resubmit cycle; returns snapshots for assertions."""
	before = get_request_snapshot(pm_request)
	returned = apply_return_as_user("PM Request", pm_request, manager_email)
	after_return = get_request_snapshot(pm_request)
	submitted = submit_request_as_holder(pm_request, holder_email)
	after_submit = get_request_snapshot(pm_request)
	actions = workflow_actions("PM Request", pm_request, manager_email)
	return {
		"before": before,
		"returned": returned,
		"after_return": after_return,
		"submitted": submitted,
		"after_submit": after_submit,
		"manager_actions": actions,
	}


@frappe.whitelist()
def run_clearance_repeat_cycle(
	pm_clearance: str,
	actor_email: str,
	holder_email: str,
	*,
	manager_email: str | None = None,
) -> dict:
	before = get_clearance_snapshot(pm_clearance)
	returned = apply_return_as_user("PM Clearance", pm_clearance, actor_email)
	after_return = get_clearance_snapshot(pm_clearance)
	submitted = submit_clearance_as_holder(pm_clearance, holder_email)
	after_submit = get_clearance_snapshot(pm_clearance)
	if manager_email and _wf_title(after_submit.get("workflow_state")) == "Pending Manager Approval":
		approved = manager_approve_clearance(pm_clearance, manager_email)
	else:
		approved = None
	after = get_clearance_snapshot(pm_clearance)
	actions = workflow_actions("PM Clearance", pm_clearance, actor_email)
	return {
		"before": before,
		"returned": returned,
		"after_return": after_return,
		"submitted": submitted,
		"approved": approved,
		"after": after,
		"actor_actions": actions,
	}
