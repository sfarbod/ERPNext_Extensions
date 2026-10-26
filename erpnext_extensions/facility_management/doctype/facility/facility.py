# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from erpnext_extensions.facility_management.facility_accounting import (
	create_and_submit_receipt_je,
	refresh_facility_paid_fields,
)
from erpnext_extensions.facility_management.facility_balances import get_facility_balance_row
from erpnext_extensions.facility_management.facility_identity import (
	assert_exclusive_receipt_je,
	assert_facility_number_unique,
	clear_inherited_receipt_state,
)
from erpnext_extensions.facility_management.facility_monetary import (
	FACILITY_INPUT_CURRENCY_FIELDS,
	persist_exact_currency_fields,
)
from erpnext_extensions.facility_management.facility_settings_doc import (
	apply_facility_settings_defaults,
	get_facility_settings_defaults_payload,
	get_facility_settings_doc,
	resolve_account,
)


class Facility(Document):
	def before_insert(self):
		# Duplicate / Copy must never inherit receipt ownership or derived receipt state.
		clear_inherited_receipt_state(self)

	def validate(self):
		apply_facility_settings_defaults(self, overwrite=False)
		self._exact_currency = {}
		flag_exact = getattr(self.flags, "facility_exact_currency", None) or {}
		for fn in FACILITY_INPUT_CURRENCY_FIELDS:
			if fn in flag_exact:
				self._exact_currency[fn] = flag_exact[fn]
				continue
			val = self.__dict__.get(fn)
			if val is None:
				val = self.get(fn)
			if val not in (None, "") and isinstance(val, str):
				self._exact_currency[fn] = val
		assert_facility_number_unique(self)
		assert_exclusive_receipt_je(self)
		self._validate_accounts()
		self._validate_installment_count()
		self._validate_opening_amounts()
		self._validate_status_rules()
		self._recalculate_totals()
		self._sync_balance_fields()

	def after_insert(self):
		persist_exact_currency_fields("Facility", self.name, getattr(self, "_exact_currency", {}))

	def on_update(self):
		if getattr(self, "_exact_currency", None):
			persist_exact_currency_fields("Facility", self.name, self._exact_currency)

	def _recalculate_totals(self):
		self.total_liability_amount = flt(self.principal_amount) + flt(self.profit_amount)

	def _sync_balance_fields(self):
		if self.is_new():
			return
		bal = get_facility_balance_row(self)
		self.paid_principal_amount = bal["paid_principal"]
		self.paid_profit_amount = bal["paid_profit"]
		self.paid_penalty_amount = bal["paid_penalty"]
		self.remaining_principal_amount = bal["remaining_principal"]
		self.remaining_profit_amount = bal["remaining_profit"]
		self.remaining_total_amount = bal["remaining_total"]

	def _validate_opening_amounts(self):
		if not cint(self.is_opening_facility):
			if (
				flt(self.opening_paid_principal_amount)
				or flt(self.opening_paid_profit_amount)
				or flt(self.opening_paid_penalty_amount)
			):
				frappe.throw(_("Opening paid amounts are only allowed for Opening / Migrated facilities."))
			return
		if self.receipt_journal_entry:
			frappe.throw(_("Opening / migrated facilities cannot have a Receipt Journal Entry."))
		if flt(self.opening_paid_principal_amount) > flt(self.principal_amount):
			frappe.throw(_("Opening paid principal cannot exceed principal amount."))
		if flt(self.opening_paid_profit_amount) > flt(self.profit_amount):
			frappe.throw(_("Opening paid profit cannot exceed profit amount."))

	def _validate_status_rules(self):
		if self.status == "Active" and not cint(self.is_opening_facility):
			je_name = (self.receipt_journal_entry or "").strip()
			if not je_name:
				frappe.throw(
					_(
						"Active status requires a Receipt Journal Entry unless this is an Opening / Migrated facility."
					)
				)
			else:
				ds = frappe.db.get_value("Journal Entry", je_name, "docstatus")
				if ds is None or cint(ds) != 1:
					frappe.throw(
						_(
							"Active status requires a submitted Receipt Journal Entry. "
							"Referenced {0} is missing or not submitted (docstatus={1})."
						).format(je_name, ds),
					)
		if self.status == "Closed":
			bal = get_facility_balance_row(self)
			if flt(bal["remaining_principal"]) > 0 or flt(bal["remaining_profit"]) > 0:
				frappe.throw(_("Cannot close facility while principal or profit remains outstanding."))

	def _validate_installment_count(self):
		if cint(self.installment_count) < 0:
			frappe.throw(_("Installment Count cannot be negative."))

	def _validate_accounts(self):
		settings = get_facility_settings_doc(self.company)
		if flt(self.profit_amount) > 0 and not resolve_account(
			"deferred_loan_interest_account",
			facility=self,
			settings=settings,
		):
			frappe.throw(
				_("Deferred Loan Interest Account is required when Profit Amount is greater than zero.")
			)
		for req_fn, req_label in (
			("loan_payable_account", _("Loan Payable Account")),
			("bank_account", _("Bank Account")),
		):
			if not resolve_account(req_fn, facility=self, settings=settings):
				frappe.throw(_("{0} is required on the facility or in Facility Settings.").format(req_label))
		for fieldname, label, root_types, account_types in (
			(
				"loan_payable_account",
				_("Loan Payable Account"),
				("Liability",),
				None,
			),
			(
				"bank_account",
				_("Bank Account"),
				("Asset",),
				("Bank",),
			),
		):
			acc = self.get(fieldname)
			if not acc:
				continue
			self._validate_company_account(acc, label)
			meta = frappe.get_cached_value(
				"Account", acc, ["is_group", "root_type", "account_type"], as_dict=True
			)
			if meta and cint(meta.is_group):
				frappe.throw(_("{0} cannot be a group account.").format(label))
			if root_types and meta and meta.root_type not in root_types:
				frappe.throw(_("{0} must be a {1} account.").format(label, root_types[0]))
			if account_types and meta and meta.account_type not in account_types:
				frappe.throw(_("{0} must be of type {1}.").format(label, account_types[0]))
		if self.deferred_loan_interest_account:
			self._validate_company_account(
				self.deferred_loan_interest_account, _("Deferred Loan Interest Account")
			)
		if self.interest_expense_account:
			self._validate_company_account(self.interest_expense_account, _("Interest Expense Account"))
		if self.penalty_expense_account:
			self._validate_company_account(self.penalty_expense_account, _("Penalty Expense Account"))

	def _validate_company_account(self, account: str, label: str) -> None:
		company = frappe.get_cached_value("Account", account, "company")
		if company and company != self.company:
			frappe.throw(_("{0} must belong to company {1}.").format(label, self.company))


