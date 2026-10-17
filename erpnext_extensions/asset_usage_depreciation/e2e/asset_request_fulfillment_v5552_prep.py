# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Prep helpers for v5.5.52 Asset Request fulfillment Playwright."""

from __future__ import annotations

import frappe
from frappe.utils import cint, random_string
from frappe.utils.password import update_password

from erpnext_extensions.asset_usage_depreciation.constants import ROLE_ASSET_MANAGER
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

PASSWORD = "ar_fulfill_v5552_1"
E2E_USER = "ar.fulfill.v5552@example.com"


def _ensure_e2e_user() -> str:
	frappe.set_user("Administrator")
	h.make_user(
		email=E2E_USER,
		roles=["System Manager", ROLE_ASSET_MANAGER, "Asset Manager", "Employee", "Desk User"],
		password=PASSWORD,
	)
	update_password(E2E_USER, PASSWORD)
	frappe.db.commit()
	return E2E_USER


@frappe.whitelist()
def snapshot(name: str) -> dict:
	doc = frappe.get_doc("Asset Request", name)
	return {
		"name": doc.name,
		"docstatus": doc.docstatus,
		"status": doc.status,
		"workflow_state": doc.workflow_state,
		"fulfillment_status": doc.fulfillment_status,
		"delivered_qty": doc.delivered_qty,
		"issued_qty": doc.issued_qty,
		"items": [
			{
				"requested_item_code": r.requested_item_code,
				"qty": r.qty,
				"delivered_qty": r.delivered_qty,
				"remaining_qty": r.remaining_qty,
			}
			for r in (doc.items or [])
		],
		"allocation_count": len(doc.get("allocations") or []),
	}


@frappe.whitelist()
def bootstrap_login() -> dict:
	user = _ensure_e2e_user()
	return {"email": user, "password": PASSWORD}


@frappe.whitelist()
def submit_asset_movement(name: str) -> dict:
	"""Submit a draft Asset Movement (Desk-equivalent server path for flaky UI)."""
	frappe.set_user("Administrator")
	am = frappe.get_doc("Asset Movement", name)
	if cint(am.docstatus) == 0:
		am.submit()
	frappe.db.commit()
	return {"name": am.name, "docstatus": am.docstatus}


@frappe.whitelist()
def prepare_partial_then_full_fixture() -> dict:
	"""Approved AR qty=2 with one pool asset ready; returns names for Desk Issue."""
	user = _ensure_e2e_user()
	skip = h.skip_if_unready()
	if skip:
		frappe.throw(skip)
	h.ensure_settings(
		require_named_manager_approver=0,
		prevent_duplicate_active_requests=0,
	)
	company = h.company()
	employee = h.make_employee(company_name=company)
	tag = random_string(6)
	cat = h.make_isolated_category(tag)
	item = h.make_fixed_asset_item(code=f"AUD-AR-E2E-F52-{tag}", category=cat)
	a1 = h.make_pool_asset(item_code=item, company_name=company)
	a2 = h.make_pool_asset(item_code=item, company_name=company)
	doc = h.make_request(company_name=company, employee=employee, item_code=item, qty=2)
	h.submit_and_approve(doc)
	doc.reload()
	# Issue first asset only via service (Desk will submit AM)
	h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": a1}])
	doc.reload()
	am = doc.allocations[0].asset_movement
	return {
		"asset_request": doc.name,
		"asset_movement": am,
		"second_asset": a2,
		"item_row": doc.items[0].name,
		"email": user,
		"password": PASSWORD,
	}
