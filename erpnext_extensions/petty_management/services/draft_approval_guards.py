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

PM_PENDING_EDITABLE_FIELDS = frozenset({"remark"})
_PENDING_SAVE_IGNORE_FIELDS = frozenset({"modified", "modified_by"})
# Parent fields recomputed/stamped by Desk or validate — not user business edits.
_PARENT_DERIVED_IGNORE_FIELDS = frozenset(
	{
		"pending_amount",
		"total_available",
		"funded_available",
		"opening_available",
		"current_petty_balance",
		"total_funded_amount",
		"total_cleared_amount",
		"total_expense_without_tax",
		"total_tax_amount",
		"total_expense_amount",
		"total_petty_cash",
		"remaining_amount",
		"je_clearance_date",
		"total_requested_amount",
		"previous_balance",
		"max_balance_for_petty_cash",
	}
)
_CHILD_DERIVED_IGNORE_FIELDS = frozenset(
	{
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"parent",
		"parentfield",
		"parenttype",
		"idx",
		"docstatus",
		"percent_of_total",
		"amount_plus_tax",
		"outstanding_amount",
		"request_amount",
		"paid_amount",
		"previously_allocated_amount",
		"available_amount",
		"currency",
	}
)


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


def _changed_parent_fields(doc: Document) -> set[str]:
	before = doc.get_doc_before_save()
	if not before:
		return set()
	changed: set[str] = set()
	for fname in doc.meta.get_valid_columns():
		if fname in _PENDING_SAVE_IGNORE_FIELDS:
			continue
		if fname in _PARENT_DERIVED_IGNORE_FIELDS:
			continue
		if (doc.get(fname) or None) != (before.get(fname) or None):
			changed.add(fname)
	return changed


def _child_tables_changed(doc: Document) -> bool:
	before = doc.get_doc_before_save()
	if not before:
		return False
	for df in doc.meta.get_table_fields():
		fname = df.fieldname
		before_rows = before.get(fname) or []
		current_rows = doc.get(fname) or []
		if len(before_rows) != len(current_rows):
			return True
		before_by_name = {row.name: row for row in before_rows}
		for row in current_rows:
			prev = before_by_name.get(row.name)
			if not prev:
				return True
			for field in row.meta.get_valid_columns():
				if field in _CHILD_DERIVED_IGNORE_FIELDS:
					continue
				if (row.get(field) or None) != (prev.get(field) or None):
					return True
	return False


def only_remark_changed_while_pending(doc: Document) -> bool:
	"""True when save is allowed while Pending: no-op or remark-only (explicit allow-list)."""
	if doc.doctype not in ("PM Request", "PM Clearance"):
		return False
	if not doc.get_doc_before_save():
		return False
	if _child_tables_changed(doc):
		return False
	changed = _changed_parent_fields(doc)
	if not changed:
		return True
	return changed <= PM_PENDING_EDITABLE_FIELDS


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

	Ordinary Desk saves may update ``remark`` only. Workflow apply and privileged
	``ignore_permissions`` saves (ops helpers / stamped field refresh) are allowed.
	"""
	if cint(getattr(doc, "docstatus", 0)) != 0:
		return
	if not is_pending_approval_workflow(doc):
		return
	if only_remark_changed_while_pending(doc):
		return
	if _in_workflow_apply():
		return
	if cint(getattr(doc.flags, "ignore_permissions", 0)):
		return
	if doc.is_new():
		return

	frappe.throw(
		_("Only Remarks may be edited while approval is pending."),
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
