# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Shared fixtures for Asset Request QA tests (acquisition only)."""

from __future__ import annotations

import frappe
from frappe.utils import nowdate, random_string

from erpnext_extensions.asset_usage_depreciation.constants import (
	ACTION_APPROVE,
	ACTION_SUBMIT,
	ASSET_REQUEST_SETTINGS_DOCTYPE,
	COMPANY_FIELD_AR_POOL_LOCATION,
	COMPANY_FIELD_AR_REQUIRE_CEO,
	COMPANY_FIELD_AR_REQUIRE_PLANNING,
	ROLE_AR_EXECUTIVE,
	ROLE_AR_MANAGER,
	ROLE_AR_PLANNER,
	ROLE_ASSET_MANAGER,
)


def company() -> str | None:
	if frappe.db.exists("Company", "_Test Company"):
		return "_Test Company"
	row = frappe.get_all("Company", pluck="name", limit=1)
	return row[0] if row else None


def ensure_location(name: str = "Test Location") -> str:
	if not frappe.db.exists("Location", name):
		frappe.get_doc({"doctype": "Location", "location_name": name}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)
	return name


def ensure_asset_category(name: str = "Computers") -> str | None:
	if frappe.db.exists("Asset Category", name):
		return name
	row = frappe.get_all("Asset Category", pluck="name", limit=1)
	return row[0] if row else None


def make_isolated_category(tag: str) -> str:
	"""Clone Computers accounts into a unique category so pool matching stays isolated."""
	name = f"AUD-AR-Cat-{tag}"
	if frappe.db.exists("Asset Category", name):
		return name
	src_name = ensure_asset_category()
	if not src_name:
		frappe.throw("No Asset Category available")
	src = frappe.get_doc("Asset Category", src_name)
	doc = frappe.new_doc("Asset Category")
	doc.asset_category_name = name
	doc.enable_cwip_accounting = src.enable_cwip_accounting
	doc.non_depreciable_category = src.non_depreciable_category
	for acc in src.get("accounts") or []:
		doc.append(
			"accounts",
			{
				"company_name": acc.company_name,
				"fixed_asset_account": acc.fixed_asset_account,
				"accumulated_depreciation_account": acc.accumulated_depreciation_account,
				"depreciation_expense_account": acc.depreciation_expense_account,
				"capital_work_in_progress_account": acc.capital_work_in_progress_account,
			},
		)
	for fb in src.get("finance_books") or []:
		row = {
			"finance_book": fb.finance_book,
			"depreciation_method": fb.depreciation_method,
			"total_number_of_depreciations": fb.total_number_of_depreciations,
			"frequency_of_depreciation": fb.frequency_of_depreciation,
			"depreciation_start_date": fb.depreciation_start_date,
			"rate_of_depreciation": fb.rate_of_depreciation,
			"expected_value_after_useful_life": fb.expected_value_after_useful_life,
		}
		doc.append("finance_books", {k: v for k, v in row.items() if v or v == 0})
	doc.insert(ignore_permissions=True)
	return name


def clear_optional_approvals(company_name: str) -> None:
	if not company_name:
		return
	frappe.db.set_value("Company", company_name, COMPANY_FIELD_AR_REQUIRE_PLANNING, 0)
	frappe.db.set_value("Company", company_name, COMPANY_FIELD_AR_REQUIRE_CEO, 0)


def submit_and_approve(doc):
	"""Drive Draft → Pending Manager → Approved using System Manager transitions."""
	from frappe.model.workflow import apply_workflow

	current = frappe.session.user
	frappe.set_user("Administrator")
	try:
		doc.reload()
		if (doc.workflow_state or "Draft") in ("Draft", "", None):
			apply_workflow(doc, ACTION_SUBMIT)
			doc.reload()
		safety = 0
		while int(doc.docstatus or 0) == 0 and safety < 4:
			apply_workflow(doc, ACTION_APPROVE)
			doc.reload()
			safety += 1
		return doc
	finally:
		frappe.set_user(current)


