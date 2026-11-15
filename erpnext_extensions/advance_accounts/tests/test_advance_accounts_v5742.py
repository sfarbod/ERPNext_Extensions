# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit + integration tests for multi advance accounts (v5.7.42)."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import add_days, cint, today

from erpnext_extensions.advance_accounts.bootstrap import apply as apply_advance_bootstrap
from erpnext_extensions.advance_accounts.constants import (
	ADVANCE_PAID,
	ADVANCE_RECEIVED,
	CF_ADVANCE_ACCOUNT_TYPE,
)
from erpnext_extensions.advance_accounts.custom_fields import ensure_custom_fields
from erpnext_extensions.advance_accounts.eligibility import (
	DIRECTION_PAID,
	DIRECTION_RECEIVED,
	get_eligible_advance_accounts,
	is_eligible_advance_account,
)


def _uniq(prefix: str) -> str:
	return f"{prefix}-{frappe.generate_hash(length=6)}"


def _company() -> str:
	# Prefer a company that has an active Fiscal Year linked (required for PE submit).
	linked = frappe.db.sql(
		"""
		SELECT fyc.company
		FROM `tabFiscal Year Company` fyc
		INNER JOIN `tabFiscal Year` fy ON fy.name = fyc.parent
		WHERE IFNULL(fy.disabled, 0) = 0
		ORDER BY fy.year_start_date DESC
		LIMIT 1
		"""
	)
	if linked:
		return linked[0][0]
	return frappe.db.get_value("Company", {}, "name")


class TestAdvanceAccountClassification(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		ensure_custom_fields()
		apply_advance_bootstrap()
		cls.company = _company()
		if not cls.company:
			raise unittest.SkipTest("No Company")
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")

	def _parent(self, root_type: str) -> str:
		return frappe.db.get_value(
			"Account",
			{"company": self.company, "root_type": root_type, "is_group": 1},
			"name",
			order_by="lft asc",
		)

	def test_reject_wrong_root_type_for_received(self):
		parent = self._parent("Asset")
		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": _uniq("Bad Adv Rec"),
				"company": self.company,
				"parent_account": parent,
				"is_group": 0,
				"account_type": "Receivable",
				"account_currency": self.currency,
			}
		)
		doc.set(CF_ADVANCE_ACCOUNT_TYPE, ADVANCE_RECEIVED)
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_accept_valid_advance_received_and_paid(self):
		rec = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": _uniq("Adv Rec OK"),
				"company": self.company,
				"parent_account": self._parent("Liability"),
				"is_group": 0,
				"account_type": "Receivable",
				"account_currency": self.currency,
			}
		)
		rec.set(CF_ADVANCE_ACCOUNT_TYPE, ADVANCE_RECEIVED)
		rec.insert(ignore_permissions=True)

		paid = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": _uniq("Adv Paid OK"),
				"company": self.company,
				"parent_account": self._parent("Asset"),
				"is_group": 0,
				"account_type": "Payable",
				"account_currency": self.currency,
			}
		)
		paid.set(CF_ADVANCE_ACCOUNT_TYPE, ADVANCE_PAID)
		paid.insert(ignore_permissions=True)

		self.assertTrue(is_eligible_advance_account(rec.name, self.company, DIRECTION_RECEIVED))
		self.assertTrue(is_eligible_advance_account(paid.name, self.company, DIRECTION_PAID))
		eligible = get_eligible_advance_accounts(self.company, DIRECTION_RECEIVED)
		self.assertIn(rec.name, eligible)


