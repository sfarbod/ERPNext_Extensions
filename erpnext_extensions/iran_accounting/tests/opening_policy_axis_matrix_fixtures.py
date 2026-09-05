# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Deterministic multi-axis fixtures for OpeningEntryPolicy 24-cell matrix."""

from __future__ import annotations

from datetime import date
from typing import Any

import frappe
from frappe.utils import getdate

from erpnext_extensions.iran_accounting.tests.opening_policy_golden_fixtures import (
	FROM_DATE,
	TO_DATE,
)
from erpnext_extensions.iran_accounting.tests.opening_policy_production_fixtures import (
	_balancing_accounts,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	create_test_unified_accounting_party,
)

MARKER = "AE-OEP-MATRIX"


def _cancel_marker_jes(company: str) -> None:
	for name in frappe.get_all(
		"Journal Entry",
		filters={"company": company, "user_remark": ("like", f"{MARKER}%"), "docstatus": 1},
		pluck="name",
	):
		doc = frappe.get_doc("Journal Entry", name)
		if doc.docstatus != 1:
			continue
		doc.flags.ignore_permissions = True
		doc.cancel()
	frappe.db.commit()


def _purge_marker_gl_orphans(company: str) -> None:
	voucher_nos = frappe.get_all(
		"Journal Entry",
		filters={"company": company, "user_remark": ("like", f"{MARKER}%"), "docstatus": 2},
		pluck="name",
	)
	if not voucher_nos:
		return
	frappe.db.sql(
		"""
		update `tabGL Entry`
		set is_cancelled=1
		where company=%(company)s and voucher_no in %(vouchers)s and is_cancelled=0
		""",
		{"company": company, "vouchers": voucher_nos},
	)
	frappe.db.commit()


def _ensure_cost_center(company: str) -> str:
	name = f"{MARKER}-CC"
	existing = frappe.db.get_value("Cost Center", {"company": company, "cost_center_name": name}, "name")
	if existing:
		return existing
	parent = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 1}, "name", order_by="lft"
	) or frappe.db.get_value("Company", company, "cost_center")
	doc = frappe.new_doc("Cost Center")
	doc.cost_center_name = name
	doc.company = company
	if parent:
		doc.parent_cost_center = parent
	doc.is_group = 0
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _ensure_project(company: str) -> str:
	project_name = f"{MARKER}-PRJ"
	existing = frappe.db.get_value("Project", {"project_name": project_name, "company": company}, "name")
	if existing:
		return existing
	doc = frappe.new_doc("Project")
	doc.project_name = project_name
	doc.company = company
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _ensure_customer(name: str) -> str:
	if frappe.db.exists("Customer", name):
		return name
	doc = frappe.new_doc("Customer")
	doc.customer_name = name
	doc.customer_type = "Company"
	doc.customer_group = (
		frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
	)
	doc.territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
	# Restore sites may mark regional fields (e.g. tax_id) mandatory + unique.
	if hasattr(doc, "tax_id") and not doc.tax_id:
		doc.tax_id = f"AE-{frappe.generate_hash(length=10)}"
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert()
	return doc.name


def _dedicated_target_account(company: str, currency: str | None) -> tuple[str, str]:
	account_name = f"{MARKER}-Target"
	existing = frappe.db.get_value("Account", {"company": company, "account_name": account_name}, "name")
	if existing:
		parent = frappe.db.get_value("Account", existing, "parent_account")
		frappe.db.set_value("Account", existing, "account_number", "120001", update_modified=False)
		return existing, parent
	parent = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": "Asset"},
		"name",
		order_by="lft",
	)
	doc = frappe.new_doc("Account")
	doc.account_name = account_name
	doc.account_number = "120001"
	doc.company = company
	doc.parent_account = parent
	doc.is_group = 0
	if currency:
		doc.account_currency = currency
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name, parent


def _receivable_account(company: str, currency: str | None) -> str:
	account = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Receivable", "is_group": 0, "disabled": 0},
		"name",
		order_by="lft",
	)
	if account:
		return account
	raise frappe.ValidationError(f"No receivable account for {company}")


def _submit_je(
	company: str,
	posting_date,
	*,
	lines: list[dict],
	remark: str,
	is_opening: str = "No",
) -> str:
	je = frappe.new_doc("Journal Entry")
	je.company = company
	je.posting_date = getdate(posting_date)
	je.user_remark = remark
	je.is_opening = is_opening
	for line in lines:
		je.append("accounts", line)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je.name


