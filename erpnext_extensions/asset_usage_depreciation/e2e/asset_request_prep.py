# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Playwright fixtures for Asset Request v4.4.0 (acquisition only)."""

from __future__ import annotations

import frappe
from frappe.utils import random_string

from erpnext_extensions.asset_usage_depreciation.constants import (
	ROLE_AR_MANAGER,
	ROLE_ASSET_MANAGER,
)
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

PASSWORD = frappe.conf.get("e2e_password") or "admin"


def _ensure_request() -> None:
	from werkzeug.test import EnvironBuilder
	from werkzeug.wrappers import Request

	from frappe.auth import CookieManager

	if getattr(frappe.local, "request", None) is None:
		env = EnvironBuilder(path="/", method="GET", environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_environ()
		frappe.local.request = Request(env)
	frappe.local.request_ip = getattr(frappe.local, "request_ip", None) or "127.0.0.1"
	if getattr(frappe.local, "cookie_manager", None) is None:
		frappe.local.cookie_manager = CookieManager()


def _session_sid_for(user: str) -> str:
	"""Issue a desk session using Frappe Session.start (cache + tabSessions)."""
	from frappe.sessions import Session

	_ensure_request()
	full_name = frappe.db.get_value("User", user, "full_name") or user.split("@")[0]
	user_type = frappe.db.get_value("User", user, "user_type") or "System User"
	sess = Session(user=user, resume=False, full_name=full_name, user_type=user_type)
	frappe.db.commit()
	return sess.sid


@frappe.whitelist()
def insert_draft_asset_request(company: str, employee: str, item_code: str, purpose: str = "E2E employee create") -> dict:
	"""DB-first draft insert for Playwright (avoids desk link-permission noise)."""
	frappe.set_user("Administrator")
	doc = h.make_request(
		company_name=company,
		employee=employee,
		item_code=item_code,
		purpose=purpose,
	)
	frappe.db.commit()
	return {"name": doc.name, "docstatus": int(doc.docstatus or 0)}


@frappe.whitelist()
def inspect_asset_request(name: str) -> dict:
	"""DB snapshot including child item codes and linked fulfillment docs."""
	if not name or not frappe.db.exists("Asset Request", name):
		return {"exists": False, "name": name}
	doc = frappe.get_doc("Asset Request", name)
	return {
		"exists": True,
		"name": doc.name,
		"docstatus": int(doc.docstatus or 0),
		"workflow_state": doc.workflow_state,
		"status": doc.status,
		"fulfillment_status": doc.fulfillment_status,
		"material_request": doc.material_request,
		"item_codes": [r.requested_item_code for r in (doc.items or [])],
		"asset_movements": [a.asset_movement for a in (doc.allocations or []) if a.asset_movement],
	}


@frappe.whitelist()
def apply_asset_request_workflow(name: str, action: str) -> dict:
	"""DB-first workflow step for Playwright (serializable result)."""
	from frappe.model.workflow import apply_workflow

	frappe.set_user("Administrator")
	doc = frappe.get_doc("Asset Request", name)
	apply_workflow(doc, action)
	doc.reload()
	frappe.db.commit()
	return {
		"name": doc.name,
		"workflow_state": doc.workflow_state,
		"docstatus": int(doc.docstatus or 0),
		"status": doc.status,
		"material_request": doc.material_request,
	}


@frappe.whitelist()
def prepare_asset_request_e2e() -> dict:
	"""Create isolated users, items, pool asset, and seed documents for UI E2E."""
	frappe.set_user("Administrator")
	if not frappe.db.exists("DocType", "Asset Request"):
		frappe.throw("Asset Request is not migrated")
	from erpnext_extensions.asset_usage_depreciation.workflow import ensure_asset_request_workflow

	ensure_asset_request_workflow()
	company = h.company()
	if not company:
		frappe.throw("No Company")
	h.ensure_settings(
		require_named_manager_approver=0,
		prevent_duplicate_active_requests=0,
		allow_category_substitution=1,
		auto_create_asset_movement=0,
		auto_create_material_request=0,
		auto_submit_asset_movement=0,
		auto_submit_material_request=0,
	)
	tag = random_string(6).lower()
	# Reuse stable desk users so Frappe user-creation throttle does not fire.
	emp_email = "ar.e2e.emp@example.com"
	mgr_email = "ar.e2e.mgr@example.com"
	am_email = "ar.e2e.am@example.com"
	# Desk User keeps Employee on the desk without elevating to System Manager.
	h.make_user(email=emp_email, roles=["Employee", "Desk User"], password=PASSWORD)
	h.ensure_employee_asset_request_perms()
	if not frappe.db.exists("User Permission", {"user": emp_email, "allow": "Company", "for_value": company}):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": emp_email,
				"allow": "Company",
				"for_value": company,
			}
		).insert(ignore_permissions=True)
	h.make_user(email=mgr_email, roles=["Employee", "Desk User", ROLE_AR_MANAGER], password=PASSWORD)
	h.make_user(email=am_email, roles=["Employee", "Desk User", ROLE_ASSET_MANAGER], password=PASSWORD)
	from frappe.utils.password import update_password, delete_login_failed_cache

	for email in (emp_email, mgr_email, am_email):
		frappe.db.set_value("User", email, {"user_type": "System User", "enabled": 1})
		update_password(email, PASSWORD)
		delete_login_failed_cache(email)

	employee = h.make_employee(company_name=company, user_id=emp_email)
	category = h.make_isolated_category(tag)
	samsung = h.make_fixed_asset_item(code=f"AUD-E2E-S-{tag}", title="Samsung Monitor 24 inch", category=category)
	lg = h.make_fixed_asset_item(code=f"AUD-E2E-L-{tag}", title="LG Monitor 24 inch", category=category)
	unique_buy = h.make_fixed_asset_item(code=f"AUD-E2E-B-{tag}", title="E2E Unique Buy Item")
	employee_item = h.make_fixed_asset_item(code=f"AUD-E2E-C-{tag}", title="E2E Employee Create Item")
	pending_item = h.make_fixed_asset_item(code=f"AUD-E2E-P-{tag}", title="E2E Pending Approval Item")
	pool_asset = h.make_pool_asset(item_code=lg, company_name=company, asset_name=f"E2E Pool {tag}")

	pending = h.make_request(
		company_name=company,
		employee=employee,
		item_code=pending_item,
		purpose="E2E pending manager approval",
	)
	pending.manager_approver = mgr_email
	pending.workflow_state = "Pending Manager Approval"
	pending.status = "Pending Manager Approval"
	pending.save(ignore_permissions=True)

	approved_pool = h.make_request(
		company_name=company,
		employee=employee,
		item_code=samsung,
		purpose="E2E fulfillment from pool",
	)
	h.submit_and_approve(approved_pool)
	approved_pool.reload()

	purchase = h.make_request(
		company_name=company,
		employee=employee,
		item_code=unique_buy,
		purpose="E2E purchase path",
	)
	h.ensure_settings(allow_category_substitution=0, prevent_duplicate_active_requests=0)
	h.submit_and_approve(purchase)
	purchase.reload()
	h.ensure_settings(allow_category_substitution=1, prevent_duplicate_active_requests=0)

	frappe.db.commit()
	emp_sid = _session_sid_for(emp_email)
	mgr_sid = _session_sid_for(mgr_email)
	am_sid = _session_sid_for(am_email)
	frappe.set_user("Administrator")
	frappe.db.commit()
	return {
		"company": company,
		"employee": employee,
		"emp_email": emp_email,
		"mgr_email": mgr_email,
		"am_email": am_email,
		"password": PASSWORD,
		"emp_sid": emp_sid,
		"mgr_sid": mgr_sid,
		"am_sid": am_sid,
		"samsung": samsung,
		"lg": lg,
		"unique_buy": unique_buy,
		"employee_item": employee_item,
		"pending_item": pending_item,
		"pool_asset": pool_asset,
		"pending_request": pending.name,
		"approved_request": approved_pool.name,
		"purchase_request": purchase.name,
		"purchase_mr": purchase.material_request,
		"approved_movement": (approved_pool.allocations[0].asset_movement if approved_pool.allocations else None),
	}


