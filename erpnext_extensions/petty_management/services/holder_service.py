from __future__ import annotations

from dataclasses import dataclass

import frappe
from erpnext.accounts.utils import get_balance_on
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

from erpnext_extensions.petty_management.utils import get_pm_holder_name


@dataclass(frozen=True)
class HolderBalances:
	account_gl_balance: float
	total_paid_amount: float
	total_allocated_amount: float
	funded_available_amount: float
	opening_available_amount: float
	available_amount: float
	pending_clearance_amount: float
	settled_amount: float
	remaining_limit: float | None
	opening_gross_amount: float
	opening_previously_settled_amount: float
	opening_remaining_at_cutover: float
	opening_allocated_amount: float


def get_holder(employee: str | None, company: str | None, *, required: bool = True) -> Document | None:
	holder_name = get_pm_holder_name(employee, company)
	if not holder_name:
		if required:
			frappe.throw(
				_("No PM Holder found for this employee and company. Please create PM Holder first.")
			)
		return None
	return frappe.get_doc("PM Holder", holder_name)


def get_holder_petty_cash_account(holder: str | None) -> str:
	if not holder:
		return ""
	return frappe.db.get_value("PM Holder", holder, "petty_cash_account") or ""


def clearance_petty_cash_account(doc: Document) -> str:
	if getattr(doc, "petty_cash_account", None):
		return (doc.petty_cash_account or "").strip()
	if getattr(doc, "holder", None):
		return get_holder_petty_cash_account(doc.holder)
	return ""


def request_petty_cash_account(pm_request_doc: Document) -> str:
	holder_name = pm_request_doc.holder or get_pm_holder_name(pm_request_doc.employee, pm_request_doc.company)
	return get_holder_petty_cash_account(holder_name)


def validate_holder(doc: Document) -> None:
	if not doc.employee:
		frappe.throw(_("Employee is required"))
	if not doc.company:
		frappe.throw(_("Company is required"))
	if not doc.petty_cash_account:
		frappe.throw(_("Petty Cash Account is required"))

	validate_unique_employee_company(doc)
	validate_petty_cash_account_company(doc.petty_cash_account, doc.company)
	set_holder_balance_fields(doc)


def validate_unique_employee_company(doc: Document) -> None:
	filters = {"employee": doc.employee, "company": doc.company}
	if doc.name:
		filters["name"] = ["!=", doc.name]
	if frappe.db.exists("PM Holder", filters):
		frappe.throw(
			_("A PM Holder already exists for this employee and company."),
			title=_("Duplicate PM Holder"),
		)


def validate_petty_cash_account_company(account: str | None, company: str | None) -> None:
	if not account or not company:
		return
	acc_company = frappe.db.get_value("Account", account, "company")
	if acc_company and acc_company != company:
		frappe.throw(_("Petty Cash Account {0} must belong to company {1}").format(account, company))


def sync_request_holder_fields(doc: Document) -> Document:
	holder = get_holder(doc.employee, doc.company)
	doc.holder = holder.name
	doc.petty_cash_account = holder.petty_cash_account
	doc.max_balance_for_petty_cash = holder.max_balance
	balances = get_holder_balances(holder.name, posting_date=doc.transaction_date or today())
	doc.previous_balance = balances.available_amount
	return holder


def sync_clearance_holder_fields(doc: Document) -> Document:
	holder = get_holder(doc.employee, doc.company)
	doc.holder = holder.name
	doc.petty_cash_account = holder.petty_cash_account
	exclude_clearance = _exclude_clearance_name_for_holder_sync(doc)
	balances = get_holder_balances(
		holder.name,
		posting_date=doc.transaction_date or today(),
		exclude_clearance_name=exclude_clearance,
	)
	doc.funded_available = balances.funded_available_amount
	doc.opening_available = balances.opening_available_amount
	doc.total_available = balances.available_amount
	doc.pending_amount = balances.available_amount
	doc.current_petty_balance = balances.available_amount
	doc.total_funded_amount = balances.total_paid_amount
	doc.total_cleared_amount = balances.settled_amount
	return holder


def clearance_exclude_name_for_validation(doc: Document) -> str | None:
	"""Submitted clearances reserve funding; settle/validate must not double-count own allocation."""
	from frappe.utils import cint

	name = (getattr(doc, "name", None) or "").strip()
	if not name or cint(doc.docstatus) != 1:
		return None
	return name


def _exclude_clearance_name_for_holder_sync(doc: Document) -> str | None:
	return clearance_exclude_name_for_validation(doc)


def set_holder_balance_fields(holder_doc: Document) -> None:
	balances = get_holder_balances(holder_doc.name)
	holder_doc.account_gl_balance = balances.account_gl_balance
	holder_doc.current_balance = balances.available_amount
	holder_doc.pending_clearance_amount = balances.pending_clearance_amount
	holder_doc.consumed_amount = balances.settled_amount


