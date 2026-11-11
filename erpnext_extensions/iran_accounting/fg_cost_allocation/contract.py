# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Stable data contract for future cost-absorption consumers."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.iran_accounting.fg_cost_allocation import CONSUMABLE_STATUSES, DETAIL_DOCTYPE_MAP


class FGCostAllocationNotConsumable(frappe.ValidationError):
	pass


def get_monthly_calculation(company: str, persian_year: int, persian_month: int):
	name = frappe.db.get_value(
		"Monthly FG Cost Calculation",
		{
			"company": company,
			"persian_year": cint(persian_year),
			"persian_month": cint(persian_month),
		},
		"name",
	)
	return frappe.get_doc("Monthly FG Cost Calculation", name) if name else None


def assert_consumable(monthly_name: str):
	"""Raise if calculation must not be consumed by a downstream feature."""
	doc = frappe.get_doc("Monthly FG Cost Calculation", monthly_name)
	if doc.status not in CONSUMABLE_STATUSES:
		raise FGCostAllocationNotConsumable(
			_("Monthly FG Cost Calculation {0} status {1} is not consumable.").format(
				monthly_name, doc.status
			)
		)
	if not cint(doc.is_consistent):
		raise FGCostAllocationNotConsumable(
			_("Monthly FG Cost Calculation {0} is inconsistent.").format(monthly_name)
		)
	return doc


def _revision(monthly_name: str, revision: int | None) -> int:
	if revision is not None:
		return cint(revision)
	return cint(frappe.db.get_value("Monthly FG Cost Calculation", monthly_name, "revision"))


def get_expense_pools(monthly_name: str, revision: int | None = None):
	rev = _revision(monthly_name, revision)
	return frappe.get_all(
		DETAIL_DOCTYPE_MAP["expense_pool"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=["*"],
	)


def get_gl_lines(expense_pool: str | None = None, monthly_name: str | None = None, revision: int | None = None):
	filters = {}
	if expense_pool:
		filters["expense_pool"] = expense_pool
	if monthly_name:
		filters["monthly_calculation"] = monthly_name
		filters["revision"] = _revision(monthly_name, revision)
	return frappe.get_all(DETAIL_DOCTYPE_MAP["gl_line"], filters=filters, fields=["*"])


def get_batch_allocations(monthly_name: str, revision: int | None = None):
	rev = _revision(monthly_name, revision)
	return frappe.get_all(
		DETAIL_DOCTYPE_MAP["batch"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=["*"],
	)


def get_batch_expense_allocations(monthly_name: str, revision: int | None = None):
	rev = _revision(monthly_name, revision)
	return frappe.get_all(
		DETAIL_DOCTYPE_MAP["batch_expense"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=["*"],
	)


def get_product_allocations(monthly_name: str, revision: int | None = None):
	rev = _revision(monthly_name, revision)
	return frappe.get_all(
		DETAIL_DOCTYPE_MAP["product"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=["*"],
	)


def get_product_expense_allocations(monthly_name: str, revision: int | None = None):
	rev = _revision(monthly_name, revision)
	return frappe.get_all(
		DETAIL_DOCTYPE_MAP["product_expense"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=["*"],
	)
