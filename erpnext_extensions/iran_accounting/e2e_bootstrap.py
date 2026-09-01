# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import erpnext
import frappe
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from frappe.utils import cint, flt, nowtime, random_string, today

from erpnext_extensions.iran_accounting.validation import fetch_gl_rows, fetch_sle_rows


def _make_item(item_code: str, properties: dict | None = None):
	if frappe.db.exists("Item", item_code):
		return frappe.get_doc("Item", item_code)
	item_group = "Products"
	if not frappe.db.exists("Item Group", item_group):
		item_group = frappe.db.get_value(
			"Item Group", {"is_group": 0}, "name", order_by="creation asc"
		) or frappe.db.get_value("Item Group", {}, "name")
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"description": item_code,
			"item_group": item_group,
		}
	)
	if properties:
		item.update(properties)
	item.insert(ignore_permissions=True)
	return item


def _create_stock_reconciliation(**args):
	args = frappe._dict(args)
	sr = frappe.new_doc("Stock Reconciliation")
	sr.purpose = args.purpose or "Stock Reconciliation"
	sr.posting_date = args.posting_date or today()
	sr.posting_time = args.posting_time or nowtime()
	sr.set_posting_time = 1
	sr.company = args.company
	if sr.purpose == "Opening Stock":
		sr.expense_account = args.expense_account or (
			frappe.get_cached_value("Company", sr.company, "temporary_opening_account")
			or frappe.get_cached_value(
				"Account",
				{"company": sr.company, "account_type": "Temporary", "is_group": 0},
				"name",
			)
		)
	else:
		sr.expense_account = args.expense_account or (
			frappe.get_cached_value("Company", sr.company, "stock_adjustment_account")
			or frappe.get_cached_value(
				"Account", {"account_type": "Stock Adjustment", "company": sr.company}, "name"
			)
		)
	sr.cost_center = args.cost_center or frappe.get_cached_value("Company", sr.company, "cost_center")
	if not sr.cost_center:
		sr.cost_center = frappe.db.get_value(
			"Cost Center", {"company": sr.company, "is_group": 0}, "name", order_by="creation asc"
		)
	if sr.purpose == "Opening Stock":
		sr.difference_account = sr.expense_account
	sr.append(
		"items",
		{
			"item_code": args.item_code,
			"warehouse": args.warehouse,
			"qty": args.qty,
			"valuation_rate": args.rate,
			"reconcile_all_serial_batch": 1,
		},
	)
	sr.insert(ignore_permissions=True)
	sr.submit()
	return sr


def get_irr_company(preferred: str | None = None) -> str:
	if preferred and frappe.db.exists("Company", preferred):
		cur = frappe.db.get_value("Company", preferred, "default_currency")
		if cur == "IRR":
			return preferred
	name = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name", order_by="creation asc")
	if not name:
		raise frappe.ValidationError("No IRR company on site")
	return name


def get_warehouse(company: str) -> str:
	wh = frappe.db.get_value(
		"Warehouse",
		{"company": company, "is_group": 0, "disabled": 0},
		"name",
		order_by="creation asc",
	)
	if not wh:
		raise frappe.ValidationError(f"No warehouse for company {company}")
	return wh


def get_second_warehouse(company: str, exclude: str) -> str:
	wh = frappe.db.get_value(
		"Warehouse",
		{"company": company, "is_group": 0, "disabled": 0, "name": ("!=", exclude)},
		"name",
		order_by="creation asc",
	)
	return wh or exclude


def ensure_test_item(company: str, prefix: str = "IRR-TEST", stock_uom: str | None = None) -> str:
	"""Create a stock item and return the persisted Item name.

	On sites with Item Auto Name rules, ``item_code`` may be rewritten at insert;
	callers must use the returned ``doc.name``, never the pre-insert code.
	"""
	item_code = f"{prefix}-{random_string(6)}"
	if frappe.db.exists("Item", item_code):
		return item_code
	props: dict = {"is_stock_item": 1}
	if stock_uom:
		props["stock_uom"] = stock_uom
	# Prefer an existing leaf Item Group when "Products" is absent (restore sites).
	if not frappe.db.exists("Item Group", "Products"):
		leaf = frappe.db.get_value(
			"Item Group", {"is_group": 0}, "name", order_by="creation asc"
		)
		if leaf:
			props["item_group"] = leaf
	doc = _make_item(item_code, props)
	return doc.name


