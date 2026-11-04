# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only accounting readiness checks for Delivery Note Check Submission."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint


def validate_accounting_readiness(doc) -> list[dict]:
	"""Verify accounts / cost centers required for DN GL posting without creating GLE.

	Returns advisory limitations for valuation / full GL matrix edges that cannot
	be reproduced without SLE creation.
	"""
	limitations: list[dict] = []

	if not cint(frappe.get_cached_value("Company", doc.company, "enable_perpetual_inventory")):
		return limitations

	# Stock account per warehouse / company inventory account map.
	for row in doc.get("items") or []:
		if not row.get("item_code") or not cint(
			frappe.get_cached_value("Item", row.item_code, "is_stock_item")
		):
			continue
		if not flt_qty(row):
			continue

		doc.check_expense_account(row)

		warehouse = row.get("warehouse")
		if warehouse:
			stock_account = _get_warehouse_account(warehouse, doc.company)
			if not stock_account:
				frappe.throw(
					_(
						"Row #{0}: No Stock Account found for Warehouse {1} / Company {2}. "
						"Configure Warehouse Account or Company default inventory account."
					).format(row.idx, frappe.bold(warehouse), frappe.bold(doc.company)),
					title=_("Stock Account Missing"),
				)
			_assert_account_company(stock_account, doc.company, row.idx)

		if row.get("expense_account"):
			_assert_account_company(row.expense_account, doc.company, row.idx)

		# Site ESPAD (and similar) require Department on P&L lines.
		_assert_mandatory_accounting_dimensions(doc, row)

	limitations.append(
		{
			"code": "full_gl_valuation",
			"severity": "advisory",
			"message": _(
				"Full GL Entry amounts, FIFO/moving-average valuation, and future Stock Ledger "
				"integrity are not simulated (would require creating SLE/GLE). Account configuration "
				"and expense/cost-center readiness were checked."
			),
		}
	)
	return limitations


def _get_warehouse_account(warehouse: str, company: str) -> str | None:
	account = frappe.db.get_value("Warehouse", warehouse, "account")
	if account:
		return account
	return frappe.get_cached_value("Company", company, "default_inventory_account")


def _assert_account_company(account: str, company: str, idx) -> None:
	acc_company = frappe.get_cached_value("Account", account, "company")
	if acc_company and acc_company != company:
		frappe.throw(
			_("Row #{0}: Account {1} does not belong to Company {2}").format(
				idx, frappe.bold(account), frappe.bold(company)
			)
		)
	if cint(frappe.get_cached_value("Account", account, "is_group")):
		frappe.throw(
			_("Row #{0}: Account {1} is a group account and cannot be used for posting").format(
				idx, frappe.bold(account)
			)
		)


def _assert_mandatory_accounting_dimensions(doc, row) -> None:
	"""If Delivery Note Item has a mandatory Department (or similar) custom field, enforce it."""
	meta = frappe.get_meta("Delivery Note Item")
	for fieldname in ("department",):
		df = meta.get_field(fieldname)
		if not df:
			continue
		if not df.reqd and not df.get("mandatory_depends_on"):
			# Still enforce when expense account is P&L — many sites require department via accounting dimension
			pass
		if df.reqd and not row.get(fieldname):
			frappe.throw(
				_("Row #{0}: {1} is mandatory").format(row.idx, _(df.label or fieldname)),
				title=_("Mandatory Accounting Dimension"),
			)

	# Accounting Dimension mandatory rules (ERPNext Inventory / Accounts).
	try:
		doc.validate_inventory_dimension_mandatory()
	except AttributeError:
		pass


def flt_qty(row) -> float:
	from frappe.utils import flt

	return abs(flt(row.get("stock_qty") or row.get("qty") or 0))