def get_holder_balances(
	holder: str, posting_date=None, exclude_clearance_name: str | None = None
) -> HolderBalances:
	row = frappe.db.get_value(
		"PM Holder",
		holder,
		["employee", "company", "petty_cash_account", "max_balance"],
		as_dict=True,
	)
	if not row:
		return HolderBalances(0, 0, 0, 0, 0, 0, 0, 0, None, 0, 0, 0, 0)

	as_on = getdate(posting_date or today())
	account_gl_balance = flt(get_balance_on(account=row.petty_cash_account, date=as_on, company=row.company))
	total_paid = get_holder_paid_amount(holder)
	total_allocated = get_holder_allocated_amount(holder, exclude_clearance_name=exclude_clearance_name)
	funded_reserved = get_holder_funded_reserved_amount(holder, exclude_clearance_name=exclude_clearance_name)
	funded_available = total_paid - funded_reserved
	opening_stats = get_holder_opening_balance_stats(holder, exclude_clearance_name=exclude_clearance_name)
	opening_available = opening_stats["opening_available"]
	total_available = funded_available + opening_available
	pending = get_holder_pending_clearance_amount(holder)
	settled = get_holder_settled_amount(holder)
	remaining_limit = (flt(row.max_balance) - total_available) if row.max_balance is not None else None

	return HolderBalances(
		account_gl_balance=account_gl_balance,
		total_paid_amount=total_paid,
		total_allocated_amount=total_allocated,
		funded_available_amount=funded_available,
		opening_available_amount=opening_available,
		available_amount=total_available,
		pending_clearance_amount=pending,
		settled_amount=settled,
		remaining_limit=remaining_limit,
		opening_gross_amount=opening_stats["opening_gross"],
		opening_previously_settled_amount=opening_stats["opening_previously_settled"],
		opening_remaining_at_cutover=opening_stats["opening_remaining_at_cutover"],
		opening_allocated_amount=opening_stats["opening_allocated"],
	)


def get_holder_context(employee: str | None, company: str | None, posting_date=None) -> dict:
	holder = get_holder(employee, company, required=False)
	if not holder:
		return {}
	balances = get_holder_balances(holder.name, posting_date=posting_date)
	return {
		"name": holder.name,
		"employee": holder.employee,
		"company": holder.company,
		"petty_cash_account": holder.petty_cash_account,
		"max_balance": holder.max_balance,
		"default_employee_bank_account": holder.default_employee_bank_account,
		"account_gl_balance": balances.account_gl_balance,
		"current_balance": balances.available_amount,
		"funded_available_amount": balances.funded_available_amount,
		"opening_available_amount": balances.opening_available_amount,
		"total_available_amount": balances.available_amount,
		"pending_clearance_amount": balances.pending_clearance_amount,
		"consumed_amount": balances.settled_amount,
		"total_funded_amount": balances.total_paid_amount,
		"total_allocated_amount": balances.total_allocated_amount,
		"remaining_limit": balances.remaining_limit,
		"opening_gross_amount": balances.opening_gross_amount,
		"opening_previously_settled_amount": balances.opening_previously_settled_amount,
		"opening_remaining_at_cutover": balances.opening_remaining_at_cutover,
		"opening_allocated_amount": balances.opening_allocated_amount,
	}


def get_holder_paid_amount(holder: str) -> float:
	if not frappe.db.has_table("PM Request"):
		return 0.0
	from erpnext_extensions.petty_management.services.funding_queries import sum_submitted_pe_amount

	total = 0.0
	for req_name in frappe.get_all(
		"PM Request",
		filters={"holder": holder, "docstatus": 1},
		pluck="name",
	):
		total += flt(sum_submitted_pe_amount(req_name))
	return total


def get_holder_allocated_amount(holder: str, exclude_clearance_name: str | None = None) -> float:
	"""All reserved funding allocations (PM Request + Opening Advance) for holder."""
	return flt(get_holder_funded_reserved_amount(holder, exclude_clearance_name)) + flt(
		get_holder_opening_reserved_amount(holder, exclude_clearance_name)
	)


def get_holder_funded_reserved_amount(holder: str, exclude_clearance_name: str | None = None) -> float:
	if not frappe.db.has_table("PM Clearance Request Allocation"):
		return 0.0
	from erpnext_extensions.petty_management.services.clearance_reservation import (
		clearance_reserves_pm_request_balance_sql,
		pm_request_allocation_sql_filter,
	)

	res = clearance_reserves_pm_request_balance_sql("cl")
	excl_sql = ""
	params: list = [holder]
	if exclude_clearance_name:
		excl_sql = " AND cl.name != %s "
		params.append(exclude_clearance_name)
	return flt(
		frappe.db.sql(
			f"""
			select coalesce(sum(a.allocated_amount), 0)
			from `tabPM Clearance Request Allocation` a
			inner join `tabPM Request` pr on pr.name = a.pm_request
			inner join `tabPM Clearance` cl on cl.name = a.parent and a.parenttype = 'PM Clearance'
			where pr.holder = %s
				and a.parentfield = 'request_allocations'
				and ifnull(a.is_legacy_row, 0) = 0
				and {pm_request_allocation_sql_filter("a")}
				and {res}
				{excl_sql}
			""",
			tuple(params),
		)[0][0]
	)