def apply_stock_entry_site_defaults(doc) -> None:
	"""Fill restore-site mandatory Stock Entry fields when gate/test builders omit them.

	Enabled only when ``frappe.flags.iran_gate_defaults`` is set so interactive Desk
	users still see real mandatory validation.
	"""
	if not getattr(frappe.flags, "iran_gate_defaults", False):
		return
	if not doc or doc.doctype != "Stock Entry":
		return
	meta = frappe.get_meta("Stock Entry")
	row_meta = frappe.get_meta("Stock Entry Detail")
	if meta.has_field("custom_rahkaran_no") and not doc.get("custom_rahkaran_no"):
		doc.custom_rahkaran_no = f"GATE-{random_string(8)}"
	if meta.has_field("department") and not doc.get("department"):
		dept = frappe.db.get_value("Department", {"company": doc.company}, "name")
		if dept:
			doc.department = dept
	default_cc = frappe.get_cached_value("Company", doc.company, "cost_center")
	if not default_cc:
		default_cc = frappe.db.get_value(
			"Cost Center", {"company": doc.company, "is_group": 0}, "name", order_by="creation asc"
		)
	for row in doc.get("items") or []:
		if row_meta.has_field("department") and not row.get("department"):
			row.department = doc.get("department")
		if row_meta.has_field("cost_center") and not row.get("cost_center") and default_cc:
			row.cost_center = default_cc
		if row_meta.has_field("expense_account") and not row.get("expense_account"):
			# leave blank unless Material Issue / Manufacture needs it — ERPNext often fills
			pass
	if meta.has_field("cost_center") and not doc.get("cost_center") and default_cc:
		doc.cost_center = default_cc



def fractional_uom() -> str | None:
	return frappe.db.get_value(
		"UOM", {"must_be_whole_number": 0, "enabled": 1}, "name", order_by="creation asc"
	)


def submit_stock_reconciliation_adjustment(
	company: str,
	item_code: str,
	qty: float,
	rate: float,
	warehouse: str | None = None,
):
	warehouse = warehouse or get_warehouse(company)
	return _create_stock_reconciliation(
		item_code=item_code,
		warehouse=warehouse,
		qty=qty,
		rate=rate,
		posting_date=today(),
		posting_time=nowtime(),
		purpose="Stock Reconciliation",
		company=company,
	)


def submit_material_receipt(
	company: str,
	item_code: str,
	qty: float,
	rate: float,
	warehouse: str | None = None,
):
	warehouse = warehouse or get_warehouse(company)
	se = make_stock_entry(
		item_code=item_code,
		qty=qty,
		rate=rate,
		target=warehouse,
		company=company,
		purpose="Material Receipt",
	)
	return se


def submit_opening_stock_reconciliation(
	company: str,
	item_code: str,
	qty: float,
	rate: float,
	warehouse: str | None = None,
):
	warehouse = warehouse or get_warehouse(company)
	sr = _create_stock_reconciliation(
		item_code=item_code,
		warehouse=warehouse,
		qty=qty,
		rate=rate,
		posting_date=today(),
		posting_time=nowtime(),
		purpose="Opening Stock",
		company=company,
	)
	return sr


def submit_material_transfer(
	company: str,
	item_code: str,
	qty: float,
	from_wh: str,
	to_wh: str,
):
	se = make_stock_entry(
		item_code=item_code,
		qty=qty,
		source=from_wh,
		target=to_wh,
		company=company,
		purpose="Material Transfer",
	)
	return se


def preview_stock_entry_gl(company: str, stock_entry_name: str) -> dict:
	from erpnext.controllers.stock_controller import show_accounting_ledger_preview

	return show_accounting_ledger_preview(company, "Stock Entry", stock_entry_name)


def preview_gl_totals(preview: dict) -> tuple[float, float]:
	debit = credit = 0.0
	idx_debit = idx_credit = None
	for i, col in enumerate(preview.get("gl_columns") or []):
		label = (col.get("name") or "").lower()
		if label == "debit":
			idx_debit = i
		if label == "credit":
			idx_credit = i
	for row in preview.get("gl_data") or []:
		if idx_debit is not None and len(row) > idx_debit:
			debit += flt(row[idx_debit])
		if idx_credit is not None and len(row) > idx_credit:
			credit += flt(row[idx_credit])
	return debit, credit


def voucher_ledger_snapshot(voucher_type: str, voucher_no: str):
	return fetch_gl_rows(voucher_type, voucher_no), fetch_sle_rows(voucher_type, voucher_no)