@frappe.whitelist()
def get_facility_settings_defaults(company: str) -> dict:
	return get_facility_settings_defaults_payload(company)


@frappe.whitelist()
def preview_receipt_journal_entry(name: str) -> dict:
	from erpnext_extensions.facility_management.facility_accounting import (
		preview_receipt_journal_entry as build_preview,
	)

	doc = frappe.get_doc("Facility", name)
	return build_preview(doc)


@frappe.whitelist()
def create_receipt_journal_entry(name: str) -> dict:
	from erpnext_extensions.facility_management.facility_identity import assign_receipt_journal_entry

	doc = frappe.get_doc("Facility", name)
	if cint(doc.is_opening_facility):
		frappe.throw(_("Opening / migrated facilities must not create a Receipt Journal Entry."))
	je_name = create_and_submit_receipt_je(doc)
	assign_receipt_journal_entry(
		name,
		je_name,
		received_amount=flt(doc.principal_amount),
		status="Active",
	)
	refresh_facility_paid_fields(name)
	frappe.db.commit()
	return {"journal_entry": je_name}


@frappe.whitelist()
def close_facility(name: str) -> dict:
	doc = frappe.get_doc("Facility", name)
	doc.status = "Closed"
	doc.validate()
	updates = {"status": "Closed"}
	if not doc.settlement_date:
		updates["settlement_date"] = frappe.utils.today()
	doc.db_set(updates, update_modified=True)
	frappe.db.commit()
	return {"status": "Closed", "settlement_date": updates.get("settlement_date")}


def get_account_query(doctype, txt, searchfield, start, page_len, filters):
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	company = filters.get("company")
	conditions = ["disabled = 0", "is_group = 0"]
	params = {"txt": f"%{txt}%", "start": start, "page_len": page_len}
	if company:
		conditions.append("company = %(company)s")
		params["company"] = company
	where = " AND ".join(conditions)
	return frappe.db.sql(
		f"""
		SELECT name, account_name FROM `tabAccount`
		WHERE {where} AND ({searchfield} LIKE %(txt)s OR account_name LIKE %(txt)s)
		ORDER BY name LIMIT %(page_len)s OFFSET %(start)s
		""",
		params,
	)
