# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe.utils import cint, flt, random_string, today

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
)
from erpnext_extensions.consignment_stock.constants import (
	F_HAS_RECEIPT_REF,
	F_IS_RECEIPT,
	F_IS_RETURN,
	F_PARTY,
	F_PARTY_TYPE,
	F_RECEIPT_DETAIL,
	F_RECEIPT_REF,
	F_RECEIPT_SE,
)
from erpnext_extensions.consignment_stock.custom_fields import ensure_custom_fields


def ensure_module_ready():
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply

	apply()
	ensure_custom_fields()
	frappe.set_user("Administrator")


def _get_or_create_account(
	company: str,
	account_name: str,
	*,
	parent_account: str,
	account_type: str | None = None,
	root_type: str = "Asset",
	report_type: str = "Balance Sheet",
) -> str:
	existing = frappe.db.get_value(
		"Account",
		{"account_name": account_name, "company": company},
		"name",
	)
	if existing:
		return existing

	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"company": company,
			"parent_account": parent_account,
			"is_group": 0,
			"root_type": root_type,
			"report_type": report_type,
			"account_type": account_type,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _pick_parent(company: str, root_type: str) -> str:
	parent = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": root_type},
		"name",
		order_by="lft asc",
	)
	if not parent:
		frappe.throw(f"No {root_type} parent account for {company}")
	return parent


def ensure_consignment_accounts(company: str) -> dict:
	asset_parent = _pick_parent(company, "Asset")
	liability_parent = _pick_parent(company, "Liability")
	expense_parent = _pick_parent(company, "Expense")

	inventory = _get_or_create_account(
		company,
		"Consignment Inventory Test",
		parent_account=asset_parent,
		account_type="Stock",
		root_type="Asset",
	)
	temp = _get_or_create_account(
		company,
		"Consignment Temp Clearing Test",
		parent_account=liability_parent,
		account_type=None,
		root_type="Liability",
	)
	diff = _get_or_create_account(
		company,
		"Consignment Valuation Diff Test",
		parent_account=expense_parent,
		account_type="Expense Account",
		root_type="Expense",
		report_type="Profit and Loss",
	)
	return {"inventory": inventory, "temporary": temp, "difference": diff}


def ensure_consignment_warehouse(company: str, inventory_account: str) -> str:
	label = f"Consignment WH - {company}"
	existing = frappe.db.get_value(
		"Warehouse",
		{"warehouse_name": label, "company": company},
		"name",
	)
	if existing:
		frappe.db.set_value("Warehouse", existing, "account", inventory_account)
		return existing

	# Also match autoname pattern "Label - ABBR"
	existing = frappe.db.get_value(
		"Warehouse",
		{"name": ("like", f"Consignment WH - {company}%"), "company": company},
		"name",
	)
	if existing:
		frappe.db.set_value("Warehouse", existing, "account", inventory_account)
		return existing

	parent = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 1}, "name", order_by="creation asc"
	)
	doc = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": label,
			"company": company,
			"parent_warehouse": parent,
			"account": inventory_account,
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Warehouse", doc.name, "account", inventory_account)
	return doc.name


def ensure_settings(company: str) -> str:
	accounts = ensure_consignment_accounts(company)
	wh = ensure_consignment_warehouse(company, accounts["inventory"])
	name = frappe.db.get_value("Consignment Stock Settings", {"company": company}, "name")
	if name:
		doc = frappe.get_doc("Consignment Stock Settings", name)
	else:
		doc = frappe.new_doc("Consignment Stock Settings")
		doc.company = company

	doc.consignment_temporary_clearing_account = accounts["temporary"]
	doc.consignment_valuation_difference_account = accounts["difference"]
	doc.default_consignment_warehouse = wh
	doc.allow_zero_receipt_rate = 0
	doc.save(ignore_permissions=True)
	return doc.name


def resolve_warehouse_inventory_account(warehouse: str, company: str) -> str:
	from erpnext_extensions.consignment_stock.accounting import resolve_warehouse_account

	return resolve_warehouse_account(warehouse, company)


def ensure_stock_entry_types() -> dict:
	ensure_custom_fields()
	receipt = frappe.db.get_value(
		"Stock Entry Type", {"purpose": "Material Receipt", F_IS_RECEIPT: 1}, "name"
	)
	if not receipt:
		# Prefer renaming/creating dedicated type
		name = "Consignment Receipt"
		if frappe.db.exists("Stock Entry Type", name):
			doc = frappe.get_doc("Stock Entry Type", name)
		else:
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = name
			doc.purpose = "Material Receipt"
		doc.purpose = "Material Receipt"
		doc.set(F_IS_RECEIPT, 1)
		doc.set(F_IS_RETURN, 0)
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
		receipt = doc.name

	ret = frappe.db.get_value(
		"Stock Entry Type", {"purpose": "Material Issue", F_IS_RETURN: 1}, "name"
	)
	if not ret:
		name = "Consignment Return"
		if frappe.db.exists("Stock Entry Type", name):
			doc = frappe.get_doc("Stock Entry Type", name)
		else:
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = name
			doc.purpose = "Material Issue"
		doc.purpose = "Material Issue"
		doc.set(F_IS_RETURN, 1)
		doc.set(F_IS_RECEIPT, 0)
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
		ret = doc.name

	return {"receipt": receipt, "return": ret}


