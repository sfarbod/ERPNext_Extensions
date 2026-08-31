# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v4.8.6 Delete PM Request Playwright E2E."""

from __future__ import annotations

import frappe
from frappe.utils.password import update_password

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_ensure_pm_settings_bank,
	_new_submitted_request,
	_sync_funding_fields,
)
from erpnext_extensions.petty_management.services.request_service import cancel_pm_request

E2E_ACCOUNTANT = "pm_delete_action_v486@example.com"
E2E_REQUESTER = "pm_delete_action_v486_user@example.com"
E2E_PASSWORD = "pm_delete_v486_e2e"


def _ensure_user(email: str, roles: tuple[str, ...]) -> dict:
	frappe.set_user("Administrator")
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"user_type": "System User",
			"enabled": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	for role in roles:
		doc.append("roles", {"role": role})
	doc.save(ignore_permissions=True)
	update_password(email, E2E_PASSWORD)
	frappe.db.commit()
	return {"email": email, "password": E2E_PASSWORD}


def _site_ready() -> None:
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	tpm._ensure_petty_account()
	_ensure_pm_settings_bank()


def _cancelled_clean() -> str:
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 18_000)
	cancel_pm_request(req)
	frappe.db.commit()
	return req


@frappe.whitelist()
def prepare_delete_action_accountant_eligible() -> dict:
	_site_ready()
	user = _ensure_user(E2E_ACCOUNTANT, ("Petty Management Accountant", "Accounts User"))
	req = _cancelled_clean()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
		"expect_delete_visible": False,
	}


@frappe.whitelist()
def prepare_delete_action_requester_blocked() -> dict:
	_site_ready()
	user = _ensure_user(E2E_REQUESTER, ("Petty Management User", "Accounts User"))
	emp = tpm._make_employee()
	if frappe.db.exists("Employee", emp):
		frappe.db.set_value("Employee", emp, "user_id", E2E_REQUESTER, update_modified=False)
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 16_000)
	cancel_pm_request(req)
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
		"expect_delete_visible": False,
	}


@frappe.whitelist()
def check_delete_action_flags_as_user(pm_request: str, user: str) -> dict:
	"""Desk flag check for Playwright when form open is not required."""
	from erpnext_extensions.petty_management.services.request_action_policy import (
		compute_pm_request_action_flags,
	)
	from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
		user_may_execute_pm_request_delete,
	)

	frappe.set_user(user)
	doc = frappe.get_doc("PM Request", pm_request)
	flags = compute_pm_request_action_flags(doc)
	return {
		"may_execute": bool(user_may_execute_pm_request_delete(doc)),
		"can_delete_pm_request": bool(flags.get("can_delete_pm_request")),
	}


@frappe.whitelist()
def execute_delete_pm_request_as_user(pm_request: str, user: str) -> dict:
	"""Run business delete as the given user (Playwright / QA)."""
	from erpnext_extensions.petty_management.services.request_service import delete_pm_request

	frappe.set_user(user)
	delete_pm_request(pm_request)
	frappe.db.commit()
	return {"exists": bool(frappe.db.exists("PM Request", pm_request))}


@frappe.whitelist()
def prepare_delete_action_hidden_pe_history() -> dict:
	_site_ready()
	user = _ensure_user(E2E_ACCOUNTANT, ("Petty Management Accountant", "Accounts User"))
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 22_000)
	pe = _create_funding_pe(req, 22_000)
	frappe.get_doc("Payment Entry", pe).cancel()
	_sync_funding_fields(req)
	cancel_pm_request(req)
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
		"expect_delete_visible": False,
	}


@frappe.whitelist()
def prepare_delete_action_administrator() -> dict:
	_site_ready()
	req = _cancelled_clean()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": {"email": "Administrator"},
		"expect_delete_visible": True,
	}
