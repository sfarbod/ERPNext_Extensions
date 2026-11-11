# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class MonthlyFGCostCalculation(Document):
	def validate(self):
		self._validate_persian_month()
		self._validate_company_period_unique()
		self._default_currency()

	def on_trash(self):
		if cint(self.is_consumed):
			frappe.throw(_("Cannot delete a consumed Monthly FG Cost Calculation."))
		from erpnext_extensions.iran_accounting.fg_cost_allocation.persist import clear_details

		clear_details(self.name)

	def _validate_persian_month(self):
		month = cint(self.persian_month)
		if self.persian_month is None or month < 1 or month > 12:
			frappe.throw(
				_("Persian Month must be between 1 and 12."),
				title=_("Invalid Persian Month"),
			)

	def _validate_company_period_unique(self):
		if not self.company or self.persian_year is None or self.persian_month is None:
			return
		filters = {
			"company": self.company,
			"persian_year": cint(self.persian_year),
			"persian_month": cint(self.persian_month),
		}
		if not self.is_new():
			filters["name"] = ("!=", self.name)
		if frappe.db.exists("Monthly FG Cost Calculation", filters):
			frappe.throw(
				_(
					"Monthly FG Cost Calculation already exists for Company {0}, "
					"Persian Year {1}, Month {2}."
				).format(
					frappe.bold(self.company),
					frappe.bold(self.persian_year),
					frappe.bold(self.persian_month),
				),
				title=_("Duplicate Period"),
			)

	def _default_currency(self):
		if self.company and not self.currency:
			self.currency = frappe.get_cached_value("Company", self.company, "default_currency")