def ensure_settings(**values) -> None:
	if not frappe.db.exists("DocType", ASSET_REQUEST_SETTINGS_DOCTYPE):
		return
	doc = frappe.get_single(ASSET_REQUEST_SETTINGS_DOCTYPE)
	changed = False
	defaults = {
		"require_named_manager_approver": 0,
		"prevent_duplicate_active_requests": 1,
		"allow_category_substitution": 1,
		"reserve_available_assets": 1,
		"auto_create_asset_movement": 0,
		"auto_submit_asset_movement": 0,
		"auto_create_material_request": 0,
		"auto_submit_material_request": 0,
		"default_movement_purpose": "Issue",
	}
	defaults.update(values)
	for field, val in defaults.items():
		if doc.meta.has_field(field) and doc.get(field) != val:
			doc.set(field, val)
			changed = True
	if changed or doc.is_new():
		doc.save(ignore_permissions=True)
	company_name = company()
	if company_name:
		clear_optional_approvals(company_name)


def make_fixed_asset_item(*, code: str | None = None, category: str | None = None, title: str | None = None) -> str:
	code = code or f"AUD-AR-{random_string(8)}"
	if frappe.db.exists("Item", code):
		return code
	category = category or ensure_asset_category()
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": title or code,
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_fixed_asset": 1,
			"is_grouped_asset": 0,
			"auto_create_assets": 0,
			"asset_category": category,
		}
	)
	item.insert(ignore_permissions=True)
	return item.name


def make_pool_asset(*, item_code: str, company_name: str, asset_name: str | None = None) -> str:
	pool_location = frappe.db.get_value("Company", company_name, COMPANY_FIELD_AR_POOL_LOCATION)
	location = pool_location or ensure_location()
	if location:
		ensure_location(location)
	asset = frappe.get_doc(
		{
			"doctype": "Asset",
			"asset_name": asset_name or f"Pool {item_code} {random_string(4)}",
			"asset_category": frappe.db.get_value("Item", item_code, "asset_category") or ensure_asset_category(),
			"item_code": item_code,
			"company": company_name,
			"purchase_date": "2026-01-01",
			"available_for_use_date": "2026-01-01",
			"calculate_depreciation": 0,
			"net_purchase_amount": 1000,
			"purchase_amount": 1000,
			"location": location,
			"cost_center": company_cost_center(company_name),
			"asset_owner": "Company",
			"asset_type": "Existing Asset",
			"asset_quantity": 1,
		}
	)
	# This site uses prompt autoname for Asset.
	asset.name = asset.asset_name
	asset.flags.name_set = True
	asset.insert(ignore_permissions=True)
	asset.submit()
	frappe.db.set_value("Asset", asset.name, "custodian", "")
	return asset.name


def make_employee(*, company_name: str, user_id: str | None = None, reports_to: str | None = None) -> str:
	filters = {"company": company_name}
	if user_id:
		filters["user_id"] = user_id
	existing = frappe.db.get_value("Employee", filters, "name")
	if existing and not user_id:
		return existing
	if existing:
		if reports_to:
			frappe.db.set_value("Employee", existing, "reports_to", reports_to)
		return existing
	department = frappe.db.get_value("Department", {"company": company_name, "is_group": 0}, "name")
	if not department:
		department = frappe.db.get_value("Department", {"is_group": 0}, "name")
	cost_center = company_cost_center(company_name)
	doc = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": "AR",
			"last_name": random_string(5),
			"company": company_name,
			"status": "Active",
			"gender": "Male",
			"date_of_birth": "1990-01-01",
			"date_of_joining": nowdate(),
			"user_id": user_id,
			"reports_to": reports_to,
			"department": department,
			"payroll_cost_center": cost_center,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def make_user(*, email: str, roles: list[str], password: str = "arqa12345") -> str:
	from frappe.utils.password import update_password
	from frappe.utils import today

	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
		for role in roles:
			if role not in frappe.get_roles(email):
				user.add_roles(role)
		user.flags.ignore_password_policy = True
		user.enabled = 1
		user.last_password_reset_date = today()
		user.save(ignore_permissions=True)
		update_password(email, password)
		frappe.cache.hdel("login_failed_count", email)
		frappe.cache.hdel("login_failed_time", email)
		return email
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0][:20],
			"send_welcome_email": 0,
			"new_password": password,
			"last_password_reset_date": today(),
		}
	)
	user.flags.ignore_password_policy = True
	frappe.flags.in_import = True
	try:
		user.insert(ignore_permissions=True)
	finally:
		frappe.flags.in_import = False
	user.add_roles(*roles)
	update_password(email, password)
	frappe.cache.hdel("login_failed_count", email)
	frappe.cache.hdel("login_failed_time", email)
	return email


