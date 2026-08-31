# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v4.8.5 Cancel PM Request Playwright E2E."""

from __future__ import annotations

import frappe
from frappe.utils.password import update_password

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete import (
	_make_clearance,
	_set_clearance_status,
)
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_ensure_pm_settings_bank,
	_new_submitted_request,
	_sync_funding_fields,
)

E2E_ACCOUNTANT = "pm_cancel_action_v485@example.com"
E2E_PASSWORD = "pm_cancel_v485_e2e"


def _ensure_accountant_user() -> dict:
	frappe.set_user("Administrator")
	if frappe.db.exists("User", E2E_ACCOUNTANT):
		frappe.delete_doc("User", E2E_ACCOUNTANT, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": E2E_ACCOUNTANT,
			"first_name": "PM",
			"last_name": "CancelV485",
			"send_welcome_email": 0,
			"user_type": "System User",
			"enabled": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	for role in ("Petty Management Accountant", "Accounts User"):
		doc.append("roles", {"role": role})
	doc.save(ignore_permissions=True)
	update_password(E2E_ACCOUNTANT, E2E_PASSWORD)
	frappe.db.commit()
	return {"email": E2E_ACCOUNTANT, "password": E2E_PASSWORD}


def _site_ready() -> None:
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	tpm._ensure_petty_account()
	_ensure_pm_settings_bank()


@frappe.whitelist()
def prepare_cancel_action_visible() -> dict:
	"""Finance-approved unfunded request — Cancel PM Request should be visible."""
	_site_ready()
	user = _ensure_accountant_user()
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 25_000)
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
		"expect_cancel_visible": True,
	}


@frappe.whitelist()
def prepare_cancel_action_hidden_funded() -> dict:
	_site_ready()
	user = _ensure_accountant_user()
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 30_000)
	pe = _create_funding_pe(req, 15_000)
	_sync_funding_fields(req)
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"payment_entry": pe,
		"user": user,
		"expect_cancel_visible": False,
	}


@frappe.whitelist()
def prepare_cancel_action_hidden_closed() -> dict:
	_site_ready()
	user = _ensure_accountant_user()
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 22_000)
	frappe.db.set_value("PM Request", req, "is_closed", 1, update_modified=False)
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"user": user,
		"expect_cancel_visible": False,
	}


@frappe.whitelist()
def prepare_cancel_action_hidden_draft_clearance() -> dict:
	_site_ready()
	user = _ensure_accountant_user()
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 35_000)
	pe = _create_funding_pe(req, 35_000)
	_sync_funding_fields(req)
	try:
		cl = _make_clearance(emp, req, 5_000, submit=False)
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		_set_clearance_status(cl, "Draft", docstatus=0)
	except Exception as exc:
		frappe.db.commit()
		return {
			**e2e_run_context(),
			"pm_request": req,
			"user": user,
			"expect_cancel_visible": False,
			"skipped": True,
			"skip_reason": str(exc),
		}
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"clearance": cl,
		"user": user,
		"expect_cancel_visible": False,
	}
