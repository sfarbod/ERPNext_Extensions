# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Read-only settlement visibility for Sales Invoice, Purchase Invoice, and Payment Request.

SI/PI use Payment Ledger outstanding and :mod:`pdc_settlement_capacity` for ``remaining_balance``.
Payment Request uses ``grand_total - PE - PR PDC coverage`` for ledger/document outstanding and remaining
(so the desk never mixes in stale DB ``outstanding_amount`` or incorrectly zeros coverage after Register JE).
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.cheque_management.pdc_payment_request_eligibility import (
	is_payment_request_settlement_eligible,
)
from erpnext_extensions.cheque_management.pdc_settlement_capacity import (
	SETTLEMENT_REFERENCE_DOCTYPES,
	get_invoice_ledger_outstanding,
	get_remaining_settlement_capacity,
	sum_effective_pdc_allocations_to_reference,
	sum_effective_pdc_allocations_via_payment_request_to_invoice,
	sum_payment_entry_allocations_to_payment_request,
	sum_payment_entry_allocations_to_reference,
	sum_payment_request_pdc_coverage_allocations,
)

PDC_ADVANCE_APP_ROW_STATUSES = ("posted", "reversed")


def _sum_net_pdc_advance_applied_on_invoice(
	invoice_doctype: str, invoice_name: str, *, company: str | None = None
) -> float:
	"""Net applied from PDC Advances on this invoice (invoice currency).

	Source-of-truth: the **posted application Journal Entries** (because those are what reduce Payment Ledger).

	Why not rely only on `PDC Invoice Application` rows?
	- Runtime shows JE can post even when child row status isn't persisted (on_submit mutations).
	- The settlement panel must reflect the economic truth that already affects `ledger_outstanding`.
	"""
	dt = (invoice_doctype or "").strip()
	nm = (invoice_name or "").strip()
	if dt not in ("Sales Invoice", "Purchase Invoice") or not nm:
		return 0.0

	co = (company or "").strip()
	if not co:
		try:
			co = (frappe.db.get_value(dt, nm, "company") or "").strip()
		except Exception:
			co = ""

	# Company default advance accounts (same ones used by posting service).
	adv = ""
	if dt == "Purchase Invoice":
		adv = (frappe.db.get_value("Company", co, "default_advance_paid_account") or "").strip() if co else ""
	else:
		adv = (
			(frappe.db.get_value("Company", co, "default_advance_received_account") or "").strip()
			if co
			else ""
		)
	if not adv:
		return 0.0

	apply_remark = f"Apply Advance Post Dated Cheque to {dt} {nm}"
	rev_remark = f"Reverse Advance Post Dated Cheque application on {dt} {nm}"

	# Net from the advance account movement on those JEs:
	# - PI apply: credit advance_paid; reversal: debit advance_paid => net = credit - debit
	# - SI apply: debit advance_received; reversal: credit advance_received => net = debit - credit
	rows = frappe.db.sql(
		"""
		SELECT
		  SUM(COALESCE(a.debit_in_account_currency, 0)) AS dr,
		  SUM(COALESCE(a.credit_in_account_currency, 0)) AS cr
		FROM `tabJournal Entry Account` a
		INNER JOIN `tabJournal Entry` je ON je.name = a.parent
		WHERE je.docstatus = 1
		  AND a.account = %s
		  AND (
			je.user_remark LIKE %s
			OR je.user_remark LIKE %s
		  )
		""",
		(adv, f"{apply_remark}%", f"{rev_remark}%"),
		as_dict=True,
	)
	r = (rows[0] if rows else {}) or {}
	dr = flt(r.get("dr"))
	cr = flt(r.get("cr"))
	return flt(cr - dr) if dt == "Purchase Invoice" else flt(dr - cr)


def _debug_dump_pdc_invoice_application_rows(invoice_name: str) -> list[dict]:
	"""Debug helper: return raw PDC Invoice Application rows for a PI/SI invoice.

	Used via bench execute to inspect runtime DB state.
	"""
	nm = (invoice_name or "").strip()
	if not nm:
		return []
	for dt in ("Purchase Invoice", "Sales Invoice"):
		if frappe.db.exists(dt, nm):
			rows = frappe.get_all(
				"PDC Invoice Application",
				filters={"parenttype": dt, "parent": nm},
				fields=[
					"name",
					"parent",
					"parenttype",
					"parentfield",
					"post_dated_cheque",
					"order_doctype",
					"order_name",
					"amount",
					"amount_in_pdc_currency",
					"application_status",
					"posted_je",
					"reversal_je",
				],
				order_by="idx asc",
			)
			return rows or []
	return []


