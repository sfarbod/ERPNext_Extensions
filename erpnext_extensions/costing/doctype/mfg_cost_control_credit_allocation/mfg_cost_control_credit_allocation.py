# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class MFGCostControlCreditAllocation(Document):
	def validate(self):
		self._validate_persian_month()
		self._set_persian_period()
		self._default_currency()
		self._validate_unique_active_period()

	def before_submit(self):
		if self.status not in ("Calculated", "Submitted"):
			frappe.throw(_("Calculate the allocation before submit."))
		if cint(self.has_unresolved_sources):
			frappe.throw(
				_("Cannot submit while unresolved credit sources remain. Review Sources table.")
			)
		if not cint(self.is_consistent):
			frappe.throw(_("Cannot submit an inconsistent Control Credit Allocation."))

	def on_submit(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.engine import (
			on_submit_control_credit,
		)

		on_submit_control_credit(self)

	def on_cancel(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.engine import (
			on_cancel_control_credit,
		)

		on_cancel_control_credit(self)

	def _validate_persian_month(self):
		month = cint(self.persian_month)
		if self.persian_month is None or month < 1 or month > 12:
			frappe.throw(_("Persian Month must be between 1 and 12."))

	def _set_persian_period(self):
		if self.persian_year is None or self.persian_month is None:
			self.persian_period = None
			return
		self.persian_period = f"{cint(self.persian_year):04d}-{cint(self.persian_month):02d}"

	def _default_currency(self):
		if self.company and not self.currency:
			self.currency = frappe.get_cached_value("Company", self.company, "default_currency")

	def _validate_unique_active_period(self):
		"""One non-cancelled allocation per company + persian period."""
		if not self.company or self.persian_year is None or self.persian_month is None:
			return
		if self.status == "Cancelled" or cint(self.docstatus) == 2:
			return
		filters = {
			"company": self.company,
			"persian_year": cint(self.persian_year),
			"persian_month": cint(self.persian_month),
			"docstatus": ("<", 2),
			"status": ("!=", "Cancelled"),
		}
		if not self.is_new():
			filters["name"] = ("!=", self.name)
		existing = frappe.db.exists("MFG Cost Control Credit Allocation", filters)
		if existing:
			frappe.throw(
				_(
					"Active Control Credit Allocation {0} already exists for {1} / {2}-{3:02d}."
				).format(
					frappe.bold(existing),
					frappe.bold(self.company),
					cint(self.persian_year),
					cint(self.persian_month),
				),
				title=_("Duplicate Period"),
			)


@frappe.whitelist()
def calculate(name: str):
	from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.engine import (
		calculate_control_credit_allocation,
	)

	return calculate_control_credit_allocation(name, commit=True)