@frappe.whitelist()
def migration_snapshot() -> dict:
	"""Counts used to prove migrate/ensure is idempotent."""
	from erpnext_extensions.asset_usage_depreciation.constants import (
		ASSET_REQUEST_DOCTYPE,
		WF_ASSET_REQUEST,
		ROLE_AR_MANAGER,
		ROLE_AR_PLANNER,
		ROLE_AR_EXECUTIVE,
		ROLE_ASSET_MANAGER,
	)

	return {
		"doctypes": {
			dt: bool(frappe.db.exists("DocType", dt))
			for dt in (
				"Asset Request",
				"Asset Request Item",
				"Asset Request Allocation",
				"Asset Request Settings",
			)
		},
		"workflow": frappe.db.exists("Workflow", WF_ASSET_REQUEST),
		"workflow_count": len(frappe.get_all("Workflow", filters={"workflow_name": WF_ASSET_REQUEST})),
		"roles": {
			r: bool(frappe.db.exists("Role", r))
			for r in (ROLE_AR_MANAGER, ROLE_AR_PLANNER, ROLE_AR_EXECUTIVE, ROLE_ASSET_MANAGER)
		},
		"custom_asset_request_fields": len(
			frappe.get_all("Custom Field", filters={"fieldname": "custom_asset_request"})
		),
		"company_ar_fields": {
			f: frappe.db.has_column("Company", f)
			for f in (
				"custom_ar_require_planning_approval",
				"custom_ar_require_ceo_approval",
				"custom_ar_ceo_min_qty",
				"custom_ar_asset_pool_location",
				"custom_ar_default_target_location",
			)
		},
		"asset_request_doctype": ASSET_REQUEST_DOCTYPE,
	}


