# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep for v5.1.6: cancelled Request after PE cancel+delete; Admin delete + banner."""

from __future__ import annotations

import frappe
from frappe.utils import cint
from frappe.utils.password import update_password

from erpnext_extensions.petty_management.services.request_service import cancel_pm_request
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_sync_funding_fields,
)

PASSWORD = "pm_v516_delete_1"
ACCOUNTANT = "pm_v516_del_acct@example.com"
E2E_ADMIN_PASSWORD = "pm_admin_e2e_v516"


def _ensure_admin_password() -> dict:
	from frappe.utils.password import check_password

	frappe.set_user("Administrator")
	try:
		check_password("Administrator", E2E_ADMIN_PASSWORD)
	except Exception:
		update_password("Administrator", E2E_ADMIN_PASSWORD)
		frappe.db.commit()
	return {"email": "Administrator", "password": E2E_ADMIN_PASSWORD}


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


@frappe.whitelist()
def prepare_v516_delete_after_pe_removed() -> dict:
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	admin = _ensure_admin_password()
	_ensure_user(ACCOUNTANT, ["Petty Management Accountant", "Accounts User", "Employee"])

	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 12_000)
	pe = _create_funding_pe(req, 12_000)
	_sync_funding_fields(req)
	pe_doc = frappe.get_doc("Payment Entry", pe)
	pe_doc.cancel()
	frappe.delete_doc("Payment Entry", pe, force=1, ignore_permissions=True)
	_sync_funding_fields(req)
	cancel_pm_request(req)
	frappe.db.commit()

	doc = frappe.get_doc("PM Request", req)
	return {
		"pm_request": req,
		"deleted_pe": pe,
		"pe_still_exists": bool(frappe.db.exists("Payment Entry", pe)),
		"docstatus": cint(doc.docstatus),
		"status": doc.status,
		"workflow_state": doc.workflow_state,
		"password": PASSWORD,
		"users": {
			"admin": admin,
			"accountant": {"email": ACCOUNTANT, "password": PASSWORD},
		},
	}


@frappe.whitelist()
def get_banner_messages(pm_request: str) -> list[str]:
	from erpnext_extensions.petty_management.services.request_action_policy import (
		build_pm_request_business_status_presentation,
	)

	frappe.set_user("Administrator")
	doc = frappe.get_doc("PM Request", pm_request)
	pres = build_pm_request_business_status_presentation(doc)
	return list(pres.get("ui_messages") or [])


@frappe.whitelist()
def get_action_flags(pm_request: str) -> dict:
	from erpnext_extensions.petty_management.services.request_service import (
		get_pm_request_action_flags_for_doc,
	)

	frappe.set_user("Administrator")
	return get_pm_request_action_flags_for_doc(frappe.get_doc("PM Request", pm_request))


@frappe.whitelist()
def admin_delete(pm_request: str) -> dict:
	from erpnext_extensions.petty_management.services.request_service import delete_pm_request

	frappe.set_user("Administrator")
	delete_pm_request(pm_request)
	frappe.db.commit()
	return {"deleted": not bool(frappe.db.exists("PM Request", pm_request))}


@frappe.whitelist()
def request_exists(pm_request: str) -> bool:
	return bool(frappe.db.exists("PM Request", pm_request))
