# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.9 — trace PM Clearance Pending Approval remark save failure."""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.petty_management.services.draft_approval_guards import (
	_changed_parent_fields,
	_child_field_is_user_editable,
	_child_tables_changed,
	_parent_field_is_user_editable,
	is_pending_approval_workflow,
	only_remark_changed_while_pending,
)
from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link


def _all_parent_diffs(doc) -> list[dict]:
	before = doc.get_doc_before_save()
	if not before:
		return []
	out = []
	for fname in doc.meta.get_valid_columns():
		b = before.get(fname)
		a = doc.get(fname)
		if (a or None) != (b or None):
			df = doc.meta.get_field(fname) or {}
			out.append(
				{
					"field": fname,
					"before": b,
					"after": a,
					"read_only": cint(getattr(df, "read_only", 0)),
					"user_editable": _parent_field_is_user_editable(doc, fname),
					"fetch_from": getattr(df, "fetch_from", None),
					"hidden": cint(getattr(df, "hidden", 0)),
					"is_virtual": cint(getattr(df, "is_virtual", 0)),
				}
			)
	return out


def _all_child_diffs(doc) -> list[dict]:
	before = doc.get_doc_before_save()
	if not before:
		return []
	out = []
	for df in doc.meta.get_table_fields():
		fname = df.fieldname
		before_rows = before.get(fname) or []
		current_rows = doc.get(fname) or []
		if len(before_rows) != len(current_rows):
			out.append({"table": fname, "change": "row_count", "before": len(before_rows), "after": len(current_rows)})
			continue
		before_by_name = {row.name: row for row in before_rows}
		for row in current_rows:
			prev = before_by_name.get(row.name)
			if not prev:
				out.append({"table": fname, "row": row.name, "change": "new_row"})
				continue
			for field in row.meta.get_valid_columns():
				b = prev.get(field)
				a = row.get(field)
				if (a or None) != (b or None):
					cdf = row.meta.get_field(field) or {}
					out.append(
						{
							"table": fname,
							"row": row.name,
							"field": field,
							"before": b,
							"after": a,
							"read_only": cint(getattr(cdf, "read_only", 0)),
							"user_editable": _child_field_is_user_editable(row, field),
							"fetch_from": getattr(cdf, "fetch_from", None),
							"hidden": cint(getattr(cdf, "hidden", 0)),
						}
					)
	return out


def _snapshot(doc, label: str) -> dict:
	doc.load_doc_before_save(raise_exception=True)
	return {
		"stage": label,
		"workflow_state": doc.workflow_state,
		"workflow_title": frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
		if doc.workflow_state
		else None,
		"is_pending": is_pending_approval_workflow(doc),
		"only_remark_ok": only_remark_changed_while_pending(doc),
		"parent_editable_changed": sorted(_changed_parent_fields(doc)),
		"child_tables_changed": _child_tables_changed(doc),
		"parent_all_diffs": _all_parent_diffs(doc),
		"child_all_diffs": _all_child_diffs(doc),
	}


@frappe.whitelist()
def trace_pending_approval_remark(pm_clearance: str, force_state: int = 1) -> dict:
	frappe.set_user("Administrator")
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	original_ws = doc.workflow_state
	if cint(force_state):
		frappe.db.set_value(
			"PM Clearance",
			pm_clearance,
			"workflow_state",
			resolve_workflow_state_link("Pending Approval") or "Pending Approval",
			update_modified=False,
		)
		doc.reload()
	stages = []
	stages.append(_snapshot(doc, "noop_before_save"))
	try:
		doc.save()
		stages.append({"stage": "noop_save", "ok": True})
	except Exception as exc:
		stages.append({"stage": "noop_save", "ok": False, "error": str(exc)})
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	doc.remark = (doc.remark or "") + " v509"
	stages.append(_snapshot(doc, "remark_only_before_save"))
	try:
		doc.save()
		stages.append({"stage": "remark_save", "ok": True, "remark": doc.remark})
	except Exception as exc:
		stages.append({"stage": "remark_save", "ok": False, "error": str(exc)})
		stages.append(_snapshot(doc, "remark_only_after_failed_save"))
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	doc.remark = "v509-drift"
	for row in doc.details:
		row.amount_plus_tax = flt(row.allocated_amount)
		if not row.reference:
			row.reference = row.purchase_invoice or ""
	if doc.request_allocations:
		for row in doc.request_allocations:
			row.currency = doc.currency
	stages.append(_snapshot(doc, "desk_drift_sim_before_save"))
	try:
		doc.save()
		stages.append({"stage": "desk_drift_save", "ok": True})
	except Exception as exc:
		stages.append({"stage": "desk_drift_save", "ok": False, "error": str(exc)})
	if cint(force_state) and original_ws != doc.workflow_state:
		frappe.db.set_value("PM Clearance", pm_clearance, "workflow_state", original_ws, update_modified=False)
	frappe.db.commit()
	return {"pm_clearance": pm_clearance, "stages": stages}


@frappe.whitelist()
def simulate_desk_payload_drift(pm_clearance: str) -> dict:
	"""Simulate Desk dropping hidden child stamp fields from the save payload."""
	frappe.set_user("Administrator")
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	results = {}
	for label, mutate in [
		(
			"hidden_reference_doctype_cleared",
			lambda d: setattr(d.details[0], "reference_doctype", None),
		),
		(
			"hidden_reference_set",
			lambda d: setattr(d.details[0], "reference", d.details[0].purchase_invoice),
		),
		(
			"hidden_both_reference_fields",
			lambda d: (
				setattr(d.details[0], "reference", d.details[0].purchase_invoice),
				setattr(d.details[0], "reference_doctype", "Purchase Invoice"),
			),
		),
	]:
		trial = frappe.get_doc("PM Clearance", pm_clearance)
		trial.remark = "desk-payload-test"
		mutate(trial)
		trial.load_doc_before_save(raise_exception=True)
		diag = _snapshot(trial, label)
		try:
			trial.save()
			diag["save_ok"] = True
			diag["save_error"] = None
		except Exception as exc:
			diag["save_ok"] = False
			diag["save_error"] = str(exc)
		results[label] = diag
	return results


@frappe.whitelist()
def find_legacy_pending_approval_clearances() -> list[dict]:
	return frappe.get_all(
		"PM Clearance",
		filters={"workflow_state": "Pending Approval", "docstatus": 0},
		fields=["name", "workflow_state", "status", "remark"],
		limit=20,
	)