def enable_perpetual_inventory(company: str) -> None:
	if not cint(erpnext.is_perpetual_inventory_enabled(company)):
		frappe.db.set_value("Company", company, "enable_perpetual_inventory", 1)


def _company_abbr(company: str) -> str:
	return frappe.get_cached_value("Company", company, "abbr") or company[:2].upper()


def _ensure_currency(code: str) -> None:
	if frappe.db.exists("Currency", code):
		return
	frappe.get_doc({"doctype": "Currency", "currency_name": code, "enabled": 1}).insert(
		ignore_permissions=True
	)


def _ensure_child_account(
	company: str,
	parent: str,
	account_name: str,
	currency: str,
	account_type: str | None = None,
) -> str:
	abbr = _company_abbr(company)
	full_name = f"{account_name} - {abbr}"
	if frappe.db.exists("Account", full_name):
		return full_name
	acc = frappe.new_doc("Account")
	acc.account_name = account_name
	acc.company = company
	acc.parent_account = parent
	acc.account_currency = currency
	acc.is_group = 0
	if account_type:
		acc.account_type = account_type
	acc.insert(ignore_permissions=True)
	return acc.name


def ensure_foreign_currency_acceptance_masters(company: str) -> dict:
	"""Supplier/customer + USD/EUR payable/receivable accounts for acceptance 31–38."""
	for cur in ("USD", "EUR"):
		_ensure_currency(cur)

	payable_parent = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Payable", "is_group": 1}, "name"
	) or frappe.db.get_value(
		"Account", {"company": company, "name": ("like", "%Accounts Payable%"), "is_group": 1}, "name"
	)
	recv_parent = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Receivable", "is_group": 1}, "name"
	) or frappe.db.get_value(
		"Account", {"company": company, "name": ("like", "%Accounts Receivable%"), "is_group": 1}, "name"
	)
	if not payable_parent or not recv_parent:
		raise frappe.ValidationError(
			"Payable/Receivable group accounts missing for foreign currency bootstrap"
		)

	accounts = {
		"USD": _ensure_child_account(company, payable_parent, "IA USD Creditors", "USD", "Payable"),
		"EUR": _ensure_child_account(company, payable_parent, "IA EUR Creditors", "EUR", "Payable"),
		"USD_RECV": _ensure_child_account(company, recv_parent, "IA USD Debtors", "USD", "Receivable"),
		"EUR_RECV": _ensure_child_account(company, recv_parent, "IA EUR Debtors", "EUR", "Receivable"),
	}

	def _ensure_supplier(supplier_name: str, account: str) -> str:
		acct_cur = frappe.db.get_value("Account", account, "account_currency")
		if not frappe.db.exists("Supplier", supplier_name):
			sg = frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups"
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": supplier_name,
					"supplier_group": sg,
					"default_currency": acct_cur,
				}
			).insert(ignore_permissions=True)
		supplier = frappe.get_doc("Supplier", supplier_name)
		if acct_cur:
			supplier.default_currency = acct_cur
		if not any(r.company == company and r.account == account for r in supplier.accounts or []):
			supplier.set("accounts", [{"company": company, "account": account}])
			supplier.save(ignore_permissions=True)
		return supplier_name

	def _ensure_customer(customer_name: str, account: str) -> str:
		acct_cur = frappe.db.get_value("Account", account, "account_currency")
		if not frappe.db.exists("Customer", customer_name):
			cg = frappe.db.get_value("Customer Group", {}, "name") or "All Customer Groups"
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": customer_name,
					"customer_group": cg,
					"default_currency": acct_cur,
				}
			).insert(ignore_permissions=True)
		customer = frappe.get_doc("Customer", customer_name)
		if acct_cur:
			customer.default_currency = acct_cur
		if not any(r.company == company and r.account == account for r in customer.accounts or []):
			customer.set("accounts", [{"company": company, "account": account}])
			customer.save(ignore_permissions=True)
		return customer_name

	suppliers = {
		"USD": _ensure_supplier("IA-FC-ACC-SUP-USD", accounts["USD"]),
		"EUR": _ensure_supplier("IA-FC-ACC-SUP-EUR", accounts["EUR"]),
	}
	customers = {
		"USD": _ensure_customer("IA-FC-ACC-CUS-USD", accounts["USD_RECV"]),
		"EUR": _ensure_customer("IA-FC-ACC-CUS-EUR", accounts["EUR_RECV"]),
	}

	frappe.db.commit()
	return {
		"suppliers": suppliers,
		"customers": customers,
		"accounts": accounts,
	}
