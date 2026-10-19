# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from erpnext_extensions.facility_management.facility_accounting import (
	cancel_journal_entry,
	create_and_submit_repayment_je,
	refresh_facility_paid_fields,
)
from erpnext_extensions.facility_management.facility_accounting import (
	preview_repayment_journal_entry as build_repayment_je_preview,
)
from erpnext_extensions.facility_management.facility_balances import get_facility_balance_row
from erpnext_extensions.facility_management.facility_debt_purchase import (
	REPAYMENT_METHOD_BANK,
	is_debt_purchase_cheque_method,
	normalize_repayment_method,
	settle_debt_purchase_on_submit,
	validate_bank_account_method_fields,
	validate_debt_purchase_cheque_repayment,
)
from erpnext_extensions.facility_management.facility_monetary import (
	FACILITY_REPAYMENT_CURRENCY_FIELDS,
	parse_facility_amount,
	persist_exact_currency_fields,
)
from erpnext_extensions.facility_management.facility_settings_doc import (
	get_facility_settings_doc,
	resolve_account,
	validate_repayment_je_prerequisites,
)

_REPAYMENT_ACCOUNT_FIELDS = (
	"bank_account",
	"loan_payable_account",
	"deferred_loan_interest_account",
	"interest_expense_account",
	"penalty_expense_account",
)