class TestPaymentEntryAdvanceBehavior(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		ensure_custom_fields()
		apply_advance_bootstrap()
		cls.company = _company()
		if not cls.company:
			raise unittest.SkipTest("No Company")
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")
		frappe.db.set_value("Company", cls.company, "book_advance_payments_in_separate_party_account", 1)

		cls.adv_rec = cls._ensure_advance("v5742 Adv Rec A", "Liability", "Receivable", ADVANCE_RECEIVED)
		cls.adv_rec_b = cls._ensure_advance("v5742 Adv Rec B", "Liability", "Receivable", ADVANCE_RECEIVED)
		cls.adv_paid = cls._ensure_advance("v5742 Adv Paid A", "Asset", "Payable", ADVANCE_PAID)
		cls.adv_paid_b = cls._ensure_advance("v5742 Adv Paid B", "Asset", "Payable", ADVANCE_PAID)

		# Keep existing company defaults if present; otherwise set ours
		if not frappe.db.get_value("Company", cls.company, "default_advance_received_account"):
			frappe.db.set_value("Company", cls.company, "default_advance_received_account", cls.adv_rec)
		if not frappe.db.get_value("Company", cls.company, "default_advance_paid_account"):
			frappe.db.set_value("Company", cls.company, "default_advance_paid_account", cls.adv_paid)

		cls.debtors = frappe.db.get_value("Company", cls.company, "default_receivable_account")
		cls.creditors = frappe.db.get_value("Company", cls.company, "default_payable_account")
		cls.bank = frappe.db.get_value("Company", cls.company, "default_bank_account")
		cls.cash = frappe.db.get_value("Company", cls.company, "default_cash_account") or cls.bank
		if not all([cls.debtors, cls.creditors, cls.cash]):
			raise unittest.SkipTest("Missing company default AR/AP/cash accounts")

		cls.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		cls.supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
		if not cls.customer or not cls.supplier:
			raise unittest.SkipTest("Need existing Customer and Supplier")

	@classmethod
	def _ensure_advance(cls, name, root_type, account_type, advance_type):
		existing = frappe.db.exists("Account", {"account_name": name, "company": cls.company})
		if existing:
			frappe.db.set_value("Account", existing, CF_ADVANCE_ACCOUNT_TYPE, advance_type)
			frappe.db.set_value("Account", existing, "account_type", account_type)
			return existing
		parent = frappe.db.get_value(
			"Account", {"company": cls.company, "root_type": root_type, "is_group": 1}, "name"
		)
		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"company": cls.company,
				"parent_account": parent,
				"is_group": 0,
				"account_type": account_type,
				"account_currency": cls.currency,
			}
		)
		doc.set(CF_ADVANCE_ACCOUNT_TYPE, advance_type)
		doc.insert(ignore_permissions=True)
		return doc.name

	@classmethod
	def _ensure_party(cls, doctype, name):
		if frappe.db.exists(doctype, name):
			return name
		kwargs = {"doctype": doctype, f"{doctype.lower()}_name": name}
		doc = frappe.get_doc(kwargs)
		if doctype == "Customer":
			doc.customer_group = frappe.db.get_value(
				"Customer Group", {"is_group": 0}, "name"
			) or frappe.db.get_value("Customer Group", {}, "name")
			doc.territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or frappe.db.get_value(
				"Territory", {}, "name"
			)
		else:
			doc.supplier_group = frappe.db.get_value(
				"Supplier Group", {"is_group": 0}, "name"
			) or frappe.db.get_value("Supplier Group", {}, "name")
		doc.insert(ignore_permissions=True)
		return doc.name

	def _receive_pe(self, paid_from, amount=1000, apply_liability=True):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"company": self.company,
				"posting_date": today(),
				"party_type": "Customer",
				"party": self.customer,
				"paid_from": paid_from,
				"paid_to": self.cash,
				"paid_amount": amount,
				"received_amount": amount,
				"reference_no": _uniq("REF"),
				"reference_date": today(),
			}
		)
		pe.setup_party_account_field()
		if apply_liability:
			pe.set_liability_account()
		return pe

	def _pay_pe(self, paid_to, amount=1000, apply_liability=True):
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Pay",
				"company": self.company,
				"posting_date": today(),
				"party_type": "Supplier",
				"party": self.supplier,
				"paid_from": self.cash,
				"paid_to": paid_to,
				"paid_amount": amount,
				"received_amount": amount,
				"reference_no": _uniq("REF"),
				"reference_date": today(),
			}
		)
		pe.setup_party_account_field()
		if apply_liability:
			pe.set_liability_account()
		return pe

	def test_normal_debtors_flag_off(self):
		pe = self._receive_pe(self.debtors)
		self.assertEqual(cint(pe.book_advance_payments_in_separate_party_account), 0)
		self.assertEqual(pe.paid_from, self.debtors)

	def test_advance_received_preserved(self):
		pe = self._receive_pe(self.adv_rec_b)
		self.assertEqual(cint(pe.book_advance_payments_in_separate_party_account), 1)
		self.assertEqual(pe.paid_from, self.adv_rec_b)
		pe.insert()
		self.assertEqual(pe.paid_from, self.adv_rec_b)
		self.assertEqual(cint(pe.book_advance_payments_in_separate_party_account), 1)
		pe.submit()

	def test_normal_creditors_flag_off(self):
		pe = self._pay_pe(self.creditors)
		self.assertEqual(cint(pe.book_advance_payments_in_separate_party_account), 0)

	def test_advance_paid_preserved(self):
		pe = self._pay_pe(self.adv_paid_b)
		self.assertEqual(cint(pe.book_advance_payments_in_separate_party_account), 1)
		pe.insert()
		pe.submit()
		self.assertEqual(pe.paid_to, self.adv_paid_b)

	def test_wrong_direction_rejected(self):
		pe = self._receive_pe(self.adv_paid, apply_liability=False)
		with self.assertRaises(frappe.ValidationError):
			pe.set_liability_account()

	def test_feature_off(self):
		frappe.db.set_value("Company", self.company, "book_advance_payments_in_separate_party_account", 0)
		try:
			pe = self._receive_pe(self.adv_rec_b)
			self.assertEqual(cint(pe.book_advance_payments_in_separate_party_account), 0)
		finally:
			frappe.db.set_value("Company", self.company, "book_advance_payments_in_separate_party_account", 1)

	def test_multi_account_invoice_discovery(self):
		from erpnext.controllers.accounts_controller import get_advance_payment_entries

		pe1 = self._receive_pe(self.adv_rec, 500)
		pe1.insert()
		pe1.submit()
		pe2 = self._receive_pe(self.adv_rec_b, 700)
		pe2.insert()
		pe2.submit()

		entries = get_advance_payment_entries(
			"Customer",
			self.customer,
			[self.debtors],
			"Sales Order",
			default_advance_account=self.adv_rec,
			include_unallocated=True,
			condition={"company": self.company, "get_payments": True},
		)
		names = {d.reference_name for d in entries}
		self.assertIn(pe1.name, names)
		self.assertIn(pe2.name, names)


if __name__ == "__main__":
	unittest.main()
