# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Sales Invoice references on **receivable** PDC Journal Entry party (AR) lines.

ERPNext updates Sales Invoice outstanding when a Journal Entry account row posts against the
customer receivable account with ``reference_type`` / ``reference_name`` pointing at the invoice.

This mirrors the payable Purchase Invoice slice logic.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import CHEQUE_DIRECTION_RECEIVABLE

_EPS = 1e-6


def receivable_sales_invoice_settlement_slices(doc) -> list[tuple[str, float]] | None:
	"""Return merged (sales_invoice_name, amount) slices for the cheque, or ``None`` for legacy JE lines.

	``None`` means: do not set SI references on party rows (same as historical behaviour).

	Rules:
	- Only **Receivable** cheques are considered.
	- If there are no allocation rows, or none resolve to a Sales Invoice, returns ``None``.
	- Otherwise every non-zero allocation row must be either:

	  - **Sales Invoice** directly, or
	  - **Payment Request** whose ``reference_doctype`` / ``reference_name`` point to a **Sales Invoice**.

	- Amounts must sum to ``doc.cheque_amount`` (company currency precision).
	"""
	if (getattr(doc, "cheque_direction", None) or "").strip() != CHEQUE_DIRECTION_RECEIVABLE:
		return None

	allocations = list(getattr(doc, "allocations", None) or [])
	if not allocations:
		return None

	slices: list[tuple[str, float]] = []
	unresolved: list[str] = []
	for row in allocations:
		amt = flt(getattr(row, "amount", None) or getattr(row, "allocated_amount", None) or 0)
		if amt <= _EPS:
			continue
		rdt = (getattr(row, "reference_doctype", None) or "").strip()
		rnm = (getattr(row, "reference_name", None) or "").strip()
		if not rdt or not rnm:
			frappe.throw(
				_("Receivable PDC allocation row is missing Reference DocType or Reference Name."),
				title=_("PDC Receivable Register"),
			)
		if rdt == "Sales Invoice":
			slices.append((rnm, amt))
			continue
		if rdt == "Payment Request":
			pr = frappe.db.get_value(
				"Payment Request",
				rnm,
				["reference_doctype", "reference_name"],
				as_dict=True,
			)
			if not pr:
				frappe.throw(
					_("Payment Request {0} was not found.").format(rnm),
					title=_("PDC Receivable Register"),
				)
			pr_rdt = (pr.get("reference_doctype") or "").strip()
			pr_rnm = (pr.get("reference_name") or "").strip()
			if pr_rdt == "Sales Invoice" and pr_rnm:
				slices.append((pr_rnm, amt))
				continue
			# SO/empty-based Payment Request: no invoice refs yet.
			unresolved.append(rnm)
			continue
		frappe.throw(
			_(
				"Receivable PDC allocation references {0} — use Sales Invoice or Payment Request linked to a Sales Invoice when allocating against invoices."
			).format(rdt),
			title=_("PDC Receivable Register"),
		)

	if not slices:
		return None
	if unresolved:
		frappe.throw(
			_(
				"Receivable PDC mixes invoice-linked allocations with Payment Request(s) that do not "
				"reference a Sales Invoice ({0}). Use a consistent allocation set."
			).format(", ".join(unresolved)),
			title=_("PDC Receivable Register"),
		)

	merged: dict[str, float] = {}
	for sinv, amt in slices:
		merged[sinv] = merged.get(sinv, 0.0) + flt(amt)

	out = [(s, flt(a)) for s, a in merged.items()]
	total = flt(sum(a for _, a in out))
	chq = flt(getattr(doc, "cheque_amount", None) or 0)
	prec = frappe.get_precision("Post Dated Cheque", "cheque_amount") or 2
	tol = max(_EPS, 0.5 / (10**prec))
	if abs(total - chq) > tol:
		frappe.throw(
			_(
				"Total allocated to Sales Invoices ({0}) must equal Cheque Amount ({1}) when posting invoice-linked settlement."
			).format(total, chq),
			title=_("PDC Receivable Register"),
		)
	return out


__all__ = [
	"receivable_sales_invoice_settlement_slices",
]
