# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only accounting readiness checks for Delivery Note Check Submission."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.stock_extensions.delivery_note_check_submission.errors import make_error


def collect_accounting_errors(doc) -> tuple[list[dict], list[dict]]:
	"""Return (errors, limitations) for account readiness without creating GLE."""
	errors: list[dict] = []
	limitations: list[dict] = []

	if not cint(frappe.get_cached_value("Company", doc.company, "enable_perpetual_inventory")):
		return errors, limitations

	for row in doc.get("items") or []:
		if not row.get("item_code") or not cint(
			frappe.get_cached_value("Item", row.item_code, "is_stock_item")
		):
			continue
		if not flt_qty(row):
			continue

		try:
			doc.check_expense_account(row)
		except Exception as exc:
			frappe.clear_messages()
			errors.append(
				make_error(
					category="accounting",
					message=exc,
					exc_type=type(exc).__name__,
					item_code=row.item_code,
					row=row.idx,
					table="items",
				)
			)

		warehouse = row.get("warehouse")
		if warehouse:
			stock_account = _get_warehouse_account(warehouse, doc.company)
			if not stock_account:
				errors.append(
					make_error(
						category="accounting",
						message=_(
							"Row #{0}: No Stock Account found for Warehouse {1} / Company {2}. "
							"Configure Warehouse Account or Company default inventory account."
						).format(row.idx, frappe.bold(warehouse), frappe.bold(doc.company)),
						item_code=row.item_code,
						row=row.idx,
						warehouse=warehouse,
						table="items",
					)
				)
			else:
				errors.extend(_collect_account_company_errors(stock_account, doc.company, row))

		if row.get("expense_account"):
			errors.extend(_collect_account_company_errors(row.expense_account, doc.company, row))

		errors.extend(_collect_dimension_errors(doc, row))

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
	return errors, limitations


def validate_accounting_readiness(doc) -> list[dict]:
	errors, limitations = collect_accounting_errors(doc)
	if errors:
		frappe.throw(errors[0]["message"], title=_("Accounting Validation"))
	return limitations


def _get_warehouse_account(warehouse: str, company: str) -> str | None:
	account = frappe.db.get_value("Warehouse", warehouse, "account")
	if account:
		return account
	return frappe.get_cached_value("Company", company, "default_inventory_account")


def _collect_account_company_errors(account: str, company: str, row) -> list[dict]:
	errors: list[dict] = []
	acc_company = frappe.get_cached_value("Account", account, "company")
	if acc_company and acc_company != company:
		errors.append(
			make_error(
				category="accounting",
				message=_("Row #{0}: Account {1} does not belong to Company {2}").format(
					row.idx, frappe.bold(account), frappe.bold(company)
				),
				item_code=row.item_code,
				row=row.idx,
				table="items",
			)
		)
	if cint(frappe.get_cached_value("Account", account, "is_group")):
		errors.append(
			make_error(
				category="accounting",
				message=_("Row #{0}: Account {1} is a group account and cannot be used for posting").format(
					row.idx, frappe.bold(account)
				),
				item_code=row.item_code,
				row=row.idx,
				table="items",
			)
		)
	return errors


def _collect_dimension_errors(doc, row) -> list[dict]:
	errors: list[dict] = []
	meta = frappe.get_meta("Delivery Note Item")
	for fieldname in ("department",):
		df = meta.get_field(fieldname)
		if not df:
			continue
		if df.reqd and not row.get(fieldname):
			errors.append(
				make_error(
					category="accounting",
					message=_("Row #{0}: {1} is mandatory").format(row.idx, _(df.label or fieldname)),
					item_code=row.item_code,
					row=row.idx,
					table="items",
				)
			)
	return errors


def flt_qty(row) -> float:
	from frappe.utils import flt

	return abs(flt(row.get("stock_qty") or row.get("qty") or 0))
