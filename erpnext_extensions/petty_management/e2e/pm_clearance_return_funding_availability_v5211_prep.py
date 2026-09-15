# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v5.2.11 Clearance Return vs PM Request availability Playwright."""

from __future__ import annotations

import frappe
from frappe.utils import flt, today
from frappe.utils.password import update_password

from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_allocation_context,
)
from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import REVIEWER_ROLE

PASSWORD = "pm_v5211_e2e_1"
HOLDER = "pm_clr_v5211_holder@example.com"
MANAGER = "pm_clr_v5211_mgr@example.com"
REVIEWER = "pm_clr_v5211_rev@example.com"

FUNDED = 10_000.0
RESERVED = 4_000.0


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
		frappe.throw("No submitted Purchase Invoice available for v5.2.11 E2E")
	return rows[0].name


@frappe.whitelist()
def prepare_v5211_funding_deadlock_fixtures() -> dict:
	"""Pending Finance Clearance over-allocated vs live PM Request available."""
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
	settings.db_set("allow_negative_balance", 0, update_modified=False)

	pi_name = _existing_submitted_pi()
	original_outstanding = flt(frappe.db.get_value("Purchase Invoice", pi_name, "outstanding_amount"))

	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)

	frappe.db.set_value("Purchase Invoice", pi_name, "outstanding_amount", FUNDED, update_modified=False)
	req, pe = tpm._fund_pm_request(emp, FUNDED)

	# Settled prior reservation
	cl_settled = frappe.new_doc("PM Clearance")
	cl_settled.company = tpm.COMPANY
	cl_settled.employee = emp
	cl_settled.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl_settled,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi_name,
			"allocated_amount": RESERVED,
			"outstanding_amount": RESERVED,
		},
	)
	cl_settled.append(
		"request_allocations",
		{
			"funding_source_type": "PM Request",
			"pm_request": req,
			"allocated_amount": RESERVED,
		},
	)
	cl_settled.flags.ignore_mandatory = True
	cl_settled.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Clearance",
		cl_settled.name,
		{
			"workflow_state": resolve_workflow_state_link("Approved"),
			"status": "Settled",
			"docstatus": 1,
		},
		update_modified=False,
	)

	headroom = FUNDED - RESERVED
	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi_name,
			"allocated_amount": headroom,
			"outstanding_amount": FUNDED,
		},
	)
	cl.append(
		"request_allocations",
		{
			"funding_source_type": "PM Request",
			"pm_request": req,
			"allocated_amount": headroom,
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
			"total_expense_amount": FUNDED,
		},
		update_modified=False,
	)
	frappe.db.sql(
		"""
		update `tabPM Clearance Detail`
		set allocated_amount=%s, outstanding_amount=%s
		where parent=%s and parenttype='PM Clearance'
		""",
		(FUNDED, FUNDED, cl.name),
	)
	frappe.db.sql(
		"""
		update `tabPM Clearance Request Allocation`
		set allocated_amount=%s
		where parent=%s and parenttype='PM Clearance' and parentfield='request_allocations'
		""",
		(FUNDED, cl.name),
	)
	frappe.db.set_value("Purchase Invoice", pi_name, "outstanding_amount", FUNDED, update_modified=False)
	frappe.db.commit()

	ctx = get_pm_request_allocation_context(req, pm_clearance=cl.name)
	available = flt(ctx.get("available_amount"))

	return {
		"pm_clearance": cl.name,
		"settled_clearance": cl_settled.name,
		"purchase_invoice": pi_name,
		"pm_request": req,
		"payment_entry": pe,
		"employee": emp,
		"allocated": FUNDED,
		"available": available,
		"reserved": RESERVED,
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
	row = doc.request_allocations[0] if doc.request_allocations else None
	req = row.pm_request if row else None
	ctx = get_pm_request_allocation_context(req, pm_clearance=pm_clearance) if req else {}
	return {
		"name": doc.name,
		"workflow_state": _wf_title(doc.workflow_state),
		"status": doc.status,
		"allocated_amount": flt(row.allocated_amount) if row else None,
		"child_available": flt(row.available_amount) if row else None,
		"live_available": flt(ctx.get("available_amount")) if ctx else None,
		"pm_request": req,
		"_assign": frappe.get_all(
			"ToDo",
			filters={"reference_type": "PM Clearance", "reference_name": pm_clearance, "status": "Open"},
			pluck="allocated_to",
		),
	}


@frappe.whitelist()
def fix_clearance_allocation_as_holder(pm_clearance: str, holder_email: str, amount: float) -> dict:
	"""Draft correction after Return — set allocated to live available."""
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
