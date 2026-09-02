# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.2 — edit/delete guards while PM docs are Pending* at docstatus 0."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_datetime, getdate, sbool

from erpnext_extensions.petty_management.services.business_status_service import (
	CLEARANCE_PENDING_WORKFLOW_TITLES,
	REQUEST_PENDING_WORKFLOW_TITLES,
)

PM_PENDING_EDITABLE_FIELDS = frozenset({"remark"})
_PENDING_SAVE_IGNORE_FIELDS = frozenset({"modified", "modified_by"})
_CHILD_SYSTEM_FIELDS = frozenset(
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
	}
)
_LAYOUT_FIELDTYPES = frozenset(
	{
		"Section Break",
		"Column Break",
		"Tab Break",
		"HTML",
		"Table",
		"Fold",
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


def _parent_field_is_user_editable(doc: Document, fieldname: str) -> bool:
	if fieldname in _PENDING_SAVE_IGNORE_FIELDS:
		return False
	df = doc.meta.get_field(fieldname)
	if not df or df.fieldtype in _LAYOUT_FIELDTYPES:
		return False
	if cint(getattr(df, "read_only", 0)):
		return False
	if cint(getattr(df, "is_virtual", 0)):
		return False
	return True


def _child_field_is_user_editable(row: Document, fieldname: str) -> bool:
	if fieldname in _CHILD_SYSTEM_FIELDS:
		return False
	df = row.meta.get_field(fieldname)
	if not df:
		return False
	if cint(getattr(df, "read_only", 0)):
		return False
	if cint(getattr(df, "is_virtual", 0)):
		return False
	return True


def _is_semantically_empty(value) -> bool:
	return value is None or value == ""


def normalize_pending_field_value(value, fieldtype: str | None):
	"""Normalize a DocField value for semantic pending-save comparison."""
	if _is_semantically_empty(value):
		return None
	ft = fieldtype or "Data"
	if ft in ("Currency", "Float", "Percent"):
		return flt(value)
	if ft == "Int":
		return cint(value)
	if ft == "Check":
		return cint(sbool(value))
	if ft == "Date":
		return getdate(value)
	if ft == "Datetime":
		return get_datetime(value)
	if ft == "Time":
		from frappe.utils.data import get_timedelta

		return get_timedelta(value)
	if ft in (
		"Data",
		"Text",
		"Small Text",
		"Long Text",
		"Text Editor",
		"Select",
		"Link",
		"Dynamic Link",
		"Password",
	):
		return cstr(value)
	return cstr(value)


def pending_field_values_semantically_equal(before_val, after_val, fieldtype: str | None) -> bool:
	"""True when two DocField values are semantically identical (Desk vs DB types)."""
	return normalize_pending_field_value(before_val, fieldtype) == normalize_pending_field_value(
		after_val, fieldtype
	)


def _changed_parent_fields(doc: Document) -> set[str]:
	before = doc.get_doc_before_save()
	if not before:
		return set()
	changed: set[str] = set()
	for fname in doc.meta.get_valid_columns():
		if not _parent_field_is_user_editable(doc, fname):
			continue
		df = doc.meta.get_field(fname)
		fieldtype = getattr(df, "fieldtype", None) if df else None
		if not pending_field_values_semantically_equal(before.get(fname), doc.get(fname), fieldtype):
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
				if not _child_field_is_user_editable(row, field):
					continue
				child_df = row.meta.get_field(field)
				fieldtype = getattr(child_df, "fieldtype", None) if child_df else None
				if not pending_field_values_semantically_equal(
					prev.get(field), row.get(field), fieldtype
				):
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
