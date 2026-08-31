# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v4.8.6 PM Request finalize Playwright E2E."""

from __future__ import annotations

import frappe
from frappe.utils.password import update_password

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete import _make_clearance
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_ensure_pm_settings_bank,
	_new_submitted_request,
	_sync_funding_fields,
)
from erpnext_extensions.petty_management.services.request_connections_service import (
	build_pm_request_connections_payload,
)
from erpnext_extensions.petty_management.services.request_service import cancel_pm_request

E2E_ACCOUNTANT = "pm_finalize_v486@example.com"
E2E_REQUESTER = "pm_finalize_v486_user@example.com"
E2E_PASSWORD = "pm_finalize_v486_e2e"


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


def _cancelled_clean(amount: float = 18_000) -> tuple[str, str]:
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, amount)
	cancel_pm_request(req)
	frappe.db.commit()
	return req, emp


@frappe.whitelist()
def prepare_cancel_action_accountant_eligible() -> dict:
	_site_ready()
	user = _ensure_user(E2E_ACCOUNTANT, ("Petty Management Accountant", "Accounts User"))
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 17_500)
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
	}


@frappe.whitelist()
def prepare_delete_action_administrator() -> dict:
	_site_ready()
	req, _emp = _cancelled_clean(19_000)
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": {"email": "Administrator"},
	}


@frappe.whitelist()
def prepare_requester_no_cancel_delete() -> dict:
	_site_ready()
	user = _ensure_user(E2E_REQUESTER, ("Petty Management User", "Accounts User"))
	emp = tpm._make_employee()
	if frappe.db.exists("Employee", emp):
		frappe.db.set_value("Employee", emp, "user_id", E2E_REQUESTER, update_modified=False)
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 15_000)
	cancel_req, _ = _cancelled_clean(12_000)
	return {
		**e2e_run_context(),
		"submitted_pm_request": req,
		"cancelled_pm_request": cancel_req,
		"user": user,
	}


@frappe.whitelist()
def prepare_connections_fixture() -> dict:
	_site_ready()
	user = _ensure_user(E2E_ACCOUNTANT, ("Petty Management Accountant", "Accounts User"))
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 25_000)
	pe = _create_funding_pe(req, 10_000)
	_sync_funding_fields(req)
	cl = _make_clearance(emp, req, 4_000, submit=True)
	_sync_funding_fields(req)
	doc = frappe.get_doc("PM Request", req)
	payload = build_pm_request_connections_payload(doc)
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
		"payment_entry": pe,
		"clearance": cl,
		"expected_pe_count": len(payload.get("payment_entries") or []),
		"expected_clearance_count": len(payload.get("clearances") or []),
		"summary_total_paid": payload.get("summary", {}).get("total_paid"),
	}


@frappe.whitelist()
def check_action_flags_as_user(pm_request: str, user: str) -> dict:
	from erpnext_extensions.petty_management.services.request_action_policy import (
		compute_pm_request_action_flags,
	)
	from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
		user_may_execute_pm_request_cancel,
		user_may_execute_pm_request_delete,
	)

	frappe.set_user(user)
	doc = frappe.get_doc("PM Request", pm_request)
	flags = compute_pm_request_action_flags(doc)
	return {
		"may_cancel": bool(user_may_execute_pm_request_cancel(doc)),
		"may_delete": bool(user_may_execute_pm_request_delete(doc)),
		"can_cancel_pm_request": bool(flags.get("can_cancel_pm_request")),
		"can_delete_pm_request": bool(flags.get("can_delete_pm_request")),
	}


@frappe.whitelist()
def execute_delete_pm_request_as_user(pm_request: str, user: str) -> dict:
	from erpnext_extensions.petty_management.services.request_service import delete_pm_request

	frappe.set_user(user)
	delete_pm_request(pm_request)
	frappe.db.commit()
	return {"exists": bool(frappe.db.exists("PM Request", pm_request))}
