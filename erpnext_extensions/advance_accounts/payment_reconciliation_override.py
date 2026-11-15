# Copyright (c) 2026, ERPNext Extensions contributors
"""Payment Reconciliation: include all eligible advance accounts for invoices."""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate, nowdate

from erpnext.accounts.utils import get_outstanding_invoices

from erpnext_extensions.advance_accounts.eligibility import (
	direction_for_party_type,
	get_eligible_advance_accounts,
)


class PaymentReconciliationAdvanceAccounts:
	"""``extend_doctype_class`` mixin for Payment Reconciliation."""

	def get_invoice_entries(self, vouchers=None):
		self.build_qb_filter_conditions(get_invoices=True)

		accounts = [self.receivable_payable_account]

		direction = direction_for_party_type(self.party_type)
		eligible = get_eligible_advance_accounts(
			self.company, direction, party_type=self.party_type, party=self.party
		)
		for acc in eligible:
			if acc and acc not in accounts:
				accounts.append(acc)

		if self.default_advance_account and self.default_advance_account not in accounts:
			accounts.append(self.default_advance_account)

		non_reconciled_invoices = get_outstanding_invoices(
			self.party_type,
			self.party,
			accounts,
			common_filter=self.common_filter_conditions,
			posting_date=self.ple_posting_date_filter,
			min_outstanding=self.minimum_invoice_amount if self.minimum_invoice_amount else None,
			max_outstanding=self.maximum_invoice_amount if self.maximum_invoice_amount else None,
			accounting_dimensions=self.accounting_dimension_filter_conditions,
			vouchers=vouchers,
			limit=self.invoice_limit if not vouchers else None,
			voucher_no=self.invoice_name,
		)

		cr_dr_notes = (
			[x.voucher_no for x in self.return_invoices]
			if self.party_type in ["Customer", "Supplier"]
			else []
		)
		non_reconciled_invoices = [x for x in non_reconciled_invoices if x.voucher_no not in cr_dr_notes]

		if self.invoice_limit and not vouchers:
			non_reconciled_invoices = non_reconciled_invoices[: self.invoice_limit]

		non_reconciled_invoices = sorted(
			non_reconciled_invoices, key=lambda k: k["posting_date"] or getdate(nowdate())
		)

		self.add_invoice_entries(non_reconciled_invoices)
