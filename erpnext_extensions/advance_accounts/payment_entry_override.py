# Copyright (c) 2026, ERPNext Extensions contributors
"""Payment Entry mixin: account-driven separate-advance flag (flexible Scenario D)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.advance_accounts.eligibility import (
	company_feature_enabled,
	direction_for_party_type,
	direction_for_payment_type,
	is_eligible_advance_account,
	is_wrong_direction_advance,
)


class PaymentEntryAdvanceAccounts:
	"""``extend_doctype_class`` mixin for Payment Entry."""

	def set_liability_account(self):
		"""Compute book_advance flag from party-account eligibility; never force-overwrite.

		Approved flexible Scenario D (v5.7.42):
		- Company feature OFF → flag False (standard ERPNext).
		- Invoice references → flag False (normal payment against invoice).
		- Party account eligible advance (correct direction) → flag True, keep account.
		- Wrong-direction classified advance → reject.
		- Normal Debtors/Creditors → flag False, keep account (no overwrite to default).
		"""
		# Auto setting only in draft; Internal Transfer unchanged
		if cint(self.docstatus) > 0 or self.payment_type == "Internal Transfer":
			return

		self.book_advance_payments_in_separate_party_account = 0

		if self.party_type not in ("Customer", "Supplier"):
			self.is_opening = "No"
			return

		if not company_feature_enabled(self.company):
			self.is_opening = "No"
			return

		# Ensure party_account_field / party_account are current
		if not getattr(self, "party_account_field", None):
			self.setup_party_account_field()

		party_account = (self.party_account or self.get(self.party_account_field) or "").strip()

		# Invoice (or other non-order) references → normal payment path
		if self.references:
			allowed_types = frozenset(["Sales Order", "Purchase Order"])
			reference_types = {x.reference_doctype for x in self.references}
			if reference_types - allowed_types:
				self.book_advance_payments_in_separate_party_account = 0
				self.is_opening = "No"
				return

		direction = direction_for_payment_type(self.payment_type)
		party_direction = direction_for_party_type(self.party_type)
		# Payment type and party type must agree for advance path
		if direction and party_direction and direction != party_direction:
			# e.g. Pay + Customer — leave as non-separate-advance
			self.is_opening = "No"
			return

		if not party_account:
			self.is_opening = "No"
			return

		# Cross-company / missing account handled below via eligibility
		acc_company = frappe.db.get_value("Account", party_account, "company")
		if acc_company and acc_company != self.company:
			frappe.throw(
				_("Account {0} does not belong to company {1}.").format(party_account, self.company),
				title=_("Advance Account"),
			)

		if is_wrong_direction_advance(party_account, direction):
			frappe.throw(
				_(
					"Account {0} is classified for the opposite advance direction and cannot be used "
					"on this {1} Payment Entry."
				).format(frappe.bold(party_account), _(self.payment_type)),
				title=_("Advance Account"),
			)

		if is_eligible_advance_account(
			party_account,
			self.company,
			direction,
			party_type=self.party_type,
			party=self.party,
		):
			# Keep selected advance account; enable separate-party advance path
			self.book_advance_payments_in_separate_party_account = 1
			self.is_opening = "No"
			return

		# Normal receivable/payable (or other non-advance) — do not overwrite
		self.book_advance_payments_in_separate_party_account = 0
		self.is_opening = "No"
