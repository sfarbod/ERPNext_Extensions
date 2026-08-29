# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.2 — edit/delete guards while PM docs are Pending* at docstatus 0."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from erpnext_extensions.petty_management.services.business_status_service import (
	CLEARANCE_PENDING_WORKFLOW_TITLES,
	REQUEST_PENDING_WORKFLOW_TITLES,
)

PM_CLEARANCE_PENDING_EDITABLE_FIELDS = frozenset({"remark"})


def _workflow_title(doc: Document) -> str:
	ws = (getattr(doc, "workflow_state", None) or "").strip()
	if not ws:
		return ""
	return (frappe.db.get_value("Workflow State", ws, "workflow_state_name") or ws or "").strip()


def is_pending_approval_workflow(doc: Document) -> bool:
	title = _workflow_title(doc)
	if doc.doctype == "PM Request":
		return title in REQUEST_PENDING_WORKFLOW_TITLES
	if doc.doctype == "PM Clearance":
		return title in CLEARANCE_PENDING_WORKFLOW_TITLES
	return False


def _in_workflow_apply() -> bool:
	return bool(
		getattr(frappe.flags, "in_pm_workflow_apply", False)
		or getattr(frappe.flags, "in_workflow", False)
		or getattr(frappe.flags, "in_patch", False)
	)


def _child_tables_unchanged(doc: Document) -> bool:
	for df in doc.meta.get_table_fields():
		if doc.has_value_changed(df.fieldname):
			return False
	return True


def _only_allowed_pending_field_changes(doc: Document) -> bool:
	"""v4.8.3: PM Clearance ``remark`` may be edited while Pending* at docstatus 0."""
	if doc.doctype != "PM Clearance":
		return False
	if not doc.get_doc_before_save():
		return False
	if not _child_tables_unchanged(doc):
		return False
	changed = {
		fname
		for fname in doc.meta.get_valid_columns()
		if doc.has_value_changed(fname) and fname not in ("modified", "modified_by")
	}
	return bool(changed) and changed <= PM_CLEARANCE_PENDING_EDITABLE_FIELDS


def assert_pm_clearance_remark_locked_after_submit(doc: Document) -> None:
	"""Block remark edits after Finance Approve (docstatus 1)."""
	if doc.doctype != "PM Clearance" or cint(getattr(doc, "docstatus", 0)) != 1:
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	if (getattr(doc, "remark", None) or "") != (before.get("remark") or ""):
		frappe.throw(
			_("Remarks cannot be changed after Finance Approve."),
			title=_("Read only"),
		)


def assert_pending_not_editable(doc: Document) -> None:
	"""Block edits while Pending* and docstatus=0 unless inside workflow apply.

	Ordinary Desk saves (requester / casual edits) are blocked. Workflow apply
	and privileged ``ignore_permissions`` saves (ops helpers / stamped field
	refresh) are allowed.
	"""
	if cint(getattr(doc, "docstatus", 0)) != 0:
		return
	if not is_pending_approval_workflow(doc):
		return
	if _only_allowed_pending_field_changes(doc):
		return
	if _in_workflow_apply():
		return
	if cint(getattr(doc.flags, "ignore_permissions", 0)):
		return
	if doc.is_new():
		return

	frappe.throw(
		_(
			"Cannot edit {0} while it is pending approval. "
			"An approver must use Return for Correction, or wait until approval completes."
		).format(doc.doctype),
		title=_("Pending approval"),
	)


def assert_pending_not_deletable(doc: Document) -> None:
	"""Block delete/trash while workflow is Pending*."""
	if not is_pending_approval_workflow(doc):
		return
	frappe.throw(
		_(
			"Cannot delete {0} while it is pending approval. "
			"Use Return for Correction first, then delete the Draft if eligible."
		).format(doc.doctype),
		title=_("Pending approval"),
	)
