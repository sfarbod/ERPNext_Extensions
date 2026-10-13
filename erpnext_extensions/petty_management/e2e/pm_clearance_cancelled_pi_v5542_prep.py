# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v5.5.42 cancelled-PI Return + PI cancel guard Playwright."""

from __future__ import annotations

import frappe
from frappe.utils import flt, today
from frappe.utils.password import update_password

from erpnext_extensions.petty_management.services.purchase_invoice_clearance_guard import (
	report_legacy_cancelled_pi_on_clearances,
)
from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_clearance_cancelled_pi_return_v5542 import (
	_submit_dedicated_pi,
)
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import REVIEWER_ROLE

PASSWORD = "pm_v5542_e2e_1"
HOLDER = "pm_clr_v5542_holder@example.com"
MANAGER = "pm_clr_v5542_mgr@example.com"
REVIEWER = "pm_clr_v5542_rev@example.com"
AMOUNT = 5_000.0


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


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


def _bootstrap_users_and_settings():
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	if not tpm.COMPANY:
		frappe.throw("No Company")
	from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
		_rebuild_pm_clearance_workflow,
		_seed_assignment_rules,
	)

	_rebuild_pm_clearance_workflow()
	_seed_assignment_rules()
	desk = ["Accounts User", "Employee", "Desk User", "System Manager"]
	holder = _ensure_user(HOLDER, ["Petty Management User", *desk])
	manager = _ensure_user(
		MANAGER, ["Petty Management User", "Expense Approver", "Petty Management Accountant", *desk]
	)
	reviewer = _ensure_user(
		REVIEWER, [REVIEWER_ROLE, "Petty Management User", "Petty Management Accountant", *desk]
	)
	settings = frappe.get_single("PM Settings")
	settings.db_set("require_named_manager_approver", 1, update_modified=False)
	settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)
	settings.db_set("finance_manager", reviewer, update_modified=False)
	return holder, manager, reviewer


def _advance_clearance_to_pending_finance(cl_name: str, holder: str, manager: str) -> None:
	"""Drive real workflow so Desk Actions toolbar is populated (not db_set only)."""
	from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow

	frappe.db.set_value(
		"PM Clearance",
		cl_name,
		{"owner": holder, "manager_approver": manager},
		update_modified=False,
	)
	frappe.db.commit()
	frappe.set_user(holder)
	apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Submit Finance Review")
	frappe.set_user(manager)
	apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Manager Approve")
	frappe.set_user("Administrator")
	frappe.db.commit()


@frappe.whitelist()
def prepare_cancelled_pi_return_fixture() -> dict:
	"""Pending Finance Clearance with one cancelled linked PI (legacy-style)."""
	holder, manager, reviewer = _bootstrap_users_and_settings()
	pi = _submit_dedicated_pi(AMOUNT)
	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)
	req, _pe = tpm._fund_pm_request(emp, AMOUNT)

	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi,
			"allocated_amount": AMOUNT,
			"outstanding_amount": AMOUNT,
		},
	)
	cl.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": AMOUNT},
	)
	cl.flags.ignore_mandatory = True
	cl.insert(ignore_permissions=True)
	_advance_clearance_to_pending_finance(cl.name, holder, manager)
	# Legacy: cancel PI while still linked (bypass guard via db)
	frappe.db.set_value("Purchase Invoice", pi, "docstatus", 2, update_modified=False)
	if frappe.db.has_column("Purchase Invoice", "status"):
		frappe.db.set_value("Purchase Invoice", pi, "status", "Cancelled", update_modified=False)
	frappe.db.commit()
	ws = frappe.db.get_value("PM Clearance", cl.name, "workflow_state")
	return {
		"pm_clearance": cl.name,
		"purchase_invoice": pi,
		"password": PASSWORD,
		"holder": holder,
		"manager": manager,
		"reviewer": reviewer,
		"workflow_state": _wf_title(ws),
	}


@frappe.whitelist()
def prepare_draft_pi_cancel_guard_fixture() -> dict:
	"""Draft Clearance referencing a submitted PI — cancel must be blocked."""
	holder, manager, reviewer = _bootstrap_users_and_settings()
	pi = _submit_dedicated_pi(AMOUNT)
	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)
	req, _pe = tpm._fund_pm_request(emp, AMOUNT)
	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi,
			"allocated_amount": AMOUNT,
			"outstanding_amount": AMOUNT,
		},
	)
	cl.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": AMOUNT},
	)
	cl.flags.ignore_mandatory = True
	cl.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Clearance",
		cl.name,
		{"owner": holder, "manager_approver": manager, "status": "Draft"},
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"pm_clearance": cl.name,
		"purchase_invoice": pi,
		"password": PASSWORD,
		"holder": holder,
		"manager": manager,
		"reviewer": reviewer,
	}


@frappe.whitelist()
def snapshot_clearance(name: str) -> dict:
	doc = frappe.get_doc("PM Clearance", name)
	return {
		"name": doc.name,
		"docstatus": doc.docstatus,
		"status": doc.status,
		"workflow_state": _wf_title(doc.workflow_state),
		"pi_names": [r.purchase_invoice for r in (doc.details or []) if r.purchase_invoice],
		"total_expense_amount": flt(doc.total_expense_amount),
		"journal_entry": getattr(doc, "journal_entry", None),
	}


@frappe.whitelist()
def replace_pi_on_clearance(clearance: str, new_amount: float | None = None) -> dict:
	"""Replace first PI row with a fresh submitted PI (correction after Return)."""
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	amount = flt(new_amount) or AMOUNT
	new_pi = _submit_dedicated_pi(amount)
	doc = frappe.get_doc("PM Clearance", clearance)
	if not doc.details:
		frappe.throw("No detail rows")
	old = doc.details[0].purchase_invoice
	doc.details[0].purchase_invoice = new_pi
	doc.details[0].allocated_amount = amount
	doc.details[0].outstanding_amount = amount
	if doc.request_allocations:
		doc.request_allocations[0].allocated_amount = amount
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"old_pi": old, "new_pi": new_pi, "clearance": doc.name}


@frappe.whitelist()
def legacy_cancelled_pi_report() -> list:
	return report_legacy_cancelled_pi_on_clearances()
