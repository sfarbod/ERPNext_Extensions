# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe.utils import flt, random_string, today
from frappe.utils import cint
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

from erpnext_extensions.consignment_stock.material_loan.constants import (
	F_IS_LOAN_ISSUE,
	F_IS_LOAN_RETURN,
	F_ISSUE_DETAIL,
	F_ISSUE_SE,
	F_PARTY,
	F_PARTY_TYPE,
)
from erpnext_extensions.consignment_stock.material_loan.custom_fields import ensure_custom_fields
from erpnext_extensions.consignment_stock.tests.helpers import (
	_get_or_create_account,
	_pick_parent,
	ensure_consignment_warehouse,
	ensure_customer,
	ensure_module_ready,
	ensure_settings,
	ensure_supplier,
	get_irr_company,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import enable_perpetual_inventory, ensure_test_item


def ensure_material_loan_ready():
	ensure_module_ready()
	ensure_custom_fields()


def ensure_material_loan_accounts(company: str) -> dict:
	asset_parent = _pick_parent(company, "Asset")
	expense_parent = _pick_parent(company, "Expense")
	liability_parent = _pick_parent(company, "Liability")

	inventory = _get_or_create_account(
		company,
		"Material Loan Inventory Test",
		parent_account=asset_parent,
		account_type="Stock",
		root_type="Asset",
	)
	temp = _get_or_create_account(
		company,
		"Material Loan Temp Clearing Test",
		parent_account=asset_parent,
		account_type=None,
		root_type="Asset",
	)
	diff = _get_or_create_account(
		company,
		"Material Loan Valuation Diff Test",
		parent_account=expense_parent,
		account_type="Expense Account",
		root_type="Expense",
		report_type="Profit and Loss",
	)
	cust_recv = _get_or_create_account(
		company,
		"Material Loan Receivable Customer Test",
		parent_account=asset_parent,
		account_type="Receivable",
		root_type="Asset",
	)
	sup_pay = _get_or_create_account(
		company,
		"Material Loan with Suppliers Test",
		parent_account=liability_parent,
		account_type="Payable",
		root_type="Liability",
	)
	return {
		"inventory": inventory,
		"temporary": temp,
		"difference": diff,
		"customer_receivable": cust_recv,
		"supplier_payable": sup_pay,
	}


def receive_stock(*, company: str, warehouse: str, item_code: str, qty: float, rate: float):
	"""Receive stock for Material Loan tests.

	Builds a Material Receipt Stock Entry explicitly so mandatory Accounting
	Dimensions (e.g. Department) can be set before insert.
	"""
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.purpose = "Material Receipt"
	se.stock_entry_type = "Material Receipt"
	se.posting_date = today()
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
	dept_df = frappe.get_meta("Stock Entry Detail").get_field("department")
	if dept_df and cint(dept_df.reqd):
		dept = frappe.db.get_value("Department", {"company": company}, "name")
		if dept:
			row["department"] = dept
	se.append("items", row)
	se.insert()
	se.submit()
	return se


def ensure_material_loan_settings(company: str):
	# Keep inbound settings valid
	ensure_settings(company)
	accounts = ensure_material_loan_accounts(company)
	wh = ensure_consignment_warehouse(company, accounts["inventory"])
	name = frappe.db.get_value("Consignment Stock Settings", {"company": company}, "name")
	doc = frappe.get_doc("Consignment Stock Settings", name)
	doc.material_loan_temporary_clearing_account = accounts["temporary"]
	doc.material_loan_valuation_difference_account = accounts["difference"]
	doc.default_material_loan_source_warehouse = wh
	doc.default_material_loan_return_warehouse = wh
	doc.require_expected_return_date = 0
	doc.allow_return_to_different_warehouse = 1
	doc.set("material_loan_party_accounts", [])
	doc.append(
		"material_loan_party_accounts",
		{"party_type": "Customer", "account": accounts["customer_receivable"]},
	)
	doc.append(
		"material_loan_party_accounts",
		{"party_type": "Supplier", "account": accounts["supplier_payable"]},
	)
	doc.save(ignore_permissions=True)
	return name, accounts, wh


def ensure_material_loan_stock_entry_types() -> dict:
	ensure_custom_fields()
	issue = frappe.db.get_value(
		"Stock Entry Type", {"purpose": "Material Issue", F_IS_LOAN_ISSUE: 1}, "name"
	)
	if not issue:
		name = "Material Loan Issue"
		if frappe.db.exists("Stock Entry Type", name):
			doc = frappe.get_doc("Stock Entry Type", name)
		else:
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = name
		doc.purpose = "Material Issue"
		doc.set(F_IS_LOAN_ISSUE, 1)
		doc.set(F_IS_LOAN_RETURN, 0)
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
		issue = doc.name

	ret = frappe.db.get_value(
		"Stock Entry Type", {"purpose": "Material Receipt", F_IS_LOAN_RETURN: 1}, "name"
	)
	if not ret:
		name = "Material Loan Return"
		if frappe.db.exists("Stock Entry Type", name):
			doc = frappe.get_doc("Stock Entry Type", name)
		else:
			doc = frappe.new_doc("Stock Entry Type")
			doc.name = name
		doc.purpose = "Material Receipt"
		doc.set(F_IS_LOAN_RETURN, 1)
		doc.set(F_IS_LOAN_ISSUE, 0)
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
		ret = doc.name
	return {"issue": issue, "return": ret}


def make_material_loan_issue(
	*,
	company: str,
	warehouse: str,
	item_code: str,
	qty: float,
	party_type: str,
	party: str,
	stock_entry_type: str,
	submit: bool = True,
	batch_no: str | None = None,
	serial_no: str | None = None,
	expected_return_date=None,
	item_dimensions: dict | None = None,
):
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = stock_entry_type
	se.purpose = "Material Issue"
	se.posting_date = today()
	se.set(F_IS_LOAN_ISSUE, 1)
	se.set(F_IS_LOAN_RETURN, 0)
	se.set(F_PARTY_TYPE, party_type)
	se.set(F_PARTY, party)
	if expected_return_date is not None:
		from erpnext_extensions.consignment_stock.material_loan.constants import F_EXPECTED_RETURN_DATE

		se.set(F_EXPECTED_RETURN_DATE, expected_return_date)
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"s_warehouse": warehouse,
		"conversion_factor": 1,
		"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
	}
	if batch_no or serial_no:
		row["use_serial_batch_fields"] = 1
	if batch_no:
		row["batch_no"] = batch_no
	if serial_no:
		row["serial_no"] = serial_no
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