def _cancel_cross_fixture_opening_polluters(company: str) -> None:
	"""Cancel shared-site leftovers that ERPNext TB treats as opening forever.

	ERPNext ``get_opening_balances`` includes ``is_opening='Yes'`` regardless of
	posting_date (when ignore_is_opening is off). Voucher-GL print fixtures leave
	``AE-VGL-PRINT-OPENING`` JEs on ``_Test Company`` when tearDown is skipped;
	those inflate Account Explorer opening by the leftover amount (observed +50).
	"""
	from erpnext_extensions.iran_accounting.tests.voucher_gl_print_fixtures import (
		cancel_print_fixture_jes,
	)

	cancel_print_fixture_jes(company)


def cleanup_axis_matrix(company: str) -> None:
	_cancel_cross_fixture_opening_polluters(company)
	for uap in frappe.get_all(
		"Unified Accounting Party",
		filters={"unified_name": ("like", f"{MARKER}%")},
		pluck="name",
	):
		frappe.delete_doc("Unified Accounting Party", uap, force=1)
	_cancel_marker_jes(company)
	_purge_marker_gl_orphans(company)
	frappe.db.commit()


def ensure_axis_matrix_context(company: str) -> dict[str, Any]:
	"""Post GF-10-style opening-policy rows plus axis filter anchors."""
	cleanup_axis_matrix(company)

	currency = frappe.db.get_value("Company", company, "default_currency")
	target, parent_account = _dedicated_target_account(company, currency)
	_, offset = _balancing_accounts(company, currency)
	# Ensure offset has numeric code so account-axis level rollups stay out of Unclassified.
	if not frappe.db.get_value("Account", offset, "account_number"):
		frappe.db.set_value("Account", offset, "account_number", "210001", update_modified=False)
	elif str(frappe.db.get_value("Account", offset, "account_number") or "").startswith("99"):
		frappe.db.set_value("Account", offset, "account_number", "210001", update_modified=False)
	if frappe.db.get_value("Account", target, "account_number") in (None, ""):
		frappe.db.set_value("Account", target, "account_number", "120001", update_modified=False)
	receivable = _receivable_account(company, currency)
	cost_center = _ensure_cost_center(company)
	project = _ensure_project(company)
	customer = _ensure_customer(f"{MARKER}-Customer")

	def line(account: str, debit: float = 0.0, credit: float = 0.0, **extra) -> dict:
		row = {
			"account": account,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"debit": debit,
			"credit": credit,
			"cost_center": cost_center,
		}
		row.update(extra)
		return row

	# GF-10 core on dedicated target account
	_submit_je(
		company,
		date(2026, 3, 10),
		lines=[line(target, debit=400), line(offset, credit=400)],
		remark=f"{MARKER}-PRE-NORMAL",
	)
	_submit_je(
		company,
		date(2026, 3, 25),
		lines=[line(target, debit=100), line(offset, credit=100)],
		remark=f"{MARKER}-PRE-OPENING",
		is_opening="Yes",
	)
	voucher_je = _submit_je(
		company,
		date(2026, 4, 10),
		lines=[
			line(target, debit=500, project=project),
			line(offset, credit=500, project=project),
		],
		remark=f"{MARKER}-PERIOD-NORMAL",
	)
	_submit_je(
		company,
		date(2026, 4, 15),
		lines=[line(target, debit=300), line(offset, credit=300)],
		remark=f"{MARKER}-PERIOD-OPENING",
		is_opening="Yes",
	)

	# Party axis anchor (customer receivable)
	_submit_je(
		company,
		date(2026, 3, 12),
		lines=[
			line(receivable, debit=60, party_type="Customer", party=customer),
			line(offset, credit=60),
		],
		remark=f"{MARKER}-PARTY-PRE-OPENING",
		is_opening="Yes",
	)
	_submit_je(
		company,
		date(2026, 4, 11),
		lines=[
			line(receivable, debit=120, party_type="Customer", party=customer, project=project),
			line(offset, credit=120, project=project),
		],
		remark=f"{MARKER}-PARTY-PERIOD",
	)

	uap_name = create_test_unified_accounting_party(
		[("Customer", customer)],
		unified_name=f"{MARKER} UAP",
		company=company,
	)

	frappe.db.commit()
	return {
		"company": company,
		"target_account": target,
		"parent_account": parent_account,
		"offset_account": offset,
		"receivable_account": receivable,
		"cost_center": cost_center,
		"project": project,
		"customer": customer,
		"uap_name": uap_name,
		"voucher_no": voucher_je,
		"voucher_type": "Journal Entry",
		"currency": currency,
		"from_date": str(FROM_DATE),
		"to_date": str(TO_DATE),
		"fiscal_year": "2026",
	}
