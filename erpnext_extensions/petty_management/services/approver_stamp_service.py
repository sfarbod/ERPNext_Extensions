# Copyright (c) 2026, ERPNext Extensions contributors
"""Resolve and stamp PM approver User fields.

Custom code ONLY resolves and stamps. Native Assignment Rule creates ToDos.
Do NOT create ToDo / call assign_to / send approval notifications here.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from erpnext_extensions.petty_management.utils import get_pm_settings


def resolve_manager_approver(employee: str | None) -> str | None:
	"""Priority: Employee.expense_approver → Department expense_approvers (first)."""
	if not employee:
		return None
	user = frappe.db.get_value("Employee", employee, "expense_approver")
	if user and frappe.db.exists("User", user):
		return user

	department = frappe.db.get_value("Employee", employee, "department")
	if not department:
		return None

	rows = frappe.get_all(
		"Department Approver",
		filters={"parent": department, "parentfield": "expense_approvers"},
		fields=["approver"],
		order_by="idx asc",
		limit=5,
	)
	for row in rows:
		approver = (row.get("approver") or "").strip()
		if approver and frappe.db.exists("User", approver):
			return approver
	return None


def resolve_ceo_approver(company: str | None = None) -> str | None:
	settings = get_pm_settings()
	user = getattr(settings, "ceo_approver", None) if settings else None
	if user and frappe.db.exists("User", user):
		return user
	return None


def resolve_finance_approver(company: str | None = None, context: str = "request") -> str | None:
	settings = get_pm_settings()
	if not settings:
		return None
	if context == "clearance":
		user = getattr(settings, "finance_supervisor", None) or getattr(
			settings, "finance_manager", None
		)
	else:
		user = getattr(settings, "finance_manager", None) or getattr(
			settings, "finance_supervisor", None
		)
	if user and frappe.db.exists("User", user):
		return user
	return None


def _require_named_manager() -> bool:
	settings = get_pm_settings()
	if not settings or not hasattr(settings, "require_named_manager_approver"):
		return True
	return cint(getattr(settings, "require_named_manager_approver", 1)) == 1


def stamp_pm_request_approvers(doc: Document) -> None:
	"""Stamp manager/ceo/finance User fields. Fail closed if manager missing when required."""
	manager = resolve_manager_approver(getattr(doc, "employee", None))
	if not manager and _require_named_manager():
		frappe.throw(
			_(
				"Cannot submit: Expense Approver is not set for Employee {0}. "
				"Set Employee Expense Approver or Department Expense Approvers."
			).format(doc.employee),
			title=_("Approver required"),
		)
	doc.manager_approver = manager
	doc.ceo_approver = resolve_ceo_approver(getattr(doc, "company", None))
	doc.finance_approver = resolve_finance_approver(getattr(doc, "company", None), context="request")
	if not doc.ceo_approver:
		frappe.throw(
			_("Cannot submit: CEO Approver is not configured in Petty Management Settings."),
			title=_("Approver required"),
		)
	if not doc.finance_approver:
		frappe.throw(
			_("Cannot submit: Finance Manager is not configured in Petty Management Settings."),
			title=_("Approver required"),
		)


def stamp_pm_clearance_approvers(doc: Document) -> None:
	"""Stamp manager on PM Clearance; finance uses role queue (v4.5.3).

	``finance_approver`` is stamped only after a successful Finance Approve/Reject act.
	"""
	from erpnext_extensions.petty_management.services.clearance_finance_review import (
		ensure_clearance_finance_review_role_configured,
	)

	manager = resolve_manager_approver(getattr(doc, "employee", None))
	if not manager and _require_named_manager():
		frappe.throw(
			_(
				"Cannot submit: Expense Approver is not set for Employee {0}. "
				"Set Employee Expense Approver or Department Expense Approvers."
			).format(doc.employee),
			title=_("Approver required"),
		)
	doc.manager_approver = manager
	doc.finance_approver = None
	ensure_clearance_finance_review_role_configured()



def ensure_pm_request_approver_stamps(doc: Document) -> None:
	"""On Finance Approve submit: keep existing stamps; fill only if missing."""
	has_all = bool(
		(getattr(doc, "manager_approver", None) or "").strip()
		and (getattr(doc, "ceo_approver", None) or "").strip()
		and (getattr(doc, "finance_approver", None) or "").strip()
	)
	if has_all:
		return
	# Preserve any already-stamped fields while filling gaps
	prev_manager = (getattr(doc, "manager_approver", None) or "").strip() or None
	prev_ceo = (getattr(doc, "ceo_approver", None) or "").strip() or None
	prev_finance = (getattr(doc, "finance_approver", None) or "").strip() or None
	stamp_pm_request_approvers(doc)
	if prev_manager:
		doc.manager_approver = prev_manager
	if prev_ceo:
		doc.ceo_approver = prev_ceo
	if prev_finance:
		doc.finance_approver = prev_finance


def ensure_pm_clearance_manager_stamp(doc: Document) -> None:
	"""On Clearance Approve submit: require manager stamp; never clear finance_approver."""
	if (getattr(doc, "manager_approver", None) or "").strip():
		from erpnext_extensions.petty_management.services.clearance_finance_review import (
			ensure_clearance_finance_review_role_configured,
		)

		ensure_clearance_finance_review_role_configured()
		return
	# Manager missing — resolve without wiping finance_approver
	manager = resolve_manager_approver(getattr(doc, "employee", None))
	if not manager and _require_named_manager():
		frappe.throw(
			_(
				"Cannot submit: Expense Approver is not set for Employee {0}. "
				"Set Employee Expense Approver or Department Expense Approvers."
			).format(doc.employee),
			title=_("Approver required"),
		)
	doc.manager_approver = manager
	from erpnext_extensions.petty_management.services.clearance_finance_review import (
		ensure_clearance_finance_review_role_configured,
	)

	ensure_clearance_finance_review_role_configured()
