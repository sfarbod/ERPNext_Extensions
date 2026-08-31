# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Ensure Asset Request roles and Frappe Workflow (idempotent)."""

from __future__ import annotations

import frappe

from erpnext_extensions.asset_usage_depreciation.constants import (
	ACTION_APPROVE,
	ACTION_REJECT,
	ACTION_SEND_BACK,
	ACTION_SUBMIT,
	ROLE_AR_EXECUTIVE,
	ROLE_AR_MANAGER,
	ROLE_AR_PLANNER,
	ROLE_ASSET_MANAGER,
	WF_ASSET_REQUEST,
	WF_STATE_APPROVED,
	WF_STATE_CANCELLED,
	WF_STATE_DRAFT,
	WF_STATE_PENDING_CEO,
	WF_STATE_PENDING_MANAGER,
	WF_STATE_PENDING_PLANNING,
	WF_STATE_REJECTED,
)

ASSET_REQUEST_ROLES = (
	ROLE_AR_MANAGER,
	ROLE_AR_PLANNER,
	ROLE_AR_EXECUTIVE,
	ROLE_ASSET_MANAGER,
)


def _wf_state(name: str) -> str:
	from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link

	return resolve_workflow_state_link(name) or name


def _ensure_action(name: str) -> str:
	if frappe.db.exists("Workflow Action Master", name):
		return name
	doc = frappe.new_doc("Workflow Action Master")
	doc.workflow_action_name = name
	doc.insert(ignore_permissions=True)
	return doc.name


def ensure_roles() -> None:
	for role in ASSET_REQUEST_ROLES:
		if frappe.db.exists("Role", role):
			continue
		doc = frappe.new_doc("Role")
		doc.role_name = role
		doc.desk_access = 1
		doc.insert(ignore_permissions=True)


