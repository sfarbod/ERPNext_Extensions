# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for PM Clearance Return + Remarks Playwright (v4.8.3)."""

from __future__ import annotations

import frappe
from frappe.model.workflow import get_transitions
from frappe.utils import cint, flt, today
from frappe.utils.password import update_password

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm

PASSWORD = "pm_v483_test_1"
HOLDER = "pm_clr_v483_holder@example.com"
MANAGER = "pm_clr_v483_mgr@example.com"
REVIEWER = "pm_clr_v483_rev@example.com"


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _desk_roles() -> list[str]:
	return ["Accounts User", "Employee"]


def _ensure_user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0][:30],
				"send_welcome_email": 0,
				"user_type": "System User",
				"enabled": 1,
			}
		)
		u.insert(ignore_permissions=True)
	else:
		u = frappe.get_doc("User", email)
	u.roles = []
	for role in roles:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		u.append("roles", {"role": role})
	u.enabled = 1
	u.save(ignore_permissions=True)
	update_password(email, PASSWORD)
	frappe.db.commit()
	return email


def _funded_employee(holder: str, manager: str) -> tuple[str, str]:
	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)
	req, _pe = tpm._fund_pm_request(emp, 100_000.0)
	fa = resolve_workflow_state_link("Finance Approved")
	frappe.db.set_value(
		"PM Request",
		req,
		{"workflow_state": fa, "status": "Paid", "payment_status": "Paid"},
		update_modified=False,
	)
	frappe.db.commit()
	return emp, req


def _new_clearance(emp: str, req: str, pi_name: str, allocated: float) -> str:
	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi_name,
			"allocated_amount": allocated,
		},
	)
	cl.append(
		"request_allocations",
		{
			"funding_source_type": "PM Request",
			"pm_request": req,
			"allocated_amount": allocated,
		},
	)
	cl.insert(ignore_permissions=True)
	frappe.db.commit()
	return cl.name


def _advance_to_pending_manager(cl_name: str, manager: str) -> None:
	frappe.db.set_value(
		"PM Clearance",
		cl_name,
		{"manager_approver": manager, "finance_approver": None},
		update_modified=False,
	)
	cl = frappe.get_doc("PM Clearance", cl_name)
	if _wf_title(cl.workflow_state) in ("", "Draft"):
		apply_pm_workflow(cl, "PM Submit Finance Review")
	frappe.db.commit()


def _advance_to_pending_finance(cl_name: str, manager: str) -> None:
	_advance_to_pending_manager(cl_name, manager)
	frappe.set_user(manager)
	cl = frappe.get_doc("PM Clearance", cl_name)
	if _wf_title(cl.workflow_state) == "Pending Manager Approval":
		apply_pm_workflow(cl, "PM Manager Approve")
	frappe.set_user("Administrator")
	frappe.db.commit()


def _advance_to_approved(cl_name: str, manager: str, reviewer: str) -> None:
	_advance_to_pending_finance(cl_name, manager)
	frappe.set_user(reviewer)
	cl = frappe.get_doc("PM Clearance", cl_name)
	apply_pm_workflow(cl, "PM Finance Approve")
	frappe.set_user("Administrator")
	frappe.db.commit()


def _workflow_actions(pm_clearance: str, user: str) -> list[str]:
	frappe.set_user(user)
	try:
		doc = frappe.get_doc("PM Clearance", pm_clearance)
		return [t.get("action") for t in get_transitions(doc) if t.get("action")]
	finally:
		frappe.set_user("Administrator")


@frappe.whitelist()
def prepare_v483_fixtures():
	from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
		_rebuild_pm_clearance_workflow,
	)

	frappe.set_user("Administrator")
	_rebuild_pm_clearance_workflow()
	tpm._ensure_company_context()
	if not tpm.COMPANY:
		frappe.throw("No Company")

	review_role = "Petty Management Clearance Reviewer"
	if not frappe.db.exists("Role", review_role):
		frappe.get_doc({"doctype": "Role", "role_name": review_role}).insert(ignore_permissions=True)
	settings = frappe.get_single("PM Settings")
	settings.db_set("require_named_manager_approver", 1, update_modified=False)
	settings.db_set("clearance_finance_review_role", review_role, update_modified=False)
	settings.db_set("finance_manager", REVIEWER, update_modified=False)

	_ensure_user(HOLDER, ["Petty Management User", *_desk_roles()])
	_ensure_user(
		MANAGER,
		["Petty Management User", "Expense Approver", review_role, *_desk_roles()],
	)
	_ensure_user(REVIEWER, [review_role, *_desk_roles()])

	emp, req = _funded_employee(HOLDER, MANAGER)
	pi = tpm._make_pi_outstanding(1_000)
	pi.insert(ignore_permissions=True)
	pi.submit()
	alloc = flt(pi.outstanding_amount or pi.grand_total or 1000)

	pending_mgr = _new_clearance(emp, req, pi.name, alloc)
	_advance_to_pending_manager(pending_mgr, MANAGER)

	pending_fin = _new_clearance(emp, req, pi.name, alloc)
	_advance_to_pending_finance(pending_fin, MANAGER)

	approved = _new_clearance(emp, req, pi.name, alloc)
	_advance_to_approved(approved, MANAGER, REVIEWER)

	remark_marker = f"v483-remark-{pending_fin[-8:]}"
	frappe.db.set_value("PM Clearance", pending_fin, "remark", remark_marker, update_modified=False)
	frappe.db.commit()

	return {
		**e2e_run_context(),
		"users": {
			"holder": {"email": HOLDER, "password": PASSWORD},
			"manager": {"email": MANAGER, "password": PASSWORD},
			"reviewer": {"email": REVIEWER, "password": PASSWORD},
		},
		"pending_manager": pending_mgr,
		"pending_finance": pending_fin,
		"approved": approved,
		"remark_marker": remark_marker,
		"server_actions": {
			"pending_manager": _workflow_actions(pending_mgr, MANAGER),
			"pending_finance": _workflow_actions(pending_fin, REVIEWER),
			"approved": _workflow_actions(approved, REVIEWER),
		},
	}
