# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Isolated fixtures for Asset Usage Replan tests.

Creates a dedicated company, chart of accounts, cost center, location,
asset category, item, and finance books so the suite does not depend on
`_Test Company` / `Macbook Pro` / `Computers` or other site masters.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.asset_usage_depreciation.custom_fields import ensure_custom_fields
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

COMPANY = "_AUD Replan Company"
ABBR = "_ARQ"
CATEGORY = "_AUD Replan Category"
ITEM_CODE = "_AUD Replan Item"
LOCATION = "_AUD Replan Location"
FINANCE_BOOK_SL = "default"
FINANCE_BOOK_WDV = "AUD-WDV-FB"

_CACHE: dict | None = None


def ensure_replan_fixtures() -> dict:
	"""Idempotent. Safe to call from every TestCase.setUpClass."""
	global _CACHE
	if _CACHE and frappe.db.exists("Company", _CACHE["company"]) and frappe.db.exists("Item", _CACHE["item_code"]):
		return _CACHE

	ensure_custom_fields()
	company = _ensure_company()
	_ensure_fiscal_year_membership(company)
	location = h.ensure_location(LOCATION)
	cost_center = _ensure_cost_center(company)
	_ensure_finance_book(FINANCE_BOOK_SL)
	_ensure_finance_book(FINANCE_BOOK_WDV)
	accounts = _asset_accounts(company)
	category = _ensure_category(company, accounts)
	item_code = _ensure_item(category)

	_CACHE = {
		"company": company,
		"item_code": item_code,
		"asset_category": category,
		"location": location,
		"cost_center": cost_center,
		"finance_book": FINANCE_BOOK_SL,
		"finance_book_wdv": FINANCE_BOOK_WDV,
		"accounts": accounts,
	}
	return _CACHE


def _ensure_company() -> str:
	if frappe.db.exists("Company", COMPANY):
		return COMPANY
	doc = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": COMPANY,
			"abbr": ABBR,
			"default_currency": "USD",
			"country": "United States",
			"valuation_method": "FIFO",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
			"enable_perpetual_inventory": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _ensure_fiscal_year_membership(company: str) -> None:
	"""Add `company` to existing Fiscal Years without rewriting the child table.

	Loading Fiscal Year and calling save() can drop other companies from
	`tabFiscal Year Company`. Insert child rows only.
	"""
	for fy_name in frappe.get_all("Fiscal Year", pluck="name"):
		if frappe.db.exists("Fiscal Year Company", {"parent": fy_name, "company": company}):
			continue
		frappe.get_doc(
			{
				"doctype": "Fiscal Year Company",
				"parent": fy_name,
				"parenttype": "Fiscal Year",
				"parentfield": "companies",
				"company": company,
			}
		).insert(ignore_permissions=True)


def _ensure_cost_center(company: str) -> str:
	name = h.company_cost_center(company)
	if name:
		if not frappe.db.get_value("Company", company, "cost_center"):
			frappe.db.set_value("Company", company, "cost_center", name)
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Cost Center",
			"cost_center_name": "Main",
			"company": company,
			"is_group": 0,
			"parent_cost_center": frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name"),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Company", company, "cost_center", doc.name)
	return doc.name


def _ensure_finance_book(name: str) -> str:
	if not frappe.db.exists("Finance Book", name):
		frappe.get_doc({"doctype": "Finance Book", "finance_book_name": name}).insert(
			ignore_permissions=True
		)
	return name


def _account(company: str, *, account_type: str | None = None, account_name: str | None = None) -> str | None:
	filters: dict = {"company": company, "is_group": 0}
	if account_type:
		filters["account_type"] = account_type
	if account_name:
		filters["account_name"] = account_name
	return frappe.db.get_value("Account", filters, "name")


def _asset_accounts(company: str) -> dict:
	fixed = _account(company, account_type="Fixed Asset")
	accum = _account(company, account_type="Accumulated Depreciation")
	expense = _account(company, account_type="Depreciation") or _account(company, account_name="Depreciation")
	if not expense:
		expense = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
		)
	if not (fixed and accum and expense):
		frappe.throw(
			f"Replan fixtures: missing asset accounts for {company} "
			f"(fixed={fixed}, accum={accum}, expense={expense})"
		)
	return {
		"fixed_asset_account": fixed,
		"accumulated_depreciation_account": accum,
		"depreciation_expense_account": expense,
	}


def _ensure_category(company: str, accounts: dict) -> str:
	if frappe.db.exists("Asset Category", CATEGORY):
		cat = frappe.get_doc("Asset Category", CATEGORY)
		has_company = any(row.company_name == company for row in (cat.accounts or []))
		if not has_company:
			cat.append(
				"accounts",
				{
					"company_name": company,
					**accounts,
				},
			)
			cat.save(ignore_permissions=True)
		return CATEGORY
	cat = frappe.get_doc(
		{
			"doctype": "Asset Category",
			"asset_category_name": CATEGORY,
			"enable_cwip_accounting": 0,
			"accounts": [{"company_name": company, **accounts}],
		}
	)
	cat.insert(ignore_permissions=True)
	return cat.name


def _ensure_item(category: str) -> str:
	existing = frappe.db.get_value("Item", {"item_code": ITEM_CODE}, "name")
	if existing:
		return existing
	item_group = (
		"All Item Groups"
		if frappe.db.exists("Item Group", "All Item Groups")
		else (frappe.get_all("Item Group", pluck="name", limit=1) or [None])[0]
	)
	stock_uom = "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", pluck="name", limit=1)[0]
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": ITEM_CODE,
			"item_name": ITEM_CODE,
			"item_group": item_group,
			"stock_uom": stock_uom,
			"is_stock_item": 0,
			"is_fixed_asset": 1,
			"is_grouped_asset": 0,
			"auto_create_assets": 0,
			"asset_category": category,
		}
	)
	item.flags.ignore_permissions = True
	item.insert(ignore_permissions=True)
	return item.name
