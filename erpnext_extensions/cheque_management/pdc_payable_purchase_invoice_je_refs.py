# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Purchase Invoice references on **payable** PDC Journal Entry party (AP) lines.

ERPNext updates Purchase Invoice outstanding when a Journal Entry account row posts against the
supplier payable account with ``reference_type`` / ``reference_name`` pointing at the invoice.

Used for Draft → Registered (settlement) and matching Registered → Cancelled / Issued → Returned / Cancelled / Replaced /
Returned → Replaced party-side reversals so GL and invoice outstanding stay aligned.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import CHEQUE_DIRECTION_PAYABLE

_EPS = 1e-6


def payable_purchase_invoice_settlement_slices(doc) -> list[tuple[str, float]] | None:
	"""Return merged (purchase_invoice_name, amount) slices for the cheque, or ``None`` for legacy JE lines.

	``None`` means: do not set PI references on party rows (same as historical behaviour).

	Rules:

	* Only **Payable** cheques are considered.
	* If there are no allocation rows, or none resolve to a Purchase Invoice, returns ``None``.
	* Otherwise **every** non-zero allocation row must be either:

	  - **Purchase Invoice** directly, or
	  - **Payment Request** whose ``reference_doctype`` / ``reference_name`` point to a **Purchase Invoice**.

	* Amounts must sum to ``doc.cheque_amount`` (company currency precision).
	"""
	if (getattr(doc, "cheque_direction", None) or "").strip() != CHEQUE_DIRECTION_PAYABLE:
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
				_("Payable PDC allocation row is missing Reference DocType or Reference Name."),
				title=_("PDC Payable Issue"),
			)
		if rdt == "Purchase Invoice":
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
					title=_("PDC Payable Issue"),
				)
			pr_rdt = (pr.get("reference_doctype") or "").strip()
			pr_rnm = (pr.get("reference_name") or "").strip()
			if pr_rdt == "Purchase Invoice" and pr_rnm:
				slices.append((pr_rnm, amt))
				continue
			# PO/SO/empty-based Payment Request: no invoice to mark paid yet.
			# Treat as non-invoice settlement (caller posts JE without PI refs).
			unresolved.append(rnm)
			continue
		frappe.throw(
			_(
				"Payable PDC allocation references {0} — use Purchase Invoice or Payment Request linked to a Purchase Invoice when allocating against invoices."
			).format(rdt),
			title=_("PDC Payable Issue"),
		)

	# Docstring contract: none resolve to PI → legacy JE lines (no invoice refs).
	if not slices:
		return None
	# Mixed invoice + non-invoice rows are unsafe (partial PI outstanding update).
	if unresolved:
		frappe.throw(
			_(
				"Payable PDC mixes invoice-linked allocations with Payment Request(s) that do not "
				"reference a Purchase Invoice ({0}). Use a consistent allocation set."
			).format(", ".join(unresolved)),
			title=_("PDC Payable Issue"),
		)

	merged: dict[str, float] = {}
	for pinv, amt in slices:
		merged[pinv] = merged.get(pinv, 0.0) + flt(amt)

	out = [(p, flt(a)) for p, a in merged.items()]
	total = flt(sum(a for _, a in out))
	chq = flt(getattr(doc, "cheque_amount", None) or 0)
	prec = frappe.get_precision("Post Dated Cheque", "cheque_amount") or 2
	tol = max(_EPS, 0.5 / (10**prec))
	if abs(total - chq) > tol:
		frappe.throw(
			_(
				"Total allocated to Purchase Invoices ({0}) must equal Cheque Amount ({1}) when posting invoice-linked settlement."
			).format(total, chq),
			title=_("PDC Payable Issue"),
		)
	return out


__all__ = [
	"payable_purchase_invoice_settlement_slices",
]
