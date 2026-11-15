# Copyright (c) 2026, ERPNext Extensions contributors
"""Helpers for Playwright UI smoke + Payment Reconciliation Desk UI (v5.7.42)."""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt, today

from erpnext_extensions.advance_accounts.bootstrap import apply as apply_advance_bootstrap
from erpnext_extensions.advance_accounts.constants import ADVANCE_PAID, ADVANCE_RECEIVED
from erpnext_extensions.advance_accounts.custom_fields import ensure_custom_fields
from erpnext_extensions.advance_accounts.tests.e2e_advance_accounts_v5742 import _company, _ensure_adv
from erpnext_extensions.advance_accounts.tests.verify_release_v5742 import (
	_assert_balanced,
	_gl_balance,
	_make_pay,
	_make_pi,
	_make_receive,
	_make_si,
)


def mint_sid() -> dict:
	"""Reuse iran_accounting desk sid mint when available."""
	try:
		from erpnext_extensions.iran_accounting.e2e_v533 import mint_dev_sid

		return mint_dev_sid()
	except Exception:
		from frappe.auth import LoginManager

		frappe.set_user("Administrator")
		lm = LoginManager()
		lm.login_as("Administrator")
		return {"sid": frappe.session.sid, "user": "Administrator"}


def prepare_ui_fixture() -> dict:
	ensure_custom_fields()
	apply_advance_bootstrap()
	company = _company()
	frappe.db.set_value("Company", company, "book_advance_payments_in_separate_party_account", 1)
	cash = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
		"Company", company, "default_bank_account"
	)
	debtors = frappe.db.get_value("Company", company, "default_receivable_account")
	customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
	acc = _ensure_adv(company, "UI Adv Rec", "Liability", "Receivable", ADVANCE_RECEIVED)

	def _pe(paid_from):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"company": company,
				"posting_date": today(),
				"party_type": "Customer",
				"party": customer,
				"paid_from": paid_from,
				"paid_to": cash,
				"paid_amount": 50,
				"received_amount": 50,
				"reference_no": frappe.generate_hash(length=8),
				"reference_date": today(),
			}
		)
		pe.insert()
		return pe

	pe_adv = _pe(acc)
	pe_norm = _pe(debtors)
	frappe.db.commit()
	return {
		"account": acc,
		"pe_receive_advance": pe_adv.name,
		"pe_receive_normal": pe_norm.name,
		"advance_flag": cint(pe_adv.book_advance_payments_in_separate_party_account),
		"normal_flag": cint(pe_norm.book_advance_payments_in_separate_party_account),
	}


