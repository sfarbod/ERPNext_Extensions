# Copyright (c) 2026, ERPNext Extensions contributors
"""Helpers for Playwright UI smoke (v5.7.42)."""

from __future__ import annotations

import frappe
from frappe.utils import cint, today

from erpnext_extensions.advance_accounts.bootstrap import apply as apply_advance_bootstrap
from erpnext_extensions.advance_accounts.constants import ADVANCE_RECEIVED, CF_ADVANCE_ACCOUNT_TYPE
from erpnext_extensions.advance_accounts.custom_fields import ensure_custom_fields
from erpnext_extensions.advance_accounts.tests.e2e_advance_accounts_v5742 import _company, _ensure_adv


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