def make_material_loan_return(
	*,
	company: str,
	warehouse: str,
	item_code: str,
	qty: float,
	party_type: str,
	party: str,
	stock_entry_type: str,
	issue_name: str,
	issue_detail: str,
	submit: bool = True,
	batch_no: str | None = None,
	serial_no: str | None = None,
):
	from erpnext_extensions.consignment_stock.accounting import (
		copy_accounting_dimensions_from_source_row,
	)

	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = stock_entry_type
	se.purpose = "Material Receipt"
	se.posting_date = today()
	se.set(F_IS_LOAN_ISSUE, 0)
	se.set(F_IS_LOAN_RETURN, 1)
	se.set(F_PARTY_TYPE, party_type)
	se.set(F_PARTY, party)
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"t_warehouse": warehouse,
		"conversion_factor": 1,
		"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		F_ISSUE_SE: issue_name,
		F_ISSUE_DETAIL: issue_detail,
	}
	if batch_no or serial_no:
		row["use_serial_batch_fields"] = 1
	if batch_no:
		row["batch_no"] = batch_no
	if serial_no:
		row["serial_no"] = serial_no
	if issue_detail and frappe.db.exists("Stock Entry Detail", issue_detail):
		copy_accounting_dimensions_from_source_row(
			frappe.get_doc("Stock Entry Detail", issue_detail), row
		)
	se.append("items", row)
	se.insert()
	if submit:
		se.submit()
	return se


def ensure_batch_item(prefix: str = "ML-BATCH") -> str:
	from erpnext_extensions.iran_accounting.e2e_bootstrap import _make_item

	item_code = f"{prefix}-{random_string(5)}"
	doc = _make_item(
		item_code,
		{
			"is_stock_item": 1,
			"has_batch_no": 1,
			"create_new_batch": 1,
			"batch_number_series": "MLB.#####",
		},
	)
	frappe.db.commit()
	# Site may autoname Item differently from item_code; Stock Entry links by name.
	return doc.name