def ensure_asset_request_workflow() -> None:
	if not frappe.db.exists("DocType", "Asset Request"):
		return
	ensure_roles()
	for state in (
		WF_STATE_DRAFT,
		WF_STATE_PENDING_MANAGER,
		WF_STATE_PENDING_PLANNING,
		WF_STATE_PENDING_CEO,
		WF_STATE_APPROVED,
		WF_STATE_REJECTED,
		WF_STATE_CANCELLED,
	):
		_wf_state(state)
	for action in (ACTION_SUBMIT, ACTION_APPROVE, ACTION_REJECT, ACTION_SEND_BACK):
		_ensure_action(action)

	if frappe.db.exists("Workflow", WF_ASSET_REQUEST):
		_repair_workflow()
		return

	w = frappe.new_doc("Workflow")
	w.workflow_name = WF_ASSET_REQUEST
	w.document_type = "Asset Request"
	w.is_active = 1
	w.workflow_state_field = "workflow_state"
	w.override_status = 0
	w.send_email_alert = 0

	for state, doc_status in (
		(WF_STATE_DRAFT, "0"),
		(WF_STATE_PENDING_MANAGER, "0"),
		(WF_STATE_PENDING_PLANNING, "0"),
		(WF_STATE_PENDING_CEO, "0"),
		(WF_STATE_APPROVED, "1"),
		(WF_STATE_REJECTED, "0"),
		(WF_STATE_CANCELLED, "2"),
	):
		w.append(
			"states",
			{"state": _wf_state(state), "doc_status": doc_status, "allow_edit": "All"},
		)

	transitions = (
		(WF_STATE_DRAFT, ACTION_SUBMIT, WF_STATE_PENDING_MANAGER, "Employee", None, 1),
		(WF_STATE_DRAFT, ACTION_SUBMIT, WF_STATE_PENDING_MANAGER, ROLE_ASSET_MANAGER, None, 0),
		(WF_STATE_DRAFT, ACTION_SUBMIT, WF_STATE_PENDING_MANAGER, "System Manager", None, 1),
		(
			WF_STATE_PENDING_MANAGER,
			ACTION_APPROVE,
			WF_STATE_PENDING_PLANNING,
			ROLE_AR_MANAGER,
			"doc.require_planning_approval",
			0,
		),
		(
			WF_STATE_PENDING_MANAGER,
			ACTION_APPROVE,
			WF_STATE_PENDING_CEO,
			ROLE_AR_MANAGER,
			"not doc.require_planning_approval and doc.require_ceo_approval",
			0,
		),
		(WF_STATE_PENDING_MANAGER, ACTION_APPROVE, WF_STATE_APPROVED, ROLE_AR_MANAGER, "not doc.require_planning_approval and not doc.require_ceo_approval", 0),
		(WF_STATE_PENDING_MANAGER, ACTION_APPROVE, WF_STATE_APPROVED, "System Manager", "not doc.require_planning_approval and not doc.require_ceo_approval", 1),
		(WF_STATE_PENDING_MANAGER, ACTION_REJECT, WF_STATE_REJECTED, ROLE_AR_MANAGER, None, 0),
		(WF_STATE_PENDING_MANAGER, ACTION_SEND_BACK, WF_STATE_DRAFT, ROLE_AR_MANAGER, None, 0),
		(
			WF_STATE_PENDING_PLANNING,
			ACTION_APPROVE,
			WF_STATE_PENDING_CEO,
			ROLE_AR_PLANNER,
			"doc.require_ceo_approval",
			0,
		),
		(
			WF_STATE_PENDING_PLANNING,
			ACTION_APPROVE,
			WF_STATE_APPROVED,
			ROLE_AR_PLANNER,
			"not doc.require_ceo_approval",
			0,
		),
		(WF_STATE_PENDING_PLANNING, ACTION_REJECT, WF_STATE_REJECTED, ROLE_AR_PLANNER, None, 0),
		(
			WF_STATE_PENDING_PLANNING,
			ACTION_SEND_BACK,
			WF_STATE_PENDING_MANAGER,
			ROLE_AR_PLANNER,
			None,
			0,
		),
		(WF_STATE_PENDING_CEO, ACTION_APPROVE, WF_STATE_APPROVED, ROLE_AR_EXECUTIVE, None, 0),
		(WF_STATE_PENDING_CEO, ACTION_REJECT, WF_STATE_REJECTED, ROLE_AR_EXECUTIVE, None, 0),
		(
			WF_STATE_PENDING_CEO,
			ACTION_SEND_BACK,
			WF_STATE_PENDING_PLANNING,
			ROLE_AR_EXECUTIVE,
			"doc.require_planning_approval",
			0,
		),
		(
			WF_STATE_PENDING_CEO,
			ACTION_SEND_BACK,
			WF_STATE_PENDING_MANAGER,
			ROLE_AR_EXECUTIVE,
			"not doc.require_planning_approval",
			0,
		),
	)
	for state, action, next_state, role, condition, self_ok in transitions:
		row = {
			"state": _wf_state(state),
			"action": action,
			"next_state": _wf_state(next_state),
			"allowed": role,
			"allow_self_approval": self_ok,
		}
		if condition:
			row["condition"] = condition
		w.append("transitions", row)

	w.insert(ignore_permissions=True)


def _repair_workflow() -> None:
	"""Keep an existing workflow pointed at Asset Request; do not rebuild transitions."""
	if not frappe.db.exists("Workflow", WF_ASSET_REQUEST):
		return
	w = frappe.get_doc("Workflow", WF_ASSET_REQUEST)
	if w.document_type != "Asset Request":
		w.document_type = "Asset Request"
		w.save(ignore_permissions=True)
	if not cint_is_active(w):
		w.is_active = 1
		w.save(ignore_permissions=True)
	_enable_employee_self_submit()


def _enable_employee_self_submit() -> None:
	"""Requesters must be able to run Submit for Approval on their own Draft."""
	if not frappe.db.exists("Workflow", WF_ASSET_REQUEST):
		return
	frappe.db.sql(
		"""
		update `tabWorkflow Transition`
		set allow_self_approval=1
		where parent=%s and action=%s and allowed=%s and ifnull(allow_self_approval,0)=0
		""",
		(WF_ASSET_REQUEST, ACTION_SUBMIT, "Employee"),
	)


def cint_is_active(w) -> bool:
	from frappe.utils import cint

	return bool(cint(w.is_active))
