# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class FinishedGoodsCostAllocationSettings(Document):
	def validate(self):
		self._validate_company_unique()
		self._sync_business_calendar()
		self._validate_rule_accounts_belong_to_company()

	def _validate_company_unique(self):
		if not self.company:
			return
		filters = {"company": self.company}
		if not self.is_new():
			filters["name"] = ("!=", self.name)
		if frappe.db.exists("Finished Goods Cost Allocation Settings", filters):
			frappe.throw(
				_("Finished Goods Cost Allocation Settings already exists for Company {0}.").format(
					frappe.bold(self.company)
				),
				title=_("Duplicate Company"),
			)

	def _sync_business_calendar(self):
		if not self.company:
			self.business_calendar = None
			return
		self.business_calendar = frappe.db.get_value("Company", self.company, "business_calendar")

	def _validate_rule_accounts_belong_to_company(self):
		if not self.company:
			return
		for row in self.expense_account_dimension_rules or []:
			if not row.account:
				continue
			account_company = frappe.db.get_value("Account", row.account, "company")
			if account_company and account_company != self.company:
				frappe.throw(
					_("Row {0}: Account {1} belongs to Company {2}, not {3}.").format(
						row.idx,
						frappe.bold(row.account),
						frappe.bold(account_company),
						frappe.bold(self.company),
					),
					title=_("Company Mismatch"),
				)