class FacilityRepayment(Document):
	def _repayment_amounts_decimal(self):
		return {fn: parse_facility_amount(self.get(fn)) for fn in FACILITY_REPAYMENT_CURRENCY_FIELDS}

	def _recalculate_total_payment(self) -> None:
		amounts = self._repayment_amounts_decimal()
		total = sum(amounts.values())
		self.total_payment_amount = flt(total)

	def _capture_exact_currency(self) -> None:
		self._exact_currency = {}
		flag_exact = getattr(self.flags, "facility_exact_currency", None) or {}
		for fn in FACILITY_REPAYMENT_CURRENCY_FIELDS:
			if fn in flag_exact:
				self._exact_currency[fn] = flag_exact[fn]
				continue
			val = self.get(fn)
			if val not in (None, "") and isinstance(val, str):
				self._exact_currency[fn] = val

	def _exact_persist_fields(self) -> dict[str, object]:
		fields = dict(getattr(self, "_exact_currency", None) or {})
		if not fields:
			fields = {fn: self.get(fn) for fn in FACILITY_REPAYMENT_CURRENCY_FIELDS}
		total = sum(
			parse_facility_amount(fields.get(fn, self.get(fn))) for fn in FACILITY_REPAYMENT_CURRENCY_FIELDS
		)
		fields["total_payment_amount"] = format(total, "f")
		return fields

	def _persist_amount_columns(self) -> None:
		if self.is_new():
			return
		persist_exact_currency_fields("Facility Repayment", self.name, self._exact_persist_fields())

	def _fill_empty_repayment_accounts(self, facility, settings) -> None:
		for fn in _REPAYMENT_ACCOUNT_FIELDS:
			if self.get(fn):
				continue
			# Do not auto-fill bank_account for Debt Purchase Cheque method.
			if fn == "bank_account" and is_debt_purchase_cheque_method(self):
				continue
			val = resolve_account(fn, repayment=self, facility=facility, settings=settings, required=False)
			if val:
				self.set(fn, val)

	def _normalize_repayment_method_fields(self) -> None:
		method = normalize_repayment_method(self.repayment_method)
		self.repayment_method = method
		if method == REPAYMENT_METHOD_BANK:
			self.post_dated_cheque = None
		else:
			# Stale bank must not be used as settlement credit; clear for DP method.
			self.bank_account = None

	def before_save(self):
		self._capture_exact_currency()
		self._normalize_repayment_method_fields()
		self._recalculate_total_payment()

	def validate(self):
		self._capture_exact_currency()
		self._normalize_repayment_method_fields()
		self._recalculate_total_payment()
		if flt(self.total_payment_amount) <= 0:
			frappe.throw(_("Enter at least one of principal, profit, or penalty amount."))
		facility = frappe.get_doc("Facility", self.facility)
		if facility.status != "Active":
			frappe.throw(_("Facility must be Active to record a repayment."))
		bal = get_facility_balance_row(facility)
		if flt(self.principal_amount) > flt(bal["remaining_principal"]):
			frappe.throw(_("Principal amount exceeds remaining principal."))
		if flt(self.profit_amount) > flt(bal["remaining_profit"]):
			frappe.throw(_("Profit amount exceeds remaining profit."))
		settings = get_facility_settings_doc(facility.company)
		self._fill_empty_repayment_accounts(facility, settings)

		if is_debt_purchase_cheque_method(self):
			validate_debt_purchase_cheque_repayment(self, facility=facility)
		else:
			validate_bank_account_method_fields(self)
			validate_repayment_je_prerequisites(
				self,
				facility,
				settings,
				principal=self.principal_amount,
				profit=self.profit_amount,
				penalty=self.penalty_amount,
			)

	def after_insert(self):
		persist_exact_currency_fields("Facility Repayment", self.name, self._exact_persist_fields())

	def on_update(self):
		if self.docstatus == 0:
			self._persist_amount_columns()

	def on_submit(self):
		persist_exact_currency_fields("Facility Repayment", self.name, self._exact_persist_fields())
		je = create_and_submit_repayment_je(self)
		self.db_set("journal_entry", je, update_modified=False)
		if is_debt_purchase_cheque_method(self):
			settle_debt_purchase_on_submit(self, je)
		refresh_facility_paid_fields(self.facility)

	def on_cancel(self):
		# Bank Account: preserve existing cancel order exactly.
		# Debt Purchase Cheque fail-safe order:
		#   1) lock + revalidate PDC is Settled by this repayment
		#   2) cancel settlement JE
		#   3) only then remove Settlement journal ref, restore Assigned, clear links
		# If JE cancel fails: PDC/links/refs must remain Settled (no partial rollback).
		# FLAG_IN_REPAYMENT_CANCEL lets Journal Entry hooks know this cancel is owned
		# by Facility Repayment (avoid blocking JE cancel / double reconciliation).
		from erpnext_extensions.facility_management.facility_je_reconciliation import (
			FLAG_IN_REPAYMENT_CANCEL,
		)

		je_name = self.journal_entry
		frappe.flags[FLAG_IN_REPAYMENT_CANCEL] = True
		try:
			if is_debt_purchase_cheque_method(self):
				from frappe.utils.synchronization import filelock

				from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
					WORKFLOW_DEBT_PURCHASE_SETTLED,
					normalize_workflow_state_value,
				)
				from erpnext_extensions.facility_management.facility_debt_purchase import (
					restore_pdc_after_debt_purchase_cancel,
				)

				pdc_name = (self.post_dated_cheque or "").strip()
				lock_name = f"pdc_debt_purchase_settle_{pdc_name}"
				with filelock(lock_name, timeout=120):
					pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
					if normalize_workflow_state_value(pdc.workflow_state) != WORKFLOW_DEBT_PURCHASE_SETTLED:
						frappe.throw(
							_("Linked cheque is not in Debt Purchase Settled state."),
							title=_("Debt Purchase Cancel"),
						)
					if (pdc.debt_purchase_repayment or "").strip() != self.name:
						frappe.throw(
							_("Cheque is not settled by this Facility Repayment."),
							title=_("Debt Purchase Cancel"),
						)
					try:
						cancel_journal_entry(je_name)
					except Exception:
						raise
					self.db_set("journal_entry", None, update_modified=False)
					restore_pdc_after_debt_purchase_cancel(pdc, self, settlement_je=je_name)
				refresh_facility_paid_fields(self.facility)
				return

			try:
				cancel_journal_entry(je_name)
			except Exception:
				raise
			self.db_set("journal_entry", None, update_modified=False)
			refresh_facility_paid_fields(self.facility)
		finally:
			frappe.flags[FLAG_IN_REPAYMENT_CANCEL] = False


@frappe.whitelist()
def preview_repayment_journal_entry(doc=None):
	"""Build repayment JE lines without submitting (same builder as on_submit)."""
	if isinstance(doc, str):
		doc = frappe.parse_json(doc)
	if not doc:
		frappe.throw(_("Document required for preview."))
	payload = dict(doc)
	payload.setdefault("doctype", "Facility Repayment")
	repayment = frappe.get_doc(payload)
	return build_repayment_je_preview(repayment)
