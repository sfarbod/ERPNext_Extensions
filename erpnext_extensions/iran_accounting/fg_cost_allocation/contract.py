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


def _consumable_result(doc) -> dict:
	"""Evaluate consumable flags on a Monthly FG Cost Calculation-like object."""
	status = getattr(doc, "status", None)
	if status not in CONSUMABLE_STATUSES:
		return {
			"consumable": False,
			"reason": _("Status {0} is not consumable.").format(status),
			"status": status,
		}
	if not cint(getattr(doc, "is_consistent", 0)):
		return {"consumable": False, "reason": _("Calculation is inconsistent."), "status": status}
	if cint(getattr(doc, "shares_are_provisional", 0)):
		return {
			"consumable": False,
			"reason": _("Provisional shares (UOM incomplete)."),
			"status": status,
		}
	if cint(getattr(doc, "exception_count", 0)):
		return {
			"consumable": False,
			"reason": _("Blocking exceptions remain ({0}).").format(doc.exception_count),
			"status": status,
		}
	if cint(getattr(doc, "rule_count", 0)) <= 0:
		return {"consumable": False, "reason": _("No expense rules."), "status": status}
	if cint(getattr(doc, "over_allocated", 0)):
		return {"consumable": False, "reason": _("Over-allocated."), "status": status}
	return {"consumable": True, "reason": "", "status": status}


def assert_consumable(monthly_name: str):
	"""Raise if calculation must not be consumed by a downstream feature."""
	doc = frappe.get_doc("Monthly FG Cost Calculation", monthly_name)
	result = _consumable_result(doc)
	if not result.get("consumable"):
		raise FGCostAllocationNotConsumable(result.get("reason") or _("Not consumable."))
	return doc


@frappe.whitelist()
def is_consumable(monthly_name: str) -> dict:
	"""Non-throwing consumable check for Desk indicators."""
	doc = frappe.get_doc("Monthly FG Cost Calculation", monthly_name)
	if hasattr(doc, "check_permission"):
		doc.check_permission("read")
	return _consumable_result(doc)


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
	"""Historical Product × Expense rows only. Not used for active costing totals."""
	rev = _revision(monthly_name, revision)
	return frappe.get_all(
		DETAIL_DOCTYPE_MAP["product_expense"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=["*"],
	)


@frappe.whitelist()
def get_account_dimension_summary(monthly_name: str, revision: int | None = None):
	rev = _revision(monthly_name, revision)
	rows = frappe.get_all(
		DETAIL_DOCTYPE_MAP["account_dimension_summary"],
		filters={"monthly_calculation": monthly_name, "revision": rev},
		fields=[
			"name",
			"account",
			"account_name",
			"cost_center",
			"department",
			"expense_pool",
			"pool_monthly_net_expense",
			"allocated_batch_count",
			"total_applied_batch_share",
			"total_allocated_amount",
			"allocation_status",
			"currency",
		],
		order_by="account asc, cost_center asc, department asc",
	)
	return {"revision": rev, "count": len(rows), "rows": rows}
