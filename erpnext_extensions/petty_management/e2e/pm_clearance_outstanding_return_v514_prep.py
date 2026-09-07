# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v5.1.4 Clearance Return vs live PI outstanding Playwright."""

from __future__ import annotations

import frappe
from frappe.utils import flt, today
from frappe.utils.password import update_password

from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import REVIEWER_ROLE

PASSWORD = "pm_v514_e2e_1"
HOLDER = "pm_clr_v514_holder@example.com"
MANAGER = "pm_clr_v514_mgr@example.com"
REVIEWER = "pm_clr_v514_rev@example.com"

ALLOCATED = 10_000.0
LIVE_OUTSTANDING = 1_200.0


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


def _existing_submitted_pi() -> str:
	rows = frappe.db.sql(
		"""
		select name
		from `tabPurchase Invoice`
		where docstatus = 1 and ifnull(outstanding_amount, 0) >= 1
		order by modified desc
		limit 1
		""",
		as_dict=True,
	)
	if not rows:
		frappe.throw("No submitted Purchase Invoice available for v5.1.4 E2E")
	return rows[0].name


@frappe.whitelist()
def prepare_v514_deadlock_fixtures() -> dict:
	"""Pending Finance Clearance with stale allocated vs reduced live PI outstanding."""
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
	manager = _ensure_user(MANAGER, ["Petty Management User", "Expense Approver", *desk])
	reviewer = _ensure_user(REVIEWER, [REVIEWER_ROLE, "Petty Management User", *desk])

	settings = frappe.get_single("PM Settings")
	settings.db_set("require_named_manager_approver", 1, update_modified=False)
	settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)

	pi_name = _existing_submitted_pi()
	original_outstanding = flt(frappe.db.get_value("Purchase Invoice", pi_name, "outstanding_amount"))

	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)

	frappe.db.set_value(
		"Purchase Invoice", pi_name, "outstanding_amount", ALLOCATED, update_modified=False
	)
	req, pe = tpm._fund_pm_request(emp, ALLOCATED)

	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi_name,
			"allocated_amount": ALLOCATED,
			"outstanding_amount": ALLOCATED,
		},
	)
	cl.append(
		"request_allocations",
		{
			"funding_source_type": "PM Request",
			"pm_request": req,
			"allocated_amount": ALLOCATED,
		},
	)
	cl.flags.ignore_mandatory = True
	cl.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Clearance",
		cl.name,
		{
			"workflow_state": resolve_workflow_state_link("Pending Finance Review"),
			"owner": holder,
			"manager_approver": manager,
		},
		update_modified=False,
	)
	frappe.db.set_value(
		"Purchase Invoice", pi_name, "outstanding_amount", LIVE_OUTSTANDING, update_modified=False
	)
	frappe.db.commit()

	return {
		"pm_clearance": cl.name,
		"purchase_invoice": pi_name,
		"pm_request": req,
		"payment_entry": pe,
		"employee": emp,
		"allocated": ALLOCATED,
		"live_outstanding": LIVE_OUTSTANDING,
		"original_pi_outstanding": original_outstanding,
		"password": PASSWORD,
		"users": {
			"holder": {"email": holder, "password": PASSWORD},
			"manager": {"email": manager, "password": PASSWORD},
			"reviewer": {"email": reviewer, "password": PASSWORD},
		},
	}


@frappe.whitelist()
def restore_pi_outstanding(purchase_invoice: str, outstanding: float) -> None:
	frappe.set_user("Administrator")
	frappe.db.set_value(
		"Purchase Invoice",
		purchase_invoice,
		"outstanding_amount",
		flt(outstanding),
		update_modified=False,
	)
	frappe.db.commit()


@frappe.whitelist()
def get_clearance_snapshot(pm_clearance: str) -> dict:
	frappe.set_user("Administrator")
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	pi = doc.details[0].purchase_invoice if doc.details else None
	live = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount")) if pi else None
	return {
		"name": doc.name,
		"workflow_state": _wf_title(doc.workflow_state),
		"status": doc.status,
		"allocated_amount": flt(doc.details[0].allocated_amount) if doc.details else None,
		"child_outstanding": flt(doc.details[0].outstanding_amount) if doc.details else None,
		"live_outstanding": live,
		"purchase_invoice": pi,
	}


@frappe.whitelist()
def fix_clearance_allocation_as_holder(pm_clearance: str, holder_email: str, amount: float) -> dict:
	"""Draft correction after Return — set allocated to live outstanding."""
	frappe.set_user(holder_email)
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	amt = flt(amount)
	doc.details[0].allocated_amount = amt
	if doc.request_allocations:
		doc.request_allocations[0].allocated_amount = amt
	doc.save()
	frappe.db.commit()
	frappe.set_user("Administrator")
	return get_clearance_snapshot(pm_clearance)