def get_settlement_summary_for_reference(
	reference_doctype: str | None,
	reference_name: str | None,
) -> dict | None:
	"""Return a normalized settlement summary dict, or ``None`` if not applicable.

	* ``financial_basis_amount`` — ``grand_total`` on the reference (document total).
	* ``payment_entry_amount`` — sum of submitted Payment Entry allocations against this reference.
	* ``effective_pdc_amount`` — sum of effective Post Dated Cheque allocation rows (Step 2 rules).
	* ``document_outstanding`` — for SI/PI: Payment Ledger outstanding; for **Payment Request**:
	  ``grand_total - submitted PE - effective PDC`` (not the raw DB ``outstanding_amount``, which is often 0
	  on draft PRs before ERPNext/sync updates).
	* ``remaining_balance`` — SI/PI: Step 2 capacity (ledger outstanding minus effective PDC). PR:
	  ``grand_total - PE - effective PDC`` (same numbers as ``document_outstanding``).
	"""
	rdt = (reference_doctype or "").strip()
	rnm = (reference_name or "").strip()
	if rdt not in SETTLEMENT_REFERENCE_DOCTYPES or not rnm:
		return None
	if not frappe.db.exists(rdt, rnm):
		return None

	frappe.has_permission(rdt, "read", rnm, throw=True)

	meta = frappe.get_meta(rdt)
	if not meta.has_field("grand_total") or not meta.has_field("outstanding_amount"):
		return None

	fields = ["grand_total", "outstanding_amount", "company", "currency", "docstatus"]
	# Some doctypes (e.g. Sales Invoice) may not have workflow_state column in all installs.
	if meta.has_field("workflow_state"):
		fields.append("workflow_state")

	row = frappe.db.get_value(
		rdt,
		rnm,
		fields,
		as_dict=True,
	)
	if not row:
		return None

	company = (row.get("company") or "").strip()
	currency = (row.get("currency") or "").strip()

	gt = flt(row.get("grand_total"))

	if rdt == "Payment Request":
		# Do **not** use raw ``outstanding_amount`` from DB for display: ERPNext often leaves it 0 for Draft /
		# pre-submit PRs. The desk must show one consistent decomposition everywhere:
		#   unpaid / remaining = grand_total - submitted PE - PR PDC coverage
		# PR PDC coverage uses :func:`sum_payment_request_pdc_coverage_allocations` (Draft + Submitted
		# allocations; Register JE does not clear PR coverage). Invoice paths keep JE-aware effective sums.
		if not is_payment_request_settlement_eligible(row):
			pe_sum = 0.0
			pdc_direct = 0.0
			pdc_via = 0.0
			pdc_total = 0.0
			computed_unpaid = flt(gt - pe_sum - pdc_total)
			return {
				"reference_doctype": rdt,
				"reference_name": rnm,
				"company": company,
				"currency": currency,
				"financial_basis_amount": gt,
				"payment_entry_amount": pe_sum,
				"effective_pdc_amount_direct": pdc_direct,
				"effective_pdc_amount_via_pr": pdc_via,
				"effective_pdc_amount": 0.0,
				"pdc_advance_applied_amount": 0.0,
				"ledger_outstanding": computed_unpaid,
				"document_outstanding": computed_unpaid,
				"remaining_balance": computed_unpaid,
				"payment_request_settlement_eligible": False,
			}

		pe_sum = sum_payment_entry_allocations_to_payment_request(rnm)
		pdc_direct = sum_payment_request_pdc_coverage_allocations(rnm)
		pdc_via = 0.0
		pdc_total = flt(pdc_direct) + flt(pdc_via)
		computed_unpaid = flt(gt - pe_sum - pdc_total)
		return {
			"reference_doctype": rdt,
			"reference_name": rnm,
			"company": company,
			"currency": currency,
			"financial_basis_amount": gt,
			"payment_entry_amount": pe_sum,
			"effective_pdc_amount_direct": pdc_direct,
			"effective_pdc_amount_via_pr": pdc_via,
			"effective_pdc_amount": pdc_total,
			"pdc_advance_applied_amount": 0.0,
			"ledger_outstanding": computed_unpaid,
			"document_outstanding": computed_unpaid,
			"remaining_balance": computed_unpaid,
			"payment_request_settlement_eligible": is_payment_request_settlement_eligible(row),
		}
	else:
		pe_sum = sum_payment_entry_allocations_to_reference(rdt, rnm, company=company)
		pdc_direct = sum_effective_pdc_allocations_to_reference(rdt, rnm)
		pdc_via = sum_effective_pdc_allocations_via_payment_request_to_invoice(rdt, rnm)
		ledger_out = flt(get_invoice_ledger_outstanding(rdt, rnm))
		doc_out = ledger_out

	pdc_total = flt(pdc_direct) + flt(pdc_via)
	remaining = get_remaining_settlement_capacity(rdt, rnm, outstanding_amount=doc_out, exclude_pdc=None)

	out = {
		"reference_doctype": rdt,
		"reference_name": rnm,
		"company": company,
		"currency": currency,
		"financial_basis_amount": gt,
		"payment_entry_amount": pe_sum,
		"effective_pdc_amount_direct": pdc_direct,
		"effective_pdc_amount_via_pr": pdc_via,
		"effective_pdc_amount": pdc_total,
		"pdc_advance_applied_amount": _sum_net_pdc_advance_applied_on_invoice(rdt, rnm, company=company)
		if rdt in ("Sales Invoice", "Purchase Invoice")
		else 0.0,
		"ledger_outstanding": ledger_out,
		"document_outstanding": doc_out,
		"remaining_balance": remaining,
	}
	return out


@frappe.whitelist()
def get_pdc_settlement_summary(reference_doctype: str | None = None, reference_name: str | None = None):
	"""Desk: load settlement summary for the open document (SI / PI / Payment Request)."""
	data = get_settlement_summary_for_reference(reference_doctype, reference_name)
	if data is None:
		return {}
	return data


__all__ = [
	"get_pdc_settlement_summary",
	"get_settlement_summary_for_reference",
]