def ensure_supplier(company: str) -> str:
	supplier = f"CS-SUP-{random_string(10)}"
	sg = frappe.db.get_value("Supplier Group", {}, "name", order_by="creation asc") or "All Supplier Groups"
	doc = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": supplier,
			"supplier_group": sg,
		}
	)
	from erpnext.accounts.party import get_party_account

	# Site may enforce "similar supplier" duplicate checks.
	doc.flags.ignore_permissions = True
	try:
		doc.insert(ignore_permissions=True)
	except frappe.ValidationError:
		# Fall back to any existing supplier when duplicate prevention blocks create.
		existing = frappe.db.get_value("Supplier", {"disabled": 0}, "name", order_by="modified desc")
		if existing:
			return existing
		raise
	acc = get_party_account("Supplier", doc.name, company)
	if not acc:
		payable = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Payable", "is_group": 0},
			"name",
		)
		doc.append("accounts", {"company": company, "account": payable})
		doc.save(ignore_permissions=True)
	return doc.name


def ensure_customer(company: str) -> str:
	cg = frappe.db.get_value(
		"Customer Group", {"is_group": 0}, "name", order_by="creation asc"
	) or frappe.db.get_value("Customer Group", {}, "name", order_by="lft desc")
	territory = frappe.db.get_value(
		"Territory", {"is_group": 0}, "name", order_by="creation asc"
	) or frappe.db.get_value("Territory", {}, "name", order_by="lft desc")
	payload = {
		"doctype": "Customer",
		"customer_name": f"CS-CUST-{random_string(10)}",
		"customer_group": cg,
		"territory": territory,
	}
	if frappe.get_meta("Customer").has_field("tax_id"):
		tax_df = frappe.get_meta("Customer").get_field("tax_id")
		if tax_df and (cint(tax_df.reqd) or True):
			payload["tax_id"] = f"EE-{random_string(10)}"
	doc = frappe.get_doc(payload)
	from erpnext.accounts.party import get_party_account

	doc.insert(ignore_permissions=True)
	acc = get_party_account("Customer", doc.name, company)
	if not acc:
		recv = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Receivable", "is_group": 0},
			"name",
		)
		doc.append("accounts", {"company": company, "account": recv})
		doc.save(ignore_permissions=True)
	return doc.name


def make_consignment_receipt(
	*,
	company: str,
	warehouse: str,
	item_code: str,
	qty: float,
	rate: float,
	party_type: str,
	party: str,
	stock_entry_type: str,
	submit: bool = True,
	item_dimensions: dict | None = None,
):
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = stock_entry_type
	se.purpose = "Material Receipt"
	se.posting_date = today()
	se.set(F_IS_RECEIPT, 1)
	se.set(F_IS_RETURN, 0)
	se.set(F_PARTY_TYPE, party_type)
	se.set(F_PARTY, party)
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"basic_rate": rate,
		"t_warehouse": warehouse,
		"conversion_factor": 1,
		"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"set_basic_rate_manually": 1,
	}
	if item_dimensions:
		row.update(item_dimensions)
	dept_df = frappe.get_meta("Stock Entry Detail").get_field("department")
	if dept_df and cint(dept_df.reqd) and not row.get("department"):
		dept = frappe.db.get_value("Department", {"company": company}, "name")
		if dept:
			row["department"] = dept
	se.append("items", row)
	se.insert()
	if submit:
		se.submit()
	return se


def make_consignment_return(
	*,
	company: str,
	warehouse: str,
	item_code: str,
	qty: float,
	party_type: str,
	party: str,
	stock_entry_type: str,
	receipt_name: str,
	receipt_detail: str,
	submit: bool = True,
):
	from erpnext_extensions.consignment_stock.accounting import (
		copy_accounting_dimensions_from_source_row,
	)

	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = stock_entry_type
	se.purpose = "Material Issue"
	se.posting_date = today()
	se.set(F_IS_RECEIPT, 0)
	se.set(F_IS_RETURN, 1)
	se.set(F_PARTY_TYPE, party_type)
	se.set(F_PARTY, party)
	se.set(F_HAS_RECEIPT_REF, 1)
	se.set(F_RECEIPT_REF, receipt_name)
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"s_warehouse": warehouse,
		"conversion_factor": 1,
		"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		F_RECEIPT_SE: receipt_name,
		F_RECEIPT_DETAIL: receipt_detail,
	}
	if receipt_detail and frappe.db.exists("Stock Entry Detail", receipt_detail):
		copy_accounting_dimensions_from_source_row(
			frappe.get_doc("Stock Entry Detail", receipt_detail), row
		)
	se.append("items", row)
	se.insert()
	if submit:
		se.submit()
	return se


def gl_rows_for(voucher_type: str, voucher_no: str) -> list[dict]:
	return frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["account", "debit", "credit", "party_type", "party"],
	)