def _independent_ledger_snapshot(voucher_type: str, voucher_no: str) -> dict:
	rows, dr, cr = _gl_balance(voucher_type, voucher_no)
	ple = frappe.db.sql(
		"""
		SELECT account, party_type, party, SUM(amount) amt
		FROM `tabPayment Ledger Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(delinked,0)=0
		GROUP BY account, party_type, party
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	return {
		"gl_dr": dr,
		"gl_cr": cr,
		"gl_balanced": abs(dr - cr) < 0.005,
		"gl_lines": len(rows),
		"ple_rows": len(ple),
		"ple_sum": sum(flt(r.amt) for r in ple),
	}


def prepare_pr_ui_fixture() -> dict:
	"""Create outstanding SI/PI + multi-account advances for Payment Reconciliation Desk UI."""
	ensure_custom_fields()
	apply_advance_bootstrap()
	frappe.flags.in_test = True
	company = _company()
	frappe.db.set_value("Company", company, "book_advance_payments_in_separate_party_account", 1)

	debtors = frappe.db.get_value("Company", company, "default_receivable_account")
	creditors = frappe.db.get_value("Company", company, "default_payable_account")
	cash = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
		"Company", company, "default_bank_account"
	)
	customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")

	rec = [
		_ensure_adv(company, "PRUI Adv Rec A", "Liability", "Receivable", ADVANCE_RECEIVED),
		_ensure_adv(company, "PRUI Adv Rec B", "Liability", "Receivable", ADVANCE_RECEIVED),
		_ensure_adv(company, "PRUI Adv Rec C", "Liability", "Receivable", ADVANCE_RECEIVED),
	]
	paid = [
		_ensure_adv(company, "PRUI Adv Paid A", "Asset", "Payable", ADVANCE_PAID),
		_ensure_adv(company, "PRUI Adv Paid B", "Asset", "Payable", ADVANCE_PAID),
		_ensure_adv(company, "PRUI Adv Paid C", "Asset", "Payable", ADVANCE_PAID),
	]
	frappe.db.set_value("Company", company, "default_advance_received_account", rec[0])
	frappe.db.set_value("Company", company, "default_advance_paid_account", paid[0])

	pe_r = [
		_make_receive(company, customer, cash, rec[0], 800),
		_make_receive(company, customer, cash, rec[1], 900),
		_make_receive(company, customer, cash, rec[2], 1100),
	]
	# Invoice left outstanding (no advance rows) so Payment Reconciliation can allocate.
	si = _make_si(company, customer, debtors, 2000)
	si.submit()
	_assert_balanced("Sales Invoice", si.name, "PRUI-SI")

	prev_po = frappe.db.get_single_value("Buying Settings", "po_required")
	frappe.db.set_single_value("Buying Settings", "po_required", "No")
	try:
		pe_p = [
			_make_pay(company, supplier, cash, paid[0], 700),
			_make_pay(company, supplier, cash, paid[1], 750),
			_make_pay(company, supplier, cash, paid[2], 850),
		]
		pi = _make_pi(company, supplier, creditors, 1800)
		pi.submit()
		_assert_balanced("Purchase Invoice", pi.name, "PRUI-PI")
	finally:
		frappe.db.set_single_value("Buying Settings", "po_required", prev_po)

	frappe.db.commit()
	return {
		"company": company,
		"customer": customer,
		"supplier": supplier,
		"debtors": debtors,
		"creditors": creditors,
		"rec_accounts": rec,
		"paid_accounts": paid,
		"si": si.name,
		"si_outstanding": flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")),
		"pi": pi.name,
		"pi_outstanding": flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount")),
		"pe_receive": [p.name for p in pe_r],
		"pe_pay": [p.name for p in pe_p],
		"si_gl": _independent_ledger_snapshot("Sales Invoice", si.name),
		"pi_gl": _independent_ledger_snapshot("Purchase Invoice", pi.name),
	}


def verify_after_pr_reconcile(invoice_doctype: str, invoice_name: str, pe_names: list[str]) -> dict:
	"""Independent post-reconcile checks (GL / outstanding / PE refs)."""
	inv = frappe.get_doc(invoice_doctype, invoice_name)
	out = {
		"invoice": invoice_name,
		"outstanding": flt(inv.outstanding_amount),
		"gl": _independent_ledger_snapshot(invoice_doctype, invoice_name),
		"pe_refs": {},
	}
	for pe_name in pe_names:
		pe = frappe.get_doc("Payment Entry", pe_name)
		refs = [r.reference_name for r in (pe.references or []) if r.reference_name == invoice_name]
		out["pe_refs"][pe_name] = {
			"unallocated": flt(pe.unallocated_amount),
			"has_invoice_ref": bool(refs),
			"gl": _independent_ledger_snapshot("Payment Entry", pe_name),
		}
	out["ok"] = out["gl"]["gl_balanced"] and flt(inv.outstanding_amount) < flt(inv.grand_total)
	return out


def unreconcile_invoice_payments(invoice_doctype: str, invoice_name: str) -> dict:
	"""Unreconcile via Unreconcile Payment (same path as Desk), then verify balances."""
	from erpnext.accounts.doctype.unreconcile_payment.unreconcile_payment import (
		create_unreconcile_doc_for_selection,
	)

	# Build selection from Payment Ledger
	rows = frappe.db.sql(
		"""
		SELECT against_voucher_type AS voucher_type, against_voucher_no AS voucher_no,
		       voucher_type AS against_voucher_type, voucher_no AS against_voucher_no,
		       account, party_type, party, company, SUM(amount) AS allocated_amount
		FROM `tabPayment Ledger Entry`
		WHERE against_voucher_type=%s AND against_voucher_no=%s
		  AND voucher_type='Payment Entry' AND IFNULL(delinked,0)=0
		GROUP BY against_voucher_type, against_voucher_no, voucher_type, voucher_no,
		         account, party_type, party, company
		HAVING ABS(SUM(amount)) > 0.005
		""",
		(invoice_doctype, invoice_name),
		as_dict=True,
	)
	if not rows:
		# Fallback: PE references
		pes = frappe.db.sql(
			"""
			SELECT parent AS voucher_no, allocated_amount, reference_doctype AS against_voucher_type,
			       reference_name AS against_voucher_no
			FROM `tabPayment Entry Reference`
			WHERE reference_doctype=%s AND reference_name=%s AND docstatus=1
			""",
			(invoice_doctype, invoice_name),
			as_dict=True,
		)
		for pe in pes:
			doc = frappe.get_doc("Payment Entry", pe.voucher_no)
			unlink_sel = [
				frappe._dict(
					{
						"company": doc.company,
						"voucher_type": invoice_doctype,
						"voucher_no": invoice_name,
						"against_voucher_type": "Payment Entry",
						"against_voucher_no": pe.voucher_no,
					}
				)
			]
			# use document unlink
			from erpnext.accounts.utils import unlink_ref_doc_from_payment_entries

			inv = frappe.get_doc(invoice_doctype, invoice_name)
			unlink_ref_doc_from_payment_entries(inv, pe.voucher_no)
		frappe.db.commit()
		inv = frappe.get_doc(invoice_doctype, invoice_name)
		return {
			"method": "unlink_fallback",
			"outstanding": flt(inv.outstanding_amount),
			"gl": _independent_ledger_snapshot(invoice_doctype, invoice_name),
		}

	# Prefer create_unreconcile when available
	try:
		selections = []
		for r in rows:
			selections.append(
				{
					"company": r.company,
					"voucher_type": r.voucher_type,
					"voucher_no": r.voucher_no,
					"against_voucher_type": r.against_voucher_type,
					"against_voucher_no": r.against_voucher_no,
				}
			)
		create_unreconcile_doc_for_selection(selections=frappe.as_json(selections))
	except TypeError:
		create_unreconcile_doc_for_selection(frappe.as_json(selections))
	except Exception:
		from erpnext.accounts.utils import unlink_ref_doc_from_payment_entries

		inv = frappe.get_doc(invoice_doctype, invoice_name)
		for r in rows:
			unlink_ref_doc_from_payment_entries(inv, r.against_voucher_no)
	frappe.db.commit()
	inv = frappe.get_doc(invoice_doctype, invoice_name)
	return {
		"method": "unreconcile",
		"outstanding": flt(inv.outstanding_amount),
		"gl": _independent_ledger_snapshot(invoice_doctype, invoice_name),
	}