def get_holder_opening_reserved_amount(holder: str, exclude_clearance_name: str | None = None) -> float:
	if not frappe.db.has_table("PM Opening Advance"):
		return 0.0
	from erpnext_extensions.petty_management.services.clearance_reservation import (
		clearance_reserves_pm_request_balance_sql,
		opening_allocation_sql_filter,
	)

	res = clearance_reserves_pm_request_balance_sql("cl")
	excl_sql = ""
	params: list = [holder]
	if exclude_clearance_name:
		excl_sql = " AND cl.name != %s "
		params.append(exclude_clearance_name)
	return flt(
		frappe.db.sql(
			f"""
			select coalesce(sum(a.allocated_amount), 0)
			from `tabPM Clearance Request Allocation` a
			inner join `tabPM Opening Advance` oa on oa.name = a.pm_opening_advance
			inner join `tabPM Clearance` cl on cl.name = a.parent and a.parenttype = 'PM Clearance'
			where oa.holder = %s
				and oa.docstatus = 1
				and ifnull(oa.status, '') = 'Submitted'
				and a.parentfield = 'request_allocations'
				and ifnull(a.is_legacy_row, 0) = 0
				and {opening_allocation_sql_filter("a")}
				and {res}
				{excl_sql}
			""",
			tuple(params),
		)[0][0]
	)


def get_holder_opening_balance_stats(
	holder: str, exclude_clearance_name: str | None = None
) -> dict[str, float]:
	from erpnext_extensions.petty_management.services.opening_advance_service import (
		get_opening_advance_available_amount,
		remaining_at_cutover_amount,
		sum_prior_opening_allocations,
	)

	if not holder or not frappe.db.has_table("PM Opening Advance"):
		return {
			"opening_gross": 0.0,
			"opening_previously_settled": 0.0,
			"opening_remaining_at_cutover": 0.0,
			"opening_allocated": 0.0,
			"opening_available": 0.0,
		}
	rows = frappe.get_all(
		"PM Opening Advance",
		filters={"holder": holder, "docstatus": 1, "status": "Submitted"},
		fields=[
			"name",
			"opening_advance_amount",
			"previously_settled_before_migration",
		],
	)
	gross = 0.0
	prev_settled = 0.0
	remaining_cutover = 0.0
	allocated = 0.0
	available = 0.0
	for row in rows:
		gross += flt(row.opening_advance_amount)
		prev_settled += flt(row.previously_settled_before_migration)
		rem = remaining_at_cutover_amount(row.opening_advance_amount, row.previously_settled_before_migration)
		remaining_cutover += rem
		alloc = sum_prior_opening_allocations(row.name, exclude_clearance_name)
		allocated += alloc
		available += get_opening_advance_available_amount(row.name, exclude_clearance_name)
	return {
		"opening_gross": gross,
		"opening_previously_settled": prev_settled,
		"opening_remaining_at_cutover": remaining_cutover,
		"opening_allocated": allocated,
		"opening_available": available,
	}


def get_holder_pending_clearance_amount(holder: str) -> float:
	if not frappe.db.has_table("PM Clearance"):
		return 0.0
	# v4.7.2: include docstatus 0 Pending* clearances (approval not yet submitted)
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(cl.total_expense_amount), 0)
			from `tabPM Clearance` cl
			where cl.holder = %s
				and ifnull(cl.status, '') not in ('Cancelled', 'Rejected', 'Draft')
				and (
					(
						cl.docstatus = 1
						and (
							ifnull(cl.journal_entry, '') = ''
							or ifnull((
								select je.docstatus from `tabJournal Entry` je
								where je.name = cl.journal_entry limit 1
							), 0) != 1
						)
					)
					or (
						cl.docstatus = 0
						and (
							ifnull(cl.status, '') in (
								'Pending Approval',
								'Pending Manager Approval',
								'Pending Finance Review'
							)
							or cl.workflow_state in (
								select name from `tabWorkflow State`
								where workflow_state_name in (
									'Pending Manager Approval',
									'Pending Finance Review',
									'Pending Approval'
								)
							)
						)
					)
				)
			""",
			holder,
		)[0][0]
	)


def get_holder_settled_amount(holder: str) -> float:
	"""Amount cleared in accounting: submitted clearance + submitted settlement JE only."""
	if not frappe.db.has_table("PM Clearance"):
		return 0.0
	return flt(
		frappe.db.sql(
			"""
			select coalesce(sum(cl.total_expense_amount), 0)
			from `tabPM Clearance` cl
			inner join `tabJournal Entry` je on je.name = cl.journal_entry and je.docstatus = 1
			where cl.holder = %s
				and cl.docstatus = 1
				and ifnull(cl.status, '') not in ('Cancelled', 'Rejected')
			""",
			holder,
		)[0][0]
	)
