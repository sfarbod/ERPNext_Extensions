# Copyright (c) 2026, ERPNext Extensions contributors
"""Fail-fast validation for PM workflow approvers (v5.0.2).

Ensures stamped approvers can read the document and hold workflow transition roles
before a document enters the approval workflow. Does not grant permissions.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from erpnext_extensions.petty_management.services.business_status_service import (
	CLEARANCE_PENDING_WORKFLOW_TITLES,
	REQUEST_PENDING_WORKFLOW_TITLES,
)

APPROVER_FIELD_LABELS: dict[str, str] = {
	"manager_approver": _("Manager Approver"),
	"ceo_approver": _("CEO Approver"),
	"finance_approver": _("Finance Approver"),
}

_REQUEST_APPROVER_FIELDS = ("manager_approver", "ceo_approver", "finance_approver")
_CLEARANCE_APPROVER_FIELDS = ("manager_approver",)

_ACTIONS_REQUIRING_READ_CHECK = frozenset(
	{
		"PM Manager Approve",
		"PM CEO Approve",
		"PM Finance Approve",
		"PM Approve",
		"PM Return for Correction",
	}
)


def _workflow_title_from_link(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def pending_approval_titles_for_doctype(doctype: str) -> frozenset[str]:
	if doctype == "PM Request":
		return REQUEST_PENDING_WORKFLOW_TITLES
	if doctype == "PM Clearance":
		return CLEARANCE_PENDING_WORKFLOW_TITLES
	return frozenset()


def approver_fields_for_doctype(doctype: str) -> tuple[str, ...]:
	if doctype == "PM Request":
		return _REQUEST_APPROVER_FIELDS
	if doctype == "PM Clearance":
		return _CLEARANCE_APPROVER_FIELDS
	return ()


def get_workflow_roles_for_approver_field(doctype: str, approver_field: str) -> list[str]:
	"""Roles required by pending-approval workflow transitions gated on ``approver_field``."""
	workflow_name = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
	if not workflow_name:
		return []

	pending_titles = pending_approval_titles_for_doctype(doctype)
	roles: list[str] = []
	needle = f"doc.{approver_field}"
	for row in frappe.get_all(
		"Workflow Transition",
		filters={"parent": workflow_name},
		fields=["state", "allowed", "condition"],
	):
		state_title = _workflow_title_from_link(row.get("state"))
		if state_title not in pending_titles:
			continue
		condition = (row.get("condition") or "").strip()
		if needle not in condition:
			continue
		role = (row.get("allowed") or "").strip()
		if role and role not in roles:
			roles.append(role)
	return roles


def _user_is_valid(user: str) -> tuple[bool, bool]:
	"""Return (exists, enabled)."""
	if not user or not frappe.db.exists("User", user):
		return False, False
	enabled = frappe.db.get_value("User", user, "enabled")
	return True, cint(enabled) == 1


def _doc_for_permission_check(doctype: str, doc: Document) -> Document:
	"""Build a document object suitable for ``has_permission`` checks."""
	if isinstance(doc, Document) and getattr(doc, "doctype", None) == doctype:
		return doc
	if isinstance(doc, Document):
		payload = doc.as_dict()
	else:
		payload = dict(doc)
	payload["doctype"] = doctype
	return frappe.get_doc(payload)


def user_has_effective_read(doctype: str, doc: Document, user: str) -> bool:
	"""True when Frappe grants document read (role + controller + user permissions)."""
	if user == "Administrator":
		return True
	check_doc = _doc_for_permission_check(doctype, doc)
	return bool(frappe.has_permission(doctype, "read", doc=check_doc, user=user))


def diagnose_workflow_approver(
	doctype: str,
	user: str,
	approver_field: str,
	doc: Document,
) -> dict[str, Any]:
	"""Structured diagnostics for one stamped approver (no side effects)."""
	approver_label = APPROVER_FIELD_LABELS.get(approver_field, approver_field)
	exists, enabled = _user_is_valid(user)
	user_roles = set(frappe.get_roles(user)) if exists else set()
	required_roles = get_workflow_roles_for_approver_field(doctype, approver_field)
	missing_roles = [role for role in required_roles if role not in user_roles]
	can_read = user_has_effective_read(doctype, doc, user) if exists and enabled else False

	valid = exists and enabled and not missing_roles and can_read
	return {
		"doctype": doctype,
		"user": user,
		"approver_field": approver_field,
		"approver_label": approver_label,
		"user_exists": exists,
		"user_enabled": enabled,
		"required_roles": required_roles,
		"missing_roles": missing_roles,
		"can_read": can_read,
		"valid": valid,
	}


def format_workflow_approver_error(diagnostic: dict[str, Any]) -> str:
	"""Human-readable ValidationError body for one failed approver."""
	doctype = diagnostic["doctype"]
	user = diagnostic["user"]
	label = diagnostic["approver_label"]

	if not diagnostic["user_exists"]:
		return _("{0} {1} is not a valid User.").format(label, frappe.bold(user))

	if not diagnostic["user_enabled"]:
		return _("{0} {1} is disabled.").format(label, frappe.bold(user))

	lines = [
		label,
		frappe.bold(user),
		_("has been assigned to this {0}.").format(doctype),
	]

	if diagnostic["missing_roles"]:
		lines.append(_("However this user is missing required workflow role(s):"))
		for role in diagnostic["missing_roles"]:
			lines.append(frappe.bold(role))
	elif not diagnostic["can_read"]:
		lines.append(
			_("However this user cannot read {0} documents.").format(doctype)
		)
		if diagnostic["required_roles"]:
			lines.append(_("Required workflow role(s):"))
			for role in diagnostic["required_roles"]:
				lines.append(frappe.bold(role))

	lines.append(_("Please correct the user's roles before submitting."))
	return "\n".join(lines)


def validate_workflow_approver(
	doctype: str,
	user: str,
	approver_field: str,
	doc: Document,
) -> None:
	"""Raise ValidationError when a single approver cannot execute workflow."""
	diagnostic = diagnose_workflow_approver(doctype, user, approver_field, doc)
	if diagnostic["valid"]:
		return
	frappe.throw(
		format_workflow_approver_error(diagnostic),
		title=_("Workflow approver cannot execute"),
		exc=frappe.ValidationError,
	)


def validate_workflow_approvers(doc: Document, doctype: str | None = None) -> None:
	"""Validate every stamped approver on a PM Request or PM Clearance."""
	doctype = doctype or getattr(doc, "doctype", None)
	fields = approver_fields_for_doctype(doctype or "")
	if not fields:
		return

	failures: list[dict[str, Any]] = []
	for field in fields:
		user = (getattr(doc, field, None) or "").strip()
		if not user:
			continue
		diagnostic = diagnose_workflow_approver(doctype, user, field, doc)
		if not diagnostic["valid"]:
			failures.append(diagnostic)

	if not failures:
		return

	message = "\n\n".join(format_workflow_approver_error(item) for item in failures)
	frappe.throw(message, title=_("Workflow approver cannot execute"), exc=frappe.ValidationError)


def validate_acting_approver_can_read(doc: Document, action: str) -> None:
	"""Fail before workflow mutation when the acting user lost read access after submit."""
	action = (action or "").strip()
	if action not in _ACTIONS_REQUIRING_READ_CHECK:
		return
	doctype = getattr(doc, "doctype", None)
	if doctype not in ("PM Request", "PM Clearance"):
		return
	user = frappe.session.user
	if user == "Administrator":
		return
	if user_has_effective_read(doctype, doc, user):
		return
	frappe.throw(
		_(
			"You no longer have permission to read this {0}. "
			"Your roles or company access may have changed since this document was submitted. "
			"Ask an administrator to restore the required role or company permission."
		).format(doctype),
		title=_("Workflow approver cannot execute"),
		exc=frappe.ValidationError,
	)