def ensure_serial_item(prefix: str = "ML-SER") -> str:
	from erpnext_extensions.iran_accounting.e2e_bootstrap import _make_item

	item_code = f"{prefix}-{random_string(5)}"
	doc = _make_item(
		item_code,
		{
			"is_stock_item": 1,
			"has_serial_no": 1,
			"serial_no_series": "MLS.#####",
		},
	)
	frappe.db.commit()
	return doc.name


def enable_serial_batch_fields():
	frappe.db.set_single_value("Stock Settings", "use_serial_batch_fields", 1)
	if frappe.get_meta("Stock Settings").has_field("enable_serial_and_batch_no_for_item"):
		frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
	frappe.clear_cache(doctype="Stock Settings")


def make_batch(item_code: str, batch_id: str | None = None) -> str:
	batch_id = batch_id or f"MLB-{random_string(6)}"
	if frappe.db.exists("Batch", batch_id):
		return batch_id
	doc = frappe.get_doc(
		{
			"doctype": "Batch",
			"batch_id": batch_id,
			"item": item_code,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def receive_stock_with_batch_or_serial(
	*,
	company: str,
	warehouse: str,
	item_code: str,
	qty: float,
	rate: float,
	batch_no: str | None = None,
	serial_no: str | None = None,
):
	"""Material Receipt via Stock Entry with optional batch/serial fields."""
	from erpnext_extensions.consignment_stock.constants import F_IS_RECEIPT
	from erpnext_extensions.consignment_stock.material_loan.constants import F_IS_LOAN_RETURN

	# Prefer a non-loan, non-consignment Material Receipt type
	types = frappe.get_all(
		"Stock Entry Type",
		filters={"purpose": "Material Receipt"},
		fields=["name", F_IS_LOAN_RETURN, F_IS_RECEIPT],
	)
	stock_entry_type = None
	for t in types:
		if cint(t.get(F_IS_LOAN_RETURN)) or cint(t.get(F_IS_RECEIPT)):
			continue
		stock_entry_type = t.name
		break
	if not stock_entry_type:
		# Create a plain Material Receipt type for tests
		name = "Material Receipt"
		if frappe.db.exists("Stock Entry Type", name):
			stock_entry_type = name
		else:
			doc = frappe.get_doc({"doctype": "Stock Entry Type", "name": name, "purpose": "Material Receipt"})
			doc.insert(ignore_permissions=True)
			stock_entry_type = doc.name

	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.purpose = "Material Receipt"
	se.stock_entry_type = stock_entry_type
	se.posting_date = today()
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"t_warehouse": warehouse,
		"basic_rate": rate,
		"conversion_factor": 1,
		"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"set_basic_rate_manually": 1,
	}
	if batch_no or serial_no:
		row["use_serial_batch_fields"] = 1
	if batch_no:
		row["batch_no"] = batch_no
	if serial_no:
		row["serial_no"] = serial_no
	dept_df = frappe.get_meta("Stock Entry Detail").get_field("department")
	if dept_df and cint(dept_df.reqd) and not row.get("department"):
		dept = frappe.db.get_value("Department", {"company": company}, "name")
		if dept:
			row["department"] = dept
	se.append("items", row)
	se.insert()
	se.submit()
	return se


def create_test_user(email: str, roles: list[str]) -> str:
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		user.insert(ignore_permissions=True)
	for role in roles:
		if not frappe.db.exists("Has Role", {"parent": email, "role": role}):
			user.add_roles(role)
	return email


def gl_balance(account: str, company: str) -> float:
	rows = frappe.get_all(
		"GL Entry",
		filters={"account": account, "company": company, "is_cancelled": 0},
		fields=["debit", "credit"],
	)
	return flt(sum(flt(r.debit) - flt(r.credit) for r in rows))


def party_gl_balance(account: str, party_type: str, party: str, company: str) -> float:
	rows = frappe.get_all(
		"GL Entry",
		filters={
			"account": account,
			"party_type": party_type,
			"party": party,
			"company": company,
			"is_cancelled": 0,
		},
		fields=["debit", "credit"],
	)
	return flt(sum(flt(r.debit) - flt(r.credit) for r in rows))
