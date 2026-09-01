# Copyright (c) 2026, ERPNext Extensions contributors
"""Playwright prep for v5.0.6 pending remark-only save."""

from __future__ import annotations

import frappe

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.e2e.pm_workflow_v502_prep import (
	HOLDER,
	MANAGER_GOOD,
	PASSWORD,
	_configure_settings,
	_ensure_user,
	_new_request_draft,
	_rebuild_workflows,
	_submit_request_pending_manager,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm


@frappe.whitelist()
def prepare_v506_fixtures() -> dict:
	frappe.set_user("Administrator")
	_rebuild_workflows()
	tpm._ensure_company_context()
	if not tpm.COMPANY:
		frappe.throw("No Company on site")

	_configure_settings()
	_ensure_user(HOLDER, ["Petty Management User", "Accounts User", "Employee"])
	_ensure_user(MANAGER_GOOD, ["Petty Management User", "Expense Approver", "Accounts User", "Employee"])
	from frappe.utils.password import update_password

	update_password(HOLDER, PASSWORD)
	update_password(MANAGER_GOOD, PASSWORD)

	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", MANAGER_GOOD, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", HOLDER, update_modified=False)
	tpm._make_holder(emp)

	request_pending = _submit_request_pending_manager(_new_request_draft(emp, owner=HOLDER), MANAGER_GOOD)
	clearance_pending = None
	clearance_skipped = None
	try:
		from erpnext_extensions.petty_management.e2e.pm_workflow_v502_prep import (
			_insert_draft_pi,
			_new_clearance_draft,
			_advance_clearance_pending_manager,
		)

		pi = _insert_draft_pi()
		clearance_pending = _new_clearance_draft(emp, pi, owner=HOLDER)
		_advance_clearance_pending_manager(clearance_pending, MANAGER_GOOD)
	except Exception as exc:
		clearance_skipped = str(exc)

	frappe.db.commit()
	return {
		**e2e_run_context(),
		"password": PASSWORD,
		"users": {
			"holder": {"email": HOLDER, "password": PASSWORD},
			"manager": {"email": MANAGER_GOOD, "password": PASSWORD},
		},
		"request_pending_manager": request_pending,
		"clearance_pending_manager": clearance_pending,
		"clearance_skipped": clearance_skipped,
	}


@frappe.whitelist()
def save_request_remark_as_holder(pm_request: str, holder_email: str, remark: str) -> str:
	frappe.set_user(holder_email)
	doc = frappe.get_doc("PM Request", pm_request)
	doc.remark = remark
	doc.save()
	frappe.db.commit()
	frappe.set_user("Administrator")
	return doc.remark or ""


@frappe.whitelist()
def attempt_illegal_request_edit(pm_request: str, holder_email: str) -> dict:
	frappe.set_user(holder_email)
	doc = frappe.get_doc("PM Request", pm_request)
	try:
		doc.details[0].advance_amount = 99999
		doc.save()
		ok = True
		error = None
	except Exception as exc:
		ok = False
		error = str(exc)
	finally:
		frappe.set_user("Administrator")
	return {"ok": ok, "error": error}


@frappe.whitelist()
def save_clearance_remark_as_holder(pm_clearance: str, holder_email: str, remark: str) -> str:
	frappe.set_user(holder_email)
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	doc.remark = remark
	doc.save()
	frappe.db.commit()
	frappe.set_user("Administrator")
	return doc.remark or ""
