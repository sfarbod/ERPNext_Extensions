# Copyright (c) 2026, ERPNext Extensions contributors
"""Purchase Invoice readiness for PM Clearance (v4.1.5).

Single source of truth for Draft-vs-Submitted PI policy:

- Prepare (save / submit / manager): allow docstatus 0|1; block cancelled.
- Finance approval / settlement / preview: all referenced PIs must be submitted;
  re-read DB; never silently rewrite allocated_amount.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from erpnext_extensions.petty_management.services.constants import EPSILON, SETTLEMENT_PI

PI_STATUS_DRAFT = "Draft"
PI_STATUS_SUBMITTED = "Submitted"
PI_STATUS_CANCELLED = "Cancelled"


def _pi_status_label(docstatus: int) -> str:
	if cint(docstatus) == 0:
		return PI_STATUS_DRAFT
	if cint(docstatus) == 1:
		return PI_STATUS_SUBMITTED
	if cint(docstatus) == 2:
		return PI_STATUS_CANCELLED
	return str(docstatus)


def _supplier_display(supplier: str | None, supplier_name: str | None = None) -> str:
	if supplier_name:
		return supplier_name
	if not supplier:
		return ""
	return frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier


def _draft_ceiling(pi: dict[str, Any] | Document) -> float:
	"""Approved Draft allocation ceiling = PI grand_total (fallback rounded_total)."""
	if isinstance(pi, dict):
		return flt(pi.get("grand_total") or pi.get("rounded_total") or 0)
	return flt(getattr(pi, "grand_total", None) or getattr(pi, "rounded_total", None) or 0)


def _load_pi_row(pi_name: str) -> dict[str, Any]:
	fields = [
		"name",
		"docstatus",
		"company",
		"supplier",
		"outstanding_amount",
		"grand_total",
		"rounded_total",
		"status",
	]
	if frappe.db.has_column("Purchase Invoice", "supplier_name"):
		fields.append("supplier_name")
	row = frappe.db.get_value("Purchase Invoice", pi_name, fields, as_dict=True)
	if not row:
		frappe.throw(
			_("Purchase Invoice {0} does not exist.").format(pi_name),
			title=_("Invalid Purchase Invoice"),
		)
	return row


def _iter_pi_detail_rows(doc: Document):
	for row in doc.get("details") or []:
		if (row.settlement_type or SETTLEMENT_PI).strip() != SETTLEMENT_PI:
			continue
		if not row.purchase_invoice:
			continue
		yield row


def get_purchase_invoice_readiness(doc: Document | str) -> dict[str, Any]:
	"""Re-read each PI line from DB and classify readiness for Finance / Settle."""
	if isinstance(doc, str):
		doc = frappe.get_doc("PM Clearance", doc)

	lines: list[dict[str, Any]] = []
	blocking_drafts: list[dict[str, Any]] = []
	blocking_cancelled: list[dict[str, Any]] = []
	blocking_other: list[dict[str, Any]] = []

	for row in _iter_pi_detail_rows(doc):
		pi = _load_pi_row(row.purchase_invoice)
		ds = cint(pi.docstatus)
		supplier = pi.supplier
		sname = _supplier_display(supplier, pi.get("supplier_name") if isinstance(pi, dict) else None)
		entry = {
			"row_idx": row.idx,
			"purchase_invoice": pi.name,
			"docstatus": ds,
			"status_label": _pi_status_label(ds),
			"supplier": supplier,
			"supplier_name": sname,
			"grand_total": _draft_ceiling(pi),
			"outstanding_amount": flt(pi.outstanding_amount),
			"allocated_amount_on_clearance": flt(row.allocated_amount),
			"issues": [],
		}
		if ds == 2:
			entry["issues"].append("cancelled")
			blocking_cancelled.append(entry)
		elif ds == 0:
			entry["issues"].append("draft")
			blocking_drafts.append(entry)
		elif ds != 1:
			entry["issues"].append("invalid_docstatus")
			blocking_other.append(entry)
		else:
			# submitted: check drift / over-allocation vs live outstanding
			row_supplier = (getattr(row, "supplier", None) or "").strip()
			if row_supplier and supplier and row_supplier != supplier:
				entry["issues"].append("supplier_drift")
				blocking_other.append(entry)
			if pi.company and doc.company and pi.company != doc.company:
				entry["issues"].append("company_mismatch")
				blocking_other.append(entry)
			if flt(row.allocated_amount) > flt(pi.outstanding_amount) + EPSILON:
				entry["issues"].append("over_allocated")
				blocking_other.append(entry)
		lines.append(entry)

	ready = all(not (e.get("issues") or []) for e in lines)

	message = _format_finance_block_message(blocking_drafts, blocking_cancelled, blocking_other)
	return {
		"ready": ready,
		"lines": lines,
		"blocking_drafts": blocking_drafts,
		"blocking_cancelled": blocking_cancelled,
		"blocking_other": blocking_other,
		"message": message,
	}


def _format_finance_block_message(
	drafts: list[dict],
	cancelled: list[dict],
	other: list[dict],
) -> str:
	parts: list[str] = []
	if drafts:
		parts.append(
			_("Cannot complete Finance Approval because the following Purchase Invoices are not submitted:")
		)
		for e in drafts:
			label = e.get("supplier_name") or e.get("supplier") or ""
			parts.append(f"- {e['purchase_invoice']} — {label}".rstrip(" —"))
		parts.append(_("Please submit these Purchase Invoices and retry Finance Approval."))
	if cancelled:
		parts.append(_("Cancelled Purchase Invoice:"))
		for e in cancelled:
			label = e.get("supplier_name") or e.get("supplier") or ""
			parts.append(f"- {e['purchase_invoice']} — {label}".rstrip(" —"))
		parts.append(_("Remove or replace cancelled Purchase Invoices on this Clearance."))
	if other:
		for e in other:
			issues = e.get("issues") or []
			pi = e["purchase_invoice"]
			if "over_allocated" in issues:
				parts.append(
					_(
						"Cannot complete Finance Approval: allocated amount on {0} exceeds "
						"Purchase Invoice outstanding ({1})."
					).format(pi, e.get("outstanding_amount"))
				)
			if "supplier_drift" in issues:
				parts.append(
					_(
						"Cannot complete Finance Approval: supplier on Clearance line for {0} "
						"no longer matches the Purchase Invoice supplier."
					).format(pi)
				)
			if "company_mismatch" in issues:
				parts.append(
					_("Cannot complete Finance Approval: Purchase Invoice {0} belongs to another company.").format(
						pi
					)
				)
			if "invalid_docstatus" in issues:
				parts.append(_("Purchase Invoice {0} has an invalid document status.").format(pi))
	return "\n".join(parts)


def _skip_prepare_over_allocation_gate(doc: Document) -> bool:
	"""v5.1.4 — skip ONLY prepare over-allocation (allocated > live outstanding).

	Still stamps live outstanding. Never skips cancelled/company/required-PI checks.
	Never skips Finance Approve readiness.

	Allowed skip paths:
	1. PM Clearance Return for Correction (``pm_return_for_correction`` flag).
	2. Pending remark-only save confirmed by ``only_remark_changed_while_pending``.
	"""
	if getattr(frappe.flags, "pm_return_for_correction", False):
		return True
	# Approve / other workflow actions must still hit the over-allocation gate.
	if getattr(frappe.flags, "in_pm_workflow_apply", False):
		return False
	from erpnext_extensions.petty_management.services.draft_approval_guards import (
		is_pending_approval_workflow,
		only_remark_changed_while_pending,
	)

	return bool(is_pending_approval_workflow(doc) and only_remark_changed_while_pending(doc))


def validate_purchase_invoices_for_prepare(doc: Document) -> None:
	"""Allow Draft + Submitted PIs; block Cancelled. Stamp informational snapshot only."""
	skip_over_alloc = _skip_prepare_over_allocation_gate(doc)
	for row in doc.get("details") or []:
		if (row.settlement_type or SETTLEMENT_PI).strip() != SETTLEMENT_PI:
			continue
		if not row.purchase_invoice:
			frappe.throw(
				_("Row {0}: Purchase Invoice is required for Purchase Invoice settlement.").format(row.idx)
			)
		if row.reference_doctype and row.reference_doctype != "Purchase Invoice":
			frappe.throw(
				_("Line {0}: only Purchase Invoice is supported for this settlement type.").format(row.idx)
			)
		row.reference_doctype = "Purchase Invoice"
		pi = _load_pi_row(row.purchase_invoice)
		ds = cint(pi.docstatus)
		if ds == 2:
			frappe.throw(
				_("Row {0}: Purchase Invoice {1} is cancelled and cannot be settled.").format(
					row.idx, row.purchase_invoice
				),
				title=_("Invalid Purchase Invoice"),
			)
		if ds not in (0, 1):
			frappe.throw(
				_("Row {0}: Purchase Invoice {1} has an invalid document status.").format(
					row.idx, row.purchase_invoice
				),
				title=_("Invalid Purchase Invoice"),
			)
		if pi.company and doc.company and pi.company != doc.company:
			frappe.throw(_("Row {0}: Purchase Invoice belongs to another company.").format(row.idx))

		# Informational snapshot — never rewrite allocated_amount if already set > 0
		row.supplier = pi.supplier
		if ds == 0:
			ceiling = _draft_ceiling(pi)
			row.outstanding_amount = ceiling
			if flt(row.allocated_amount) <= 0:
				row.allocated_amount = ceiling
			if flt(row.allocated_amount) <= 0:
				frappe.throw(_("Row {0}: Allocated Amount must be greater than zero.").format(row.idx))
			if not skip_over_alloc and flt(row.allocated_amount) > ceiling + EPSILON:
				frappe.throw(
					_(
						"Row {0}: allocated amount cannot exceed Draft Purchase Invoice grand total ({1})."
					).format(row.idx, ceiling)
				)
		else:
			outstanding = flt(pi.outstanding_amount)
			# Always stamp live outstanding for UI / subsequent gates.
			row.outstanding_amount = outstanding
			if flt(row.allocated_amount) <= 0:
				row.allocated_amount = outstanding
			if flt(row.allocated_amount) <= 0:
				frappe.throw(_("Row {0}: Allocated Amount must be greater than zero.").format(row.idx))
			# Over-allocation family only (allocated vs live outstanding / zero ceiling).
			if skip_over_alloc:
				continue
			if outstanding <= 0:
				frappe.throw(
					_("Row {0}: Purchase Invoice has no outstanding amount to settle.").format(row.idx)
				)
			if flt(row.allocated_amount) > outstanding + EPSILON:
				frappe.throw(
					_("Row {0}: allocated amount cannot exceed Purchase Invoice outstanding ({1}).").format(
						row.idx, outstanding
					)
				)


def _refresh_snapshot_without_changing_allocated(doc: Document) -> None:
	"""Refresh supplier / outstanding_amount from live PI; never change allocated_amount."""
	for row in _iter_pi_detail_rows(doc):
		pi = _load_pi_row(row.purchase_invoice)
		row.supplier = pi.supplier
		if cint(pi.docstatus) == 1:
			row.outstanding_amount = flt(pi.outstanding_amount)
		elif cint(pi.docstatus) == 0:
			row.outstanding_amount = _draft_ceiling(pi)


def validate_purchase_invoices_for_finance_approval(doc: Document) -> None:
	"""Hard gate for Finance Approve: all PIs submitted; drift / over-allocation blocked."""
	readiness = get_purchase_invoice_readiness(doc)
	if readiness["ready"]:
		_refresh_snapshot_without_changing_allocated(doc)
		return
	msg = readiness.get("message") or _(
		"Cannot complete Finance Approval because referenced Purchase Invoices are not ready."
	)
	frappe.throw(msg, title=_("Purchase Invoices not ready"))


def validate_purchase_invoices_for_settlement(doc: Document) -> None:
	"""Independent Settle / JE / Preview hard gate (same readiness rules as Finance)."""
	readiness = get_purchase_invoice_readiness(doc)
	if readiness["ready"]:
		_refresh_snapshot_without_changing_allocated(doc)
		return
	# Prefer settlement-oriented title when drafts remain
	if readiness["blocking_drafts"]:
		msg = readiness.get("message") or ""
		frappe.throw(
			msg
			or _(
				"Cannot settle while Purchase Invoices are still Draft. "
				"Submit all Purchase Invoices first."
			),
			title=_("Purchase Invoices not ready"),
		)
	frappe.throw(
		readiness.get("message")
		or _("Cannot settle: Purchase Invoice lines are not ready for accounting."),
		title=_("Purchase Invoices not ready"),
	)


def assert_purchase_invoice_submitted_for_je(pi_name: str) -> None:
	"""Fail closed when building a JE debit line against a non-submitted PI."""
	ds = cint(frappe.db.get_value("Purchase Invoice", pi_name, "docstatus"))
	if ds != 1:
		frappe.throw(
			_(
				"Cannot create settlement Journal Entry for Purchase Invoice {0}: "
				"it must be submitted (docstatus=1)."
			).format(pi_name),
			title=_("Purchase Invoice not submitted"),
		)
