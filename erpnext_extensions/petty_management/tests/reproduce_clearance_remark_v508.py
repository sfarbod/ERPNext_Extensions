# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.8 — diagnose PM Clearance pending remark save rejection."""

from __future__ import annotations

import frappe
from frappe.utils import flt, today

from erpnext_extensions.petty_management.services.draft_approval_guards import (
	_changed_parent_fields,
	_child_tables_changed,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct


def _child_field_diffs(doc) -> dict:
	before = doc.get_doc_before_save()
	if not before:
		return {}
	out: dict = {}
	for df in doc.meta.get_table_fields():
		fname = df.fieldname
		before_rows = before.get(fname) or []
		current_rows = doc.get(fname) or []
		if len(before_rows) != len(current_rows):
			out[fname] = {"row_count": (len(before_rows), len(current_rows))}
			continue
		before_by_name = {row.name: row for row in before_rows}
		for row in current_rows:
			prev = before_by_name.get(row.name)
			if not prev:
				out.setdefault(fname, []).append({"row": row.name, "error": "new row"})
				continue
			for field in row.meta.get_valid_columns():
				if (row.get(field) or None) != (prev.get(field) or None):
					out.setdefault(fname, []).append(
						{
							"row": row.name,
							"field": field,
							"before": prev.get(field),
							"after": row.get(field),
						}
					)
	return out


def _diagnose_doc(doc) -> dict:
	doc.load_doc_before_save(raise_exception=True)
	return {
		"name": doc.name,
		"workflow_state": doc.workflow_state,
		"parent_changed": sorted(_changed_parent_fields(doc)),
		"child_tables_changed": _child_tables_changed(doc),
		"child_diffs": _child_field_diffs(doc),
	}


def _make_pending_clearance():
	pm_ct._ensure_company_context()
	if not pm_ct.COMPANY:
		frappe.throw("No Company")
	pi = pm_ct._make_pi_outstanding(1_000.0)
	if pi.meta.has_field("remarks"):
		pi.remarks = "v508 test"
	pi.insert(ignore_permissions=True)
	emp = pm_ct._make_employee()
	frappe.db.set_value("Employee", emp, "expense_approver", "Administrator", update_modified=False)
	pm_ct._make_holder(emp)
	cl = frappe.new_doc("PM Clearance")
	cl.company = pm_ct.COMPANY
	cl.employee = emp
	cl.transaction_date = today()
	pm_ct._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi.name,
			"allocated_amount": 1_000.0,
		},
	)
	cl.flags.ignore_mandatory = True
	cl.flags.ignore_validate = True
	cl.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Clearance",
		cl.name,
		{"workflow_state": resolve_workflow_state_link("Draft")},
		update_modified=False,
	)
	apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Submit Finance Review")
	return cl.name


@frappe.whitelist()
def diagnose_remark_save(pm_clearance: str | None = None) -> dict:
	frappe.set_user("Administrator")
	name = pm_clearance or _make_pending_clearance()
	doc = frappe.get_doc("PM Clearance", name)
	before_save_diag = _diagnose_doc(doc)
	doc.remark = "v508 remark test"
	try:
		doc.save()
		save_ok = True
		save_error = None
	except Exception as exc:
		save_ok = False
		save_error = str(exc)
	doc.reload()
	after = frappe.get_doc("PM Clearance", name)
	after.remark = "v508 remark test 2"
	after.load_doc_before_save(raise_exception=True)
	remark_diag = _diagnose_doc(after)
	return {
		"pm_clearance": name,
		"noop": before_save_diag,
		"remark_only_before_save": remark_diag,
		"save_ok": save_ok,
		"save_error": save_error,
	}


@frappe.whitelist()
def diagnose_client_child_drift(pm_clearance: str) -> dict:
	"""Simulate Desk sending recalc_totals / holder refresh drift."""
	doc = frappe.get_doc("PM Clearance", pm_clearance)
	doc.remark = "client drift test"
	results = {}
	for label, mutate in [
		("amount_plus_tax_mismatch", lambda d: setattr(d.details[0], "amount_plus_tax", 0)),
		("parent_pending_amount", lambda d: setattr(d, "pending_amount", flt(d.pending_amount) + 1)),
		("parent_total_expense", lambda d: setattr(d, "total_expense_amount", flt(d.total_expense_amount) + 1)),
		("parent_remaining", lambda d: setattr(d, "remaining_amount", flt(d.remaining_amount) + 1)),
		(
			"alloc_available_amount",
			lambda d: setattr(d.request_allocations[0], "available_amount", flt(d.request_allocations[0].available_amount) + 1)
			if d.request_allocations
			else None,
		),
		(
			"alloc_request_amount",
			lambda d: setattr(d.request_allocations[0], "request_amount", flt(d.request_allocations[0].request_amount) + 1)
			if d.request_allocations
			else None,
		),
		("details_outstanding", lambda d: setattr(d.details[0], "outstanding_amount", flt(d.details[0].outstanding_amount) + 1)),
		("details_supplier", lambda d: setattr(d.details[0], "supplier", d.details[0].supplier or "X")),
	]:
		trial = frappe.get_doc("PM Clearance", pm_clearance)
		trial.remark = "client drift test"
		mutate(trial)
		trial.load_doc_before_save(raise_exception=True)
		diag = _diagnose_doc(trial)
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
def diagnose_illegal_clearance_edits(pm_clearance: str) -> dict:
	"""Verify business-field edits remain blocked while Pending."""
	from frappe.utils import flt

	results = {}
	for label, mutate in [
		("details_allocated_amount", lambda d: setattr(d.details[0], "allocated_amount", flt(d.details[0].allocated_amount) + 1)),
		(
			"alloc_allocated_amount",
			lambda d: setattr(d.request_allocations[0], "allocated_amount", flt(d.request_allocations[0].allocated_amount) + 1)
			if d.request_allocations
			else None,
		),
		("parent_employee", lambda d: setattr(d, "employee", d.employee)),
	]:
		trial = frappe.get_doc("PM Clearance", pm_clearance)
		trial.remark = "illegal edit test"
		if label == "parent_employee":
			others = frappe.get_all("Employee", filters={"name": ["!=", trial.employee]}, pluck="name", limit=1)
			if others:
				trial.employee = others[0]
		else:
			mutate(trial)
		trial.load_doc_before_save(raise_exception=True)
		diag = _diagnose_doc(trial)
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
def diagnose_request_client_drift(pm_request: str) -> dict:
	"""Simulate Desk parent drift on PM Request."""
	from frappe.utils import flt

	results = {}
	for label, mutate in [
		("parent_total_requested", lambda d: setattr(d, "total_requested_amount", flt(d.total_requested_amount) + 1)),
		("child_percent_of_total", lambda d: setattr(d.details[0], "percent_of_total", 99)),
	]:
		trial = frappe.get_doc("PM Request", pm_request)
		trial.remark = "request drift test"
		mutate(trial)
		trial.load_doc_before_save(raise_exception=True)
		diag = _diagnose_doc(trial)
		try:
			trial.save()
			diag["save_ok"] = True
			diag["save_error"] = None
		except Exception as exc:
			diag["save_ok"] = False
			diag["save_error"] = str(exc)
		results[label] = diag
	return results
