# Copyright (c) 2026, ERPNext Extensions contributors
"""Real-document E2E for multi advance accounts. Run via:
bench --site development.localhost execute erpnext_extensions.advance_accounts.tests.e2e_advance_accounts_v5742.run
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt, today

from erpnext.controllers.accounts_controller import get_advance_payment_entries

from erpnext_extensions.advance_accounts.bootstrap import apply as apply_advance_bootstrap
from erpnext_extensions.advance_accounts.constants import ADVANCE_PAID, ADVANCE_RECEIVED, CF_ADVANCE_ACCOUNT_TYPE
from erpnext_extensions.advance_accounts.custom_fields import ensure_custom_fields


def _company() -> str:
	row = frappe.db.sql(
		"""
		SELECT fyc.company FROM `tabFiscal Year Company` fyc
		INNER JOIN `tabFiscal Year` fy ON fy.name = fyc.parent
		WHERE IFNULL(fy.disabled,0)=0 ORDER BY fy.year_start_date DESC LIMIT 1
		"""
	)
	return row[0][0] if row else frappe.db.get_value("Company", {}, "name")


def _ensure_adv(company, name, root_type, account_type, adv_type):
	existing = frappe.db.exists("Account", {"account_name": name, "company": company})
	if existing:
		frappe.db.set_value("Account", existing, CF_ADVANCE_ACCOUNT_TYPE, adv_type)
		frappe.db.set_value("Account", existing, "account_type", account_type)
		return existing
	parent = frappe.db.get_value("Account", {"company": company, "root_type": root_type, "is_group": 1}, "name")
	currency = frappe.get_cached_value("Company", company, "default_currency")
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": name,
			"company": company,
			"parent_account": parent,
			"is_group": 0,
			"account_type": account_type,
			"account_currency": currency,
		}
	)
	doc.set(CF_ADVANCE_ACCOUNT_TYPE, adv_type)
	doc.insert(ignore_permissions=True)
	return doc.name


def _gl_sum(voucher_type, voucher_no, account):
	rows = frappe.db.sql(
		"""
		SELECT SUM(debit) dr, SUM(credit) cr FROM `tabGL Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND account=%s AND IFNULL(is_cancelled,0)=0
		""",
		(voucher_type, voucher_no, account),
	)
	return flt(rows[0][0]), flt(rows[0][1])


def run():
	ensure_custom_fields()
	apply_advance_bootstrap()
	company = _company()
	frappe.db.set_value("Company", company, "book_advance_payments_in_separate_party_account", 1)

	currency = frappe.get_cached_value("Company", company, "default_currency")
	debtors = frappe.db.get_value("Company", company, "default_receivable_account")
	creditors = frappe.db.get_value("Company", company, "default_payable_account")
	cash = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
		"Company", company, "default_bank_account"
	)
	customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")

	rec_a = _ensure_adv(company, "E2E Adv Rec A", "Liability", "Receivable", ADVANCE_RECEIVED)
	rec_b = _ensure_adv(company, "E2E Adv Rec B", "Liability", "Receivable", ADVANCE_RECEIVED)
	rec_c = _ensure_adv(company, "E2E Adv Rec C", "Liability", "Receivable", ADVANCE_RECEIVED)
	paid_a = _ensure_adv(company, "E2E Adv Paid A", "Asset", "Payable", ADVANCE_PAID)
	paid_b = _ensure_adv(company, "E2E Adv Paid B", "Asset", "Payable", ADVANCE_PAID)
	paid_c = _ensure_adv(company, "E2E Adv Paid C", "Asset", "Payable", ADVANCE_PAID)

	frappe.db.set_value("Company", company, "default_advance_received_account", rec_a)
	frappe.db.set_value("Company", company, "default_advance_paid_account", paid_a)

	results = {"company": company, "received": [], "paid": [], "checks": []}

	def make_receive(account, amount):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"company": company,
				"posting_date": today(),
				"party_type": "Customer",
				"party": customer,
				"paid_from": account,
				"paid_to": cash,
				"paid_amount": amount,
				"received_amount": amount,
				"reference_no": frappe.generate_hash(length=8),
				"reference_date": today(),
			}
		)
		pe.insert()
		assert cint(pe.book_advance_payments_in_separate_party_account) == 1, pe.name
		assert pe.paid_from == account, pe.paid_from
		pe.submit()
		dr, cr = _gl_sum("Payment Entry", pe.name, account)
		assert cr > 0, f"expected credit on advance {account}"
		results["received"].append({"pe": pe.name, "account": account, "amount": amount, "gl_credit": cr})
		return pe

	def make_pay(account, amount):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Pay",
				"company": company,
				"posting_date": today(),
				"party_type": "Supplier",
				"party": supplier,
				"paid_from": cash,
				"paid_to": account,
				"paid_amount": amount,
				"received_amount": amount,
				"reference_no": frappe.generate_hash(length=8),
				"reference_date": today(),
			}
		)
		pe.insert()
		assert cint(pe.book_advance_payments_in_separate_party_account) == 1
		assert pe.paid_to == account
		pe.submit()
		dr, cr = _gl_sum("Payment Entry", pe.name, account)
		assert dr > 0, f"expected debit on advance {account}"
		results["paid"].append({"pe": pe.name, "account": account, "amount": amount, "gl_debit": dr})
		return pe

	# Normal Debtors receipt — flag OFF
	normal = frappe.get_doc(
		{
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"company": company,
			"posting_date": today(),
			"party_type": "Customer",
			"party": customer,
			"paid_from": debtors,
			"paid_to": cash,
			"paid_amount": 111,
			"received_amount": 111,
			"reference_no": frappe.generate_hash(length=8),
			"reference_date": today(),
		}
	)
	normal.insert()
	assert cint(normal.book_advance_payments_in_separate_party_account) == 0
	results["checks"].append({"normal_receive_flag": 0, "pe": normal.name})

	pe_a = make_receive(rec_a, 1000)
	pe_b = make_receive(rec_b, 2000)
	pe_c = make_receive(rec_c, 3000)
	make_pay(paid_a, 1500)
	make_pay(paid_b, 2500)
	make_pay(paid_c, 3500)

	entries = get_advance_payment_entries(
		"Customer",
		customer,
		[debtors],
		"Sales Order",
		default_advance_account=rec_a,
		include_unallocated=True,
		condition={"company": company, "get_payments": True},
	)
	found = {e.reference_name for e in entries}
	for pe in (pe_a, pe_b, pe_c):
		assert pe.name in found, f"missing {pe.name} in discovery"
	results["checks"].append({"discovered_received": sorted(found)})

	pay_entries = get_advance_payment_entries(
		"Supplier",
		supplier,
		[creditors],
		"Purchase Order",
		default_advance_account=paid_a,
		include_unallocated=True,
		condition={"company": company, "get_payments": True},
	)
	results["checks"].append({"discovered_paid_count": len(pay_entries)})

	frappe.db.commit()
	return results
