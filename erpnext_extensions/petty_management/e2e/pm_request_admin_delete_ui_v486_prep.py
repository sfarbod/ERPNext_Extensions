# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v4.8.6 Administrator Desk UI delete Playwright E2E."""

from __future__ import annotations

import frappe
from frappe.utils.password import check_password, update_password

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_ensure_pm_settings_bank,
	_new_submitted_request,
)

E2E_ADMIN_PASSWORD = "pm_admin_e2e_v486"


def _site_ready() -> None:
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	tpm._ensure_petty_account()
	_ensure_pm_settings_bank()


@frappe.whitelist()
def ensure_administrator_e2e_password() -> dict:
	"""Ensure Administrator has a known password for Playwright login."""
	frappe.set_user("Administrator")
	try:
		check_password("Administrator", E2E_ADMIN_PASSWORD)
	except Exception:
		update_password("Administrator", E2E_ADMIN_PASSWORD)
		frappe.db.commit()
	return {"email": "Administrator", "password": E2E_ADMIN_PASSWORD}


@frappe.whitelist()
def prepare_admin_ui_delete_submitted_request() -> dict:
	"""Submitted finance-cleared PM Request eligible for Cancel then Delete (UI flow)."""
	_site_ready()
	ensure_administrator_e2e_password()
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 16_500)
	doc = frappe.get_doc("PM Request", req)
	return {
		**e2e_run_context(),
		"pm_request": req,
		"docstatus": doc.docstatus,
		"administrator": {"email": "Administrator", "password": E2E_ADMIN_PASSWORD},
	}


@frappe.whitelist()
def prepare_connections_empty_fixture() -> dict:
	"""Cancelled clean PM Request — Connections tab empty states."""
	_site_ready()
	ensure_administrator_e2e_password()
	emp = tpm._make_employee()
	tpm._make_holder(emp)
	req = _new_submitted_request(emp, 9_000)
	from erpnext_extensions.petty_management.services.request_service import cancel_pm_request

	cancel_pm_request(req)
	frappe.db.commit()
	return {
		**e2e_run_context(),
		"pm_request": req,
		"administrator": {"email": "Administrator", "password": E2E_ADMIN_PASSWORD},
	}