def make_request(
	*,
	company_name: str,
	employee: str,
	item_code: str,
	qty: int = 1,
	fulfilled_item_code: str | None = None,
	substitution_reason: str | None = None,
	purpose: str = "QA Asset Request",
	extra_item: dict | None = None,
	**header,
):
	row = {"requested_item_code": item_code, "qty": qty}
	if fulfilled_item_code:
		row["fulfilled_item_code"] = fulfilled_item_code
		row["fulfilled_purchase_item"] = fulfilled_item_code
	if substitution_reason:
		row["substitution_reason"] = substitution_reason
	if extra_item:
		row.update(extra_item)
	payload = {
		"doctype": "Asset Request",
		"company": company_name,
		"employee": employee,
		"transaction_date": nowdate(),
		"required_date": nowdate(),
		"purpose": purpose,
		"items": [row],
	}
	payload.update(header)
	doc = frappe.get_doc(payload)
	doc.insert(ignore_permissions=True)
	return doc


def company_cost_center(company_name: str) -> str | None:
	return frappe.db.get_value("Cost Center", {"company": company_name, "is_group": 0}, "name")


def other_company(exclude: str) -> str | None:
	rows = frappe.get_all("Company", filters={"name": ["!=", exclude]}, pluck="name", limit=1)
	return rows[0] if rows else None


def ensure_project(company_name: str, name: str | None = None) -> str:
	name = name or f"AR QA Project {company_name}"[:140]
	existing = frappe.db.get_value("Project", {"project_name": name}, "name") or (
		name if frappe.db.exists("Project", name) else None
	)
	if existing:
		frappe.db.set_value("Project", existing, "company", company_name)
		return existing
	doc = frappe.get_doc({"doctype": "Project", "project_name": name, "company": company_name})
	doc.insert(ignore_permissions=True)
	return doc.name


def ensure_test_dimension(label: str = "AR QA Region") -> dict:
	"""Create a custom DocType + Accounting Dimension. Idempotent. Generic — not Branch."""
	from frappe.utils import cint

	dt = label
	if not frappe.db.exists("DocType", dt):
		doc = frappe.get_doc(
			{
				"doctype": "DocType",
				"name": dt,
				"module": "Assets",
				"custom": 1,
				"autoname": "field:region_name",
				"fields": [
					{"fieldname": "region_name", "label": "Name", "fieldtype": "Data", "reqd": 1},
					{"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company"},
					{"fieldname": "disabled", "label": "Disabled", "fieldtype": "Check"},
				],
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
				],
			}
		)
		doc.insert(ignore_permissions=True)

	if not frappe.db.exists("Accounting Dimension", {"document_type": dt}):
		dim = frappe.get_doc({"doctype": "Accounting Dimension", "document_type": dt, "label": label})
		dim.insert(ignore_permissions=True)
		# Desk on_update enqueues outside tests; force native field creation on AR doctypes.
		from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
			make_dimension_in_accounting_doctypes,
		)

		make_dimension_in_accounting_doctypes(dim, doclist=["Asset Request", "Asset Request Item", "Material Request Item", "Purchase Order Item"])
	fieldname = frappe.db.get_value("Accounting Dimension", {"document_type": dt}, "fieldname")
	frappe.clear_cache(doctype="Asset Request")
	frappe.clear_cache(doctype="Asset Request Item")
	frappe.clear_cache(doctype="Material Request Item")
	return {"doctype": dt, "fieldname": fieldname, "label": label}


def make_dimension_value(doctype: str, name: str, company: str | None = None, disabled: int = 0) -> str:
	if frappe.db.exists(doctype, name):
		if company:
			frappe.db.set_value(doctype, name, "company", company)
		if disabled:
			frappe.db.set_value(doctype, name, "disabled", disabled)
		return name
	doc = frappe.get_doc({"doctype": doctype, "region_name": name, "company": company, "disabled": disabled})
	doc.insert(ignore_permissions=True)
	return doc.name


