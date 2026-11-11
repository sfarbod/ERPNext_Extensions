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
		self._validate_unique_item_primary_uom()
		self._validate_unique_expense_rules()
		self._refresh_uom_row_statuses()

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

	def _validate_unique_item_primary_uom(self):
		seen = set()
		for row in self.item_primary_uoms or []:
			if not row.item:
				continue
			if row.item in seen:
				frappe.throw(_("Duplicate Primary UOM row for Item {0}.").format(row.item))
			seen.add(row.item)

	def _validate_unique_expense_rules(self):
		seen = set()
		for row in self.expense_account_dimension_rules or []:
			key = (
				(row.account or "").strip(),
				(row.cost_center or "").strip(),
				(row.department or "").strip(),
			)
			if key in seen:
				frappe.throw(
					_("Duplicate expense rule for Account/Cost Center/Department: {0} / {1} / {2}").format(
						key[0], key[1] or "(blank)", key[2] or "(blank)"
					)
				)
			seen.add(key)

	def _refresh_uom_row_statuses(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import (
			get_item_stock_uom,
			resolve_conversion,
		)

		for row in self.item_primary_uoms or []:
			if not row.item or not row.primary_uom:
				continue
			factor, status, remarks = resolve_conversion(row.item, row.primary_uom)
			row.stock_uom = get_item_stock_uom(row.item)
			row.conversion_factor = float(factor) if factor is not None else None
			row.conversion_status = status
			row.remarks = remarks
