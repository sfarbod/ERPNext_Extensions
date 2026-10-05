# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v5.5.5 active reservation + Return deadlock Playwright."""

from __future__ import annotations

import frappe
from frappe.utils import flt, today
from frappe.utils.password import update_password

from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_available_amount,
	get_pm_request_paid_amount,
	sum_prior_pm_request_allocations,
)
from erpnext_extensions.petty_management.services.clearance_reservation import (
	clearance_reserves_pm_request_balance_sql,
)
from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import REVIEWER_ROLE

PASSWORD = "pm_v555_e2e_1"
HOLDER = "pm_clr_v555_holder@example.com"
MANAGER = "pm_clr_v555_mgr@example.com"
REVIEWER = "pm_clr_v555_rev@example.com"


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
		select name from `tabPurchase Invoice`
		where docstatus = 1 and ifnull(outstanding_amount, 0) >= 1
		order by modified desc limit 1
		""",
		as_dict=True,
	)
	if not rows:
		frappe.throw("No submitted Purchase Invoice for v5.5.5 E2E")
	return rows[0].name


def _setup_users_settings():
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
	return holder, manager, reviewer


@frappe.whitelist()
def prepare_return_deadlock_fixture() -> dict:
	"""Pending Finance Clearance while Settled siblings consume full Request paid amount."""
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	holder, manager, reviewer = _setup_users_settings()
	pi = _existing_submitted_pi()
	orig = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount"))
	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)
	frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", 1000, update_modified=False)
	req, pe = tpm._fund_pm_request(emp, 1000)

	# Pending allocation while headroom exists
	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi,
			"allocated_amount": 400,
			"outstanding_amount": 1000,
		},
	)
	cl.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": 400},
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
			"status": "Pending Approval",
		},
		update_modified=False,
	)

	# Force settled sibling consuming full paid (legacy over-allocation class)
	settled = frappe.new_doc("PM Clearance")
	settled.company = tpm.COMPANY
	settled.employee = emp
	settled.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		settled,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi,
			"allocated_amount": 1000,
			"outstanding_amount": 1000,
		},
	)
	settled.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": 1000},
	)
	settled.flags.ignore_mandatory = True
	settled.flags.ignore_validate = True
	settled.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Clearance",
		settled.name,
		{
			"workflow_state": resolve_workflow_state_link("Approved"),
			"status": "Settled",
			"docstatus": 1,
		},
		update_modified=False,
	)
	frappe.db.commit()

	return {
		"pm_clearance": cl.name,
		"settled_clearance": settled.name,
		"pm_request": req,
		"payment_entry": pe,
		"purchase_invoice": pi,
		"original_pi_outstanding": orig,
		"paid": get_pm_request_paid_amount(req),
		"available_excl_pending": get_pm_request_available_amount(req, cl.name),
		"password": PASSWORD,
		"users": {
			"holder": {"email": holder, "password": PASSWORD},
			"manager": {"email": manager, "password": PASSWORD},
			"reviewer": {"email": reviewer, "password": PASSWORD},
		},
	}


@frappe.whitelist()
def prepare_draft_reservation_fixture() -> dict:
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	holder, manager, reviewer = _setup_users_settings()
	pi = _existing_submitted_pi()
	orig = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount"))
	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", manager, update_modified=False)
	frappe.db.set_value("Employee", emp, "user_id", holder, update_modified=False)
	tpm._make_holder(emp)
	frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", 1000, update_modified=False)
	req, pe = tpm._fund_pm_request(emp, 1000)

	a = frappe.new_doc("PM Clearance")
	a.company = tpm.COMPANY
	a.employee = emp
	a.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		a,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi,
			"allocated_amount": 700,
			"outstanding_amount": 1000,
		},
	)
	a.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": 700},
	)
	a.flags.ignore_mandatory = True
	a.insert(ignore_permissions=True)
	frappe.db.set_value("PM Clearance", a.name, {"owner": holder}, update_modified=False)
	frappe.db.commit()

	return {
		"clearance_a": a.name,
		"pm_request": req,
		"payment_entry": pe,
		"purchase_invoice": pi,
		"original_pi_outstanding": orig,
		"employee": emp,
		"company": tpm.COMPANY,
		"password": PASSWORD,
		"users": {
			"holder": {"email": holder, "password": PASSWORD},
			"manager": {"email": manager, "password": PASSWORD},
			"reviewer": {"email": reviewer, "password": PASSWORD},
		},
	}


@frappe.whitelist()
def get_clearance_snapshot(pm_clearance: str) -> dict:
	frappe.set_user("Administrator")
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	row = doc.request_allocations[0] if doc.request_allocations else None
	req = row.pm_request if row else None
	return {
		"name": doc.name,
		"workflow_state": _wf_title(doc.workflow_state),
		"status": doc.status,
		"docstatus": doc.docstatus,
		"allocated_amount": flt(row.allocated_amount) if row else None,
		"live_available": get_pm_request_available_amount(req, pm_clearance) if req else None,
		"prior_all": sum_prior_pm_request_allocations(req, None) if req else None,
		"pm_request": req,
		"_assign": frappe.get_all(
			"ToDo",
			filters={"reference_type": "PM Clearance", "reference_name": pm_clearance, "status": "Open"},
			pluck="allocated_to",
		),
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
def legacy_reservation_conflicts() -> dict:
	"""Diagnostic: active reservations > funded amount (do not mutate)."""
	frappe.set_user("Administrator")
	from erpnext_extensions.petty_management.services.allocation_service import get_pm_request_paid_amount

	res = clearance_reserves_pm_request_balance_sql("p")
	rows = frappe.db.sql(
		f"""
		select r.pm_request, sum(r.allocated_amount) as reserved
		from `tabPM Clearance Request Allocation` r
		inner join `tabPM Clearance` p on p.name=r.parent and r.parenttype='PM Clearance'
		where ifnull(r.is_legacy_row,0)=0 and ifnull(r.pm_request,'')!=''
		  and {res}
		group by r.pm_request
		""",
		as_dict=True,
	)
	conflicts = []
	for row in rows:
		paid = get_pm_request_paid_amount(row.pm_request)
		if flt(row.reserved) > paid + 0.01:
			conflicts.append(
				{
					"pm_request": row.pm_request,
					"paid": paid,
					"reserved": flt(row.reserved),
					"over_by": flt(row.reserved) - paid,
				}
			)
	conflicts.sort(key=lambda x: x["over_by"], reverse=True)
	return {"count": len(conflicts), "conflicts": conflicts}


@frappe.whitelist()
def fix_allocation_as_holder(pm_clearance: str, holder_email: str, amount: float) -> dict:
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


@frappe.whitelist()
def try_sibling_alloc(
	pm_request: str,
	employee: str,
	purchase_invoice: str,
	company: str,
	amount: float,
) -> dict:
	"""Insert sibling Draft allocation; roll back. Same validate path as Desk save."""
	frappe.set_user("Administrator")
	amt = flt(amount)
	cl = frappe.new_doc("PM Clearance")
	cl.company = company
	cl.employee = employee
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": purchase_invoice,
			"allocated_amount": amt,
			"outstanding_amount": 1000,
		},
	)
	cl.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": pm_request, "allocated_amount": amt},
	)
	cl.flags.ignore_mandatory = True
	try:
		cl.insert(ignore_permissions=True)
		frappe.db.rollback()
		return {"ok": True, "name": cl.name}
	except Exception as e:
		frappe.db.rollback()
		return {"ok": False, "error": str(e)[:500]}