@frappe.whitelist()
def prepare_asset_request_dimension_e2e() -> dict:
	"""Fixtures for Accounting Dimension E2E: header inherit, item override, MR check, reports."""
	frappe.set_user("Administrator")
	company = h.company()
	if not company:
		frappe.throw("No Company")
	h.ensure_settings(
		require_named_manager_approver=0,
		prevent_duplicate_active_requests=0,
		allow_category_substitution=0,
		auto_create_asset_movement=0,
		auto_create_material_request=0,
		auto_submit_material_request=0,
	)
	employee = h.make_employee(company_name=company)
	dim = h.ensure_test_dimension("AR QA Region")
	fn = dim["fieldname"]
	tehran = h.make_dimension_value(dim["doctype"], "AR-QA-Tehran", company=company)
	shiraz = h.make_dimension_value(dim["doctype"], "AR-QA-Shiraz", company=company)
	cost_center = h.company_cost_center(company)
	tag = random_string(6)
	samsung = h.make_fixed_asset_item(code=f"AUD-E2E-DIM-S-{tag}", title="Samsung Dim E2E")
	lg = h.make_fixed_asset_item(code=f"AUD-E2E-DIM-L-{tag}", title="LG Dim E2E")
	sku = h.make_fixed_asset_item(code=f"AUD-E2E-DIM-SKU-{tag}", title="Shared SKU E2E")
	frappe.db.commit()
	return {
		"company": company,
		"employee": employee,
		"dimension_doctype": dim["doctype"],
		"dimension_fieldname": fn,
		"tehran": tehran,
		"shiraz": shiraz,
		"cost_center": cost_center,
		"samsung": samsung,
		"lg": lg,
		"sku": sku,
	}


@frappe.whitelist()
def reset_e2e_passwords(emails=None, password=None):
	"""Set passwords on the web process DB (may differ from bench execute)."""
	if frappe.session.user != "Administrator":
		frappe.throw("Only Administrator can reset e2e passwords")
	from frappe.utils.password import update_password, delete_login_failed_cache

	emails = emails or []
	password = password or PASSWORD
	if isinstance(emails, str):
		emails = frappe.parse_json(emails)
	for email in emails:
		if email and frappe.db.exists("User", email):
			update_password(email, password)
			delete_login_failed_cache(email)
	frappe.db.commit()
	return {"ok": True, "emails": emails, "site": frappe.local.site}
