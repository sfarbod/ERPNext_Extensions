# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Document-specific rights and assignment for the stamped Asset Request manager.

Authorization is the workflow condition ``doc.manager_approver == frappe.session.user``.
DocShare (read/write/submit) lets an Employee-only manager complete Approve → Approved.
Assignment/ToDo is discoverability only — never the permission check.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.asset_usage_depreciation.constants import (
	ASSET_REQUEST_DOCTYPE,
	WF_STATE_PENDING_MANAGER,
)

ASSIGNMENT_MARKER = "AUD-AR-manager-approval"


def is_valid_manager_user(user: str | None) -> bool:
	if not user:
		return False
	if not frappe.db.exists("User", user):
		return False
	return bool(cint(frappe.db.get_value("User", user, "enabled")))


def manager_resolution_message(employee: str | None) -> str:
	if not employee:
		return _("Cannot submit for approval: an Employee is required.")
	reports_to = frappe.db.get_value("Employee", employee, "reports_to")
	if not reports_to:
		return _(
			"Cannot submit for approval: Employee {0} has no Reports To line manager. "
			"Set Reports To to an Employee with an enabled User ID."
		).format(employee)
	if not frappe.db.exists("Employee", reports_to):
		return _("Cannot submit for approval: Reports To {0} is not a valid Employee.").format(
			reports_to
		)
	user_id = frappe.db.get_value("Employee", reports_to, "user_id")
	if not user_id:
		return _(
			"Cannot submit for approval: manager Employee {0} has no User ID."
		).format(reports_to)
	if not frappe.db.exists("User", user_id):
		return _("Cannot submit for approval: manager user {0} does not exist.").format(user_id)
	if not cint(frappe.db.get_value("User", user_id, "enabled")):
		return _("Cannot submit for approval: manager user {0} is disabled.").format(user_id)
	return _(
		"Cannot submit for approval: Employee {0} has no enabled direct line manager user."
	).format(employee)


def sync_manager_access(doc) -> None:
	"""Idempotent share + assignment for the stamped manager. Safe on every save."""
	if getattr(doc, "flags", None) and doc.flags.get("skip_manager_access"):
		return
	if doc.is_new() or not doc.name:
		return

	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	old_manager = before.manager_approver if before else None
	if old_manager and old_manager != doc.manager_approver:
		frappe.share.remove(
			ASSET_REQUEST_DOCTYPE,
			doc.name,
			old_manager,
			flags={"ignore_permissions": True},
		)
		_close_manager_assignments_for(doc, old_manager)

	if doc.manager_approver and is_valid_manager_user(doc.manager_approver):
		_sync_manager_share(doc)
	else:
		_remove_stale_manager_shares(doc, keep_user=None)

	if (doc.workflow_state or "") == WF_STATE_PENDING_MANAGER and doc.manager_approver:
		_sync_manager_assignment(doc)
	else:
		_close_manager_assignments(doc)


def provision_existing_pending_manager_requests() -> dict:
	"""Patch helper: share/assign in-flight Pending Manager docs that already have a stamp.

	Does not set manager_approver, restart workflow, or change workflow_state.
	Unstamped pending requests are left unchanged and counted.
	"""
	if not frappe.db.exists("DocType", ASSET_REQUEST_DOCTYPE):
		return {"provisioned": 0, "unstamped": 0}

	rows = frappe.get_all(
		ASSET_REQUEST_DOCTYPE,
		filters={"workflow_state": WF_STATE_PENDING_MANAGER, "docstatus": ["<", 2]},
		fields=["name", "manager_approver"],
	)
	provisioned = 0
	unstamped = 0
	for row in rows:
		if not row.manager_approver or not is_valid_manager_user(row.manager_approver):
			unstamped += 1
			continue
		doc = frappe.get_doc(ASSET_REQUEST_DOCTYPE, row.name)
		doc.flags.skip_manager_access = False
		_sync_manager_share(doc)
		_sync_manager_assignment(doc)
		provisioned += 1
	if unstamped:
		frappe.logger("erpnext_extensions").warning(
			"v4.8.9: %s Pending Manager Asset Request(s) have no valid manager_approver; left unchanged",
			unstamped,
		)
	return {"provisioned": provisioned, "unstamped": unstamped}


def _sync_manager_share(doc) -> None:
	manager = doc.manager_approver
	_remove_stale_manager_shares(doc, keep_user=manager)
	existing = frappe.db.get_value(
		"DocShare",
		{"share_doctype": ASSET_REQUEST_DOCTYPE, "share_name": doc.name, "user": manager},
		["name", "read", "write", "submit", "share"],
		as_dict=True,
	)
	if existing:
		updates = {}
		if not cint(existing.read):
			updates["read"] = 1
		if not cint(existing.write):
			updates["write"] = 1
		if not cint(existing.submit):
			updates["submit"] = 1
		if updates:
			frappe.db.set_value("DocShare", existing.name, updates, update_modified=False)
		return

	frappe.share.add_docshare(
		ASSET_REQUEST_DOCTYPE,
		doc.name,
		user=manager,
		read=1,
		write=1,
		submit=1,
		share=0,
		notify=0,
		flags={"ignore_share_permission": True, "ignore_permissions": True},
	)


def _remove_stale_manager_shares(doc, keep_user: str | None) -> None:
	"""Drop submit shares for this request that are not the current stamped manager."""
	shares = frappe.get_all(
		"DocShare",
		filters={"share_doctype": ASSET_REQUEST_DOCTYPE, "share_name": doc.name, "submit": 1},
		fields=["name", "user"],
	)
	for share in shares:
		if keep_user and share.user == keep_user:
			continue
		frappe.delete_doc("DocShare", share.name, ignore_permissions=True, force=True)


def _sync_manager_assignment(doc) -> None:
	manager = doc.manager_approver
	open_todos = frappe.get_all(
		"ToDo",
		filters={
			"reference_type": ASSET_REQUEST_DOCTYPE,
			"reference_name": doc.name,
			"status": "Open",
		},
		fields=["name", "allocated_to", "description"],
	)
	has_current = False
	for todo in open_todos:
		if todo.allocated_to == manager:
			has_current = True
			continue
		if ASSIGNMENT_MARKER in (todo.description or ""):
			frappe.db.set_value("ToDo", todo.name, "status", "Closed", update_modified=False)

	if has_current:
		return

	from frappe.desk.form.assign_to import _add

	description = _("{0}: please review Asset Request {1}").format(ASSIGNMENT_MARKER, doc.name)
	_add(
		{
			"assign_to": [manager],
			"doctype": ASSET_REQUEST_DOCTYPE,
			"name": doc.name,
			"description": description,
		},
		ignore_permissions=True,
	)


def _close_manager_assignments(doc) -> None:
	open_todos = frappe.get_all(
		"ToDo",
		filters={
			"reference_type": ASSET_REQUEST_DOCTYPE,
			"reference_name": doc.name,
			"status": "Open",
		},
		fields=["name", "description"],
	)
	for todo in open_todos:
		if ASSIGNMENT_MARKER in (todo.description or ""):
			frappe.db.set_value("ToDo", todo.name, "status", "Closed", update_modified=False)


def _close_manager_assignments_for(doc, user: str) -> None:
	open_todos = frappe.get_all(
		"ToDo",
		filters={
			"reference_type": ASSET_REQUEST_DOCTYPE,
			"reference_name": doc.name,
			"status": "Open",
			"allocated_to": user,
		},
		pluck="name",
	)
	for name in open_todos:
		frappe.db.set_value("ToDo", name, "status", "Closed", update_modified=False)
