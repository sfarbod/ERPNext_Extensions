# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint


def get_consignment_settings(company: str):
	if not company:
		frappe.throw(_("Company is required for Consignment Stock Settings."))
	name = frappe.db.get_value("Consignment Stock Settings", {"company": company}, "name")
	if not name:
		frappe.throw(
			_("Consignment Stock Settings not configured for company {0}.").format(company)
		)
	return frappe.get_cached_doc("Consignment Stock Settings", name)


def validate_settings_accounts(settings) -> None:
	company = settings.company
	for fieldname, label in (
		("consignment_temporary_clearing_account", _("Consignment Temporary Clearing Account")),
		("consignment_valuation_difference_account", _("Consignment Valuation Difference Account")),
	):
		account = settings.get(fieldname)
		if not account:
			continue
		_validate_account(account, company, label)

	temp = settings.get("consignment_temporary_clearing_account")
	if temp:
		account_type = frappe.get_cached_value("Account", temp, "account_type")
		if account_type == "Stock":
			frappe.throw(
				_("Consignment Temporary Clearing Account {0} must not be of type Stock.").format(temp)
			)

	wh = settings.get("default_consignment_warehouse")
	if wh:
		validate_consignment_warehouse(wh, company)


def _validate_account(account: str, company: str, label: str) -> None:
	meta = frappe.db.get_value(
		"Account",
		account,
		["company", "is_group", "disabled", "account_type"],
		as_dict=True,
	)
	if not meta:
		frappe.throw(_("{0} {1} does not exist.").format(label, account))
	if meta.company != company:
		frappe.throw(_("{0} {1} does not belong to company {2}.").format(label, account, company))
	if cint(meta.is_group):
		frappe.throw(_("{0} {1} cannot be a group account.").format(label, account))
	if cint(meta.disabled):
		frappe.throw(_("{0} {1} is disabled.").format(label, account))


def get_temporary_clearing_account(company: str) -> str:
	return get_consignment_settings(company).consignment_temporary_clearing_account


def get_valuation_difference_account(company: str) -> str:
	return get_consignment_settings(company).consignment_valuation_difference_account


def force_expense_account_on_items(doc, account: str) -> None:
	for row in doc.get("items") or []:
		row.expense_account = account


def validate_consignment_warehouse(warehouse: str, company: str) -> None:
	"""Validate warehouse usability and that a stock account is resolvable."""
	resolve_warehouse_account(warehouse, company)


def resolve_warehouse_account(warehouse: str, company: str) -> str:
	"""Resolve inventory account from Warehouse using standard ERPNext warehouse-account map.

	Reuses ``erpnext.stock.get_warehouse_account_map`` — the same utility
	``StockController.get_inventory_account_map`` uses when
	``Company.enable_item_wise_inventory_account`` is disabled.

	3.8.0 assumption: item-wise inventory accounts are not supported.
	"""
	from erpnext.stock import get_warehouse_account_map

	if not warehouse:
		frappe.throw(_("Warehouse is required to resolve the stock account."))

	wh = frappe.db.get_value(
		"Warehouse",
		warehouse,
		["name", "company", "is_group", "disabled"],
		as_dict=True,
	)
	if not wh:
		frappe.throw(_("Warehouse {0} does not exist.").format(warehouse))
	if wh.company != company:
		frappe.throw(_("Warehouse {0} does not belong to company {1}.").format(warehouse, company))
	if cint(wh.is_group):
		frappe.throw(_("Warehouse {0} cannot be a group warehouse.").format(warehouse))
	if cint(wh.disabled):
		frappe.throw(_("Warehouse {0} is disabled.").format(warehouse))

	# Rebuild map for this company so Warehouse.account changes are visible immediately
	frappe.flags.setdefault("warehouse_account_map", {}).pop(company, None)
	wh_map = get_warehouse_account_map(company)
	info = wh_map.get(warehouse) or {}
	account = info.get("account")
	if not account:
		frappe.throw(
			_(
				"No stock account could be resolved for Warehouse {0} and Company {1}. "
				"Set Account on the Warehouse or Default Inventory Account on the Company."
			).format(warehouse, company)
		)

	_validate_resolved_stock_account(account, company, warehouse)
	return account


def _validate_resolved_stock_account(account: str, company: str, warehouse: str) -> None:
	meta = frappe.db.get_value(
		"Account",
		account,
		["company", "is_group", "disabled", "account_type"],
		as_dict=True,
	)
	if not meta:
		frappe.throw(
			_("Resolved account {0} for Warehouse {1} does not exist.").format(account, warehouse)
		)
	if meta.company != company:
		frappe.throw(
			_(
				"Resolved account {0} for Warehouse {1} does not belong to company {2}."
			).format(account, warehouse, company)
		)
	if cint(meta.is_group):
		frappe.throw(
			_("Resolved account {0} for Warehouse {1} cannot be a group account.").format(
				account, warehouse
			)
		)
	if cint(meta.disabled):
		frappe.throw(
			_("Resolved account {0} for Warehouse {1} is disabled.").format(account, warehouse)
		)
	if meta.account_type != "Stock":
		frappe.throw(
			_(
				"Resolved account {0} for Warehouse {1} must be a Stock account "
				"(account_type must be Stock)."
			).format(account, warehouse)
		)


def validate_stock_entry_warehouses(doc) -> None:
	"""Ensure every source/target warehouse on a consignment Stock Entry is usable for stock GL."""
	warehouses = set()
	for row in doc.get("items") or []:
		if row.t_warehouse:
			warehouses.add(row.t_warehouse)
		if row.s_warehouse:
			warehouses.add(row.s_warehouse)

	if not warehouses:
		frappe.throw(_("Consignment Stock Entry must have a warehouse on every item row."))

	for wh in warehouses:
		resolve_warehouse_account(wh, doc.company)


def resolve_cost_center_from_stock_entry(stock_entry) -> str | None:
	"""Use a cost center only when all stock entry item rows agree on one.

	Mixed or missing cost centers → leave unset (standard ERPNext JE/account defaults apply).
	Does not read Consignment Stock Settings.
	"""
	ccs = {row.cost_center for row in (stock_entry.get("items") or []) if row.get("cost_center")}
	if len(ccs) == 1:
		return next(iter(ccs))
	return None


def resolve_finance_book_from_stock_entry(stock_entry) -> str | None:
	"""Copy finance book from source Stock Entry only when explicitly set."""
	return stock_entry.get("finance_book") or None


def get_stock_entry_detail_dimension_fields() -> list[str]:
	"""Return cost_center, project, and enabled Accounting Dimension fields on Stock Entry Detail."""
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_dimensions

	dimensions, _ = get_dimensions(with_cost_center_and_project=True)
	meta = frappe.get_meta("Stock Entry Detail")
	return [d["fieldname"] for d in dimensions if meta.has_field(d["fieldname"])]


def copy_accounting_dimensions_from_source_row(source_row, target_row: dict) -> dict:
	"""Copy accounting dimensions from a source Stock Entry Detail into a return row dict.

	Precedence:
	1. Keep any value already set on ``target_row``.
	2. Copy non-empty values from ``source_row``.
	3. Leave empty otherwise (standard ERPNext defaults may fill later).

	Does not hardcode Department or any single dimension.
	"""
	for fieldname in get_stock_entry_detail_dimension_fields():
		if target_row.get(fieldname) not in (None, ""):
			continue
		value = source_row.get(fieldname)
		if value not in (None, ""):
			target_row[fieldname] = value
	return target_row
