# Copyright (c) 2026, ERPNext Extensions contributors
"""PM Request Connections tab payload (read-only downstream financial usage)."""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt

from erpnext_extensions.petty_management.services.funding_queries import (
	list_payment_entries_for_pm_request,
)
from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
	_clearance_allocations_for_request,
)


def _docstatus_label(docstatus: int) -> str:
	return {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(cint(docstatus), "")


def list_clearance_connections_for_pm_request(pm_request: str) -> list[dict]:
	"""Clearance allocation rows for Connections tab (any parent status)."""
	out: list[dict] = []
	for row in _clearance_allocations_for_request(pm_request):
		ds = cint(row.clearance_docstatus)
		status = (row.clearance_status or "").strip()
		out.append(
			{
				"clearance": row.clearance,
				"docstatus": _docstatus_label(ds),
				"status": status,
				"workflow_state": frappe.db.get_value(
					"PM Clearance", row.clearance, "workflow_state"
				)
				or "",
				"allocated_amount": flt(row.allocated_amount),
				"settlement_status": status,
			}
		)
	return out


def list_journal_entry_connections_for_pm_request(doc: Document) -> list[dict]:
	"""Request-level and clearance-linked Journal Entries for Connections tab."""
	names: set[str] = set()
	if getattr(doc, "journal_entry", None):
		names.add(doc.journal_entry)
	for row in _clearance_allocations_for_request(doc.name):
		je = frappe.db.get_value("PM Clearance", row.clearance, "journal_entry")
		if je:
			names.add(je)

	out: list[dict] = []
	for name in sorted(names):
		if not frappe.db.exists("Journal Entry", name):
			continue
		je = frappe.db.get_value(
			"Journal Entry",
			name,
			["name", "docstatus", "posting_date", "total_debit", "cheque_no", "user_remark"],
			as_dict=True,
		)
		if not je:
			continue
		reference = (je.cheque_no or je.user_remark or "").strip()
		out.append(
			{
				"journal_entry": je.name,
				"docstatus": _docstatus_label(je.docstatus),
				"posting_date": je.posting_date,
				"amount": flt(je.total_debit),
				"reference": reference,
			}
		)
	return out


def build_pm_request_connections_payload(doc: Document) -> dict:
	"""Authoritative Connections tab payload; funding totals from synced Request fields."""
	if cint(doc.docstatus) == 1:
		from erpnext_extensions.petty_management.services.funding_service import (
			sync_pm_request_funding_fields,
		)

		sync_pm_request_funding_fields(doc.name)
		doc.reload()

	return {
		"pm_request": doc.name,
		"summary": {
			"total_requested": flt(doc.total_requested_amount),
			"total_paid": flt(doc.total_paid_amount),
			"remaining_to_pay": flt(doc.remaining_to_pay),
			"allocated_amount": flt(doc.allocated_amount),
			"available_for_clearance": flt(doc.available_for_clearance),
			"payment_status": (doc.payment_status or "").strip(),
			"status": (doc.status or "").strip(),
		},
		"payment_entries": list_payment_entries_for_pm_request(doc.name),
		"clearances": list_clearance_connections_for_pm_request(doc.name),
		"journal_entries": list_journal_entry_connections_for_pm_request(doc),
	}