def skip_if_unready():
	if not frappe.db.exists("DocType", "Asset Request"):
		return "Asset Request DocType not migrated"
	if not company():
		return "No Company"
	if not ensure_asset_category():
		return "No Asset Category"
	return None


# Role constants re-exported for tests
ROLES = (ROLE_AR_MANAGER, ROLE_AR_PLANNER, ROLE_AR_EXECUTIVE, ROLE_ASSET_MANAGER)


def issue_from_pool(doc, selections=None, confirm_substitution=1):
	from erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request import (
		get_pool_picker,
		issue_from_pool as _issue,
	)

	if selections is None:
		picker = get_pool_picker(doc.name)
		selections = []
		for line in picker.get("lines") or []:
			remaining = int(line.get("remaining_qty") or 0)
			for candidate in line.get("candidates") or []:
				if remaining <= 0:
					break
				selections.append({"item_row": line["item_row"], "asset": candidate["name"]})
				remaining -= 1
	_issue(doc.name, selections=selections, confirm_substitution=confirm_substitution)
	doc.reload()
	return doc


def ensure_employee_asset_request_perms() -> None:
	"""If Custom DocPerm overlays Asset Request, add missing standard JSON roles.

	Frappe ignores tabDocPerm entirely when any Custom DocPerm row exists for
	the doctype. This helper is additive: it never deletes or reduces existing
	administrator Custom DocPerm rows, never grants Employee submit/cancel,
	and never copies the full DocPerm table.
	"""
	from frappe.permissions import add_permission, update_permission_property
	from frappe.utils import cint

	if not frappe.db.exists("Custom DocPerm", {"parent": "Asset Request"}):
		return

	intended = (
		(
			"Employee",
			0,
			{"read": 1, "write": 1, "create": 1, "delete": 1, "print": 1, "email": 1, "report": 1},
			("submit", "cancel", "amend"),
		),
		(
			"Asset Request Manager",
			0,
			{"read": 1, "write": 1, "submit": 1, "print": 1, "email": 1, "report": 1},
			(),
		),
		(
			"Asset Request Planner",
			0,
			{"read": 1, "write": 1, "submit": 1, "print": 1, "email": 1, "report": 1},
			(),
		),
		(
			"Asset Request Executive",
			0,
			{"read": 1, "write": 1, "submit": 1, "print": 1, "email": 1, "report": 1},
			(),
		),
		(
			"Asset Manager",
			0,
			{
				"read": 1,
				"write": 1,
				"create": 1,
				"submit": 1,
				"cancel": 1,
				"amend": 1,
				"export": 1,
				"print": 1,
				"email": 1,
				"report": 1,
			},
			(),
		),
		(
			"Asset Manager",
			1,
			{"read": 1, "write": 1, "export": 1, "print": 1, "email": 1, "report": 1},
			("create", "submit", "cancel", "amend", "delete"),
		),
	)
	for role, permlevel, flags, denied in intended:
		exists = frappe.db.exists(
			"Custom DocPerm",
			{"parent": "Asset Request", "role": role, "permlevel": permlevel},
		)
		if not exists:
			add_permission("Asset Request", role, permlevel, ptype="read")
		for ptype, value in flags.items():
			current = cint(
				frappe.db.get_value(
					"Custom DocPerm",
					{"parent": "Asset Request", "role": role, "permlevel": permlevel},
					ptype,
				)
			)
			if current != cint(value):
				update_permission_property("Asset Request", role, permlevel, ptype, value, validate=True)
		for ptype in denied:
			if cint(
				frappe.db.get_value(
					"Custom DocPerm",
					{"parent": "Asset Request", "role": role, "permlevel": permlevel},
					ptype,
				)
			):
				update_permission_property("Asset Request", role, permlevel, ptype, 0, validate=True)
	frappe.clear_cache(doctype="Asset Request")


def request_purchase(doc):
	from erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request import (
		request_purchase as _request,
	)

	_request(doc.name)
	doc.reload()
	return doc
