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


def _format_clearance_connections(clearance_rows: list[dict]) -> list[dict]:
	out: list[dict] = []
	for row in clearance_rows:
		ds = cint(row.clearance_docstatus)
		status = (row.clearance_status or "").strip()
		out.append(
			{
				"clearance": row.clearance,
				"docstatus": _docstatus_label(ds),
				"status": status,
				"workflow_state": (row.clearance_workflow_state or "").strip(),
				"allocated_amount": flt(row.allocated_amount),
				"settlement_status": status,
			}
		)
	return out


def _journal_entry_names_for_connections(doc: Document, clearance_rows: list[dict]) -> list[str]:
	names: set[str] = set()
	if getattr(doc, "journal_entry", None):
		names.add(doc.journal_entry)
	for row in clearance_rows:
		je = (row.clearance_journal_entry or "").strip()
		if je:
			names.add(je)
	return sorted(names)


def _format_journal_entry_connections(doc: Document, clearance_rows: list[dict]) -> list[dict]:
	names = _journal_entry_names_for_connections(doc, clearance_rows)
	if not names:
		return []

	rows = frappe.db.sql(
		"""
		SELECT
			name,
			docstatus,
			posting_date,
			total_debit,
			cheque_no,
			user_remark
		FROM `tabJournal Entry`
		WHERE name IN %(names)s
		ORDER BY name
		""",
		{"names": names},
		as_dict=True,
	)

	out: list[dict] = []
	for je in rows:
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


def list_clearance_connections_for_pm_request(pm_request: str) -> list[dict]:
	"""Clearance allocation rows for Connections tab (any parent status)."""
	return _format_clearance_connections(_clearance_allocations_for_request(pm_request))


def list_journal_entry_connections_for_pm_request(doc: Document) -> list[dict]:
	"""Request-level and clearance-linked Journal Entries for Connections tab."""
	return _format_journal_entry_connections(doc, _clearance_allocations_for_request(doc.name))


def build_pm_request_connections_payload(doc: Document) -> dict:
	"""Authoritative Connections tab payload; read-only — never syncs or writes."""
	clearance_rows = _clearance_allocations_for_request(doc.name)
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
		"clearances": _format_clearance_connections(clearance_rows),
		"journal_entries": _format_journal_entry_connections(doc, clearance_rows),
	}
