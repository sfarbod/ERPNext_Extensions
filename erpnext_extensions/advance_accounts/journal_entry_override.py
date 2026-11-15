# Copyright (c) 2026, ERPNext Extensions contributors
"""Expand JE warning for all classified separate advance accounts (no hard block)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import comma_and

from erpnext_extensions.advance_accounts.constants import CF_ADVANCE_ACCOUNT_TYPE
from erpnext_extensions.advance_accounts.eligibility import get_classified_advance_accounts
from erpnext_extensions.advance_accounts.constants import DIRECTION_PAID, DIRECTION_RECEIVED


class JournalEntryAdvanceAccounts:
	"""``extend_doctype_class`` mixin for Journal Entry."""

	def validate_advance_accounts(self):
		journal_accounts = {x.account for x in self.accounts if x.account}
		advance_accounts: set[str] = set()

		# Company defaults (core behaviour)
		for field in ("default_advance_received_account", "default_advance_paid_account"):
			val = frappe.get_cached_value("Company", self.company, field)
			if val:
				advance_accounts.add(val)

		# All classified separate-party advance accounts for this company
		if frappe.db.has_column("Account", CF_ADVANCE_ACCOUNT_TYPE):
			advance_accounts.update(get_classified_advance_accounts(self.company, DIRECTION_RECEIVED))
			advance_accounts.update(get_classified_advance_accounts(self.company, DIRECTION_PAID))

		advance_accounts.discard(None)
		advance_accounts.discard("")

		if advance_accounts_used := journal_accounts & advance_accounts:
			frappe.msgprint(
				_(
					"Making Journal Entries against advance accounts: {0} is not recommended. "
					"These Journals won't be available for Reconciliation."
				).format(frappe.bold(comma_and(sorted(advance_accounts_used))))
			)
