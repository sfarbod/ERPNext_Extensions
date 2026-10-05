"""PM Opening Advance — ledger-derived balances (allocation child + clearance lifecycle)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from erpnext_extensions.petty_management.services.clearance_reservation import (
	clearance_reserves_pm_request_balance_sql,
	opening_allocation_sql_filter,
	pm_request_allocation_sql_filter,
)
from erpnext_extensions.petty_management.services.constants import (
	FUNDING_SOURCE_OPENING_ADVANCE,
	FUNDING_SOURCE_PM_REQUEST,
)

_EPS = 1e-6


def remaining_at_cutover_amount(
	opening_advance_amount: float, previously_settled_before_migration: float
) -> float:
	return flt(opening_advance_amount) - flt(previously_settled_before_migration)


def sum_prior_opening_allocations(opening_advance: str, exclude_clearance_name: str | None = None) -> float:
	"""Sum reserved allocations against a PM Opening Advance (source of truth)."""
	if not opening_advance:
		return 0.0
	params: list[Any] = [opening_advance, FUNDING_SOURCE_OPENING_ADVANCE]
	excl_sql = ""
	if exclude_clearance_name:
		excl_sql = " AND p.name != %s "
		params.append(exclude_clearance_name)

	res_clause = clearance_reserves_pm_request_balance_sql("p")

	return flt(
		frappe.db.sql(
			f"""
			SELECT COALESCE(SUM(c.allocated_amount), 0)
			FROM `tabPM Clearance Request Allocation` c
			INNER JOIN `tabPM Clearance` p ON p.name = c.parent AND c.parenttype = 'PM Clearance'
			WHERE c.parentfield = 'request_allocations'
				AND IFNULL(c.is_legacy_row, 0) = 0
				AND c.pm_opening_advance = %s
				AND IFNULL(c.funding_source_type, '') = %s
				AND {res_clause}
				{excl_sql}
			LOCK IN SHARE MODE
			""",
			tuple(params),
		)[0][0]
	)


def get_opening_advance_allocated_in_pm(
	opening_advance: str, exclude_clearance_name: str | None = None
) -> float:
	return sum_prior_opening_allocations(opening_advance, exclude_clearance_name)


def get_opening_advance_available_amount(
	opening_advance: str, exclude_clearance_name: str | None = None
) -> float:
	row = frappe.db.get_value(
		"PM Opening Advance",
		opening_advance,
		[
			"docstatus",
			"status",
			"opening_advance_amount",
			"previously_settled_before_migration",
		],
		as_dict=True,
	)
	if not row or cint_docstatus(row) != 1 or (row.status or "").strip() == "Cancelled":
		return 0.0
	remaining = remaining_at_cutover_amount(
		row.opening_advance_amount, row.previously_settled_before_migration
	)
	allocated = get_opening_advance_allocated_in_pm(opening_advance, exclude_clearance_name)
	return flt(remaining) - flt(allocated)


def cint_docstatus(row: dict) -> int:
	from frappe.utils import cint

	return cint(row.get("docstatus"))


def compute_opening_advance_derived_fields(
	opening_advance: str, exclude_clearance_name: str | None = None
) -> dict[str, float]:
	row = frappe.db.get_value(
		"PM Opening Advance",
		opening_advance,
		["opening_advance_amount", "previously_settled_before_migration"],
		as_dict=True,
	)
	if not row:
		return {
			"remaining_at_cutover": 0.0,
			"allocated_in_pm": 0.0,
			"available_opening_balance": 0.0,
		}
	remaining = remaining_at_cutover_amount(
		row.opening_advance_amount, row.previously_settled_before_migration
	)
	allocated = get_opening_advance_allocated_in_pm(opening_advance, exclude_clearance_name)
	return {
		"remaining_at_cutover": remaining,
		"allocated_in_pm": allocated,
		"available_opening_balance": flt(remaining) - flt(allocated),
	}


def stamp_opening_advance_display_balances(doc: Document) -> None:
	"""Refresh cache-only balance fields from allocation ledger (not authoritative)."""
	if not doc.name:
		doc.allocated_in_pm = 0.0
		doc.remaining_at_cutover = remaining_at_cutover_amount(
			doc.opening_advance_amount, doc.previously_settled_before_migration
		)
		doc.available_opening_balance = doc.remaining_at_cutover
		return
	derived = compute_opening_advance_derived_fields(doc.name)
	doc.remaining_at_cutover = derived["remaining_at_cutover"]
	doc.allocated_in_pm = derived["allocated_in_pm"]
	doc.available_opening_balance = derived["available_opening_balance"]


def sync_opening_advance_from_holder(doc: Document) -> None:
	if not doc.holder:
		return
	holder = frappe.db.get_value(
		"PM Holder",
		doc.holder,
		["employee", "company", "petty_cash_account", "is_blocked"],
		as_dict=True,
	)
	if not holder:
		frappe.throw(_("PM Holder {0} not found.").format(doc.holder))
	if holder.is_blocked:
		frappe.throw(_("PM Holder {0} is blocked.").format(doc.holder))
	doc.employee = holder.employee
	doc.company = holder.company
	doc.petty_cash_account = holder.petty_cash_account
	if doc.company:
		doc.currency = frappe.db.get_value("Company", doc.company, "default_currency")


def validate_opening_advance_amounts(doc: Document) -> None:
	if flt(doc.opening_advance_amount) <= 0:
		frappe.throw(_("Opening Advance must be greater than zero."))
	prev = flt(doc.previously_settled_before_migration)
	if prev < 0:
		frappe.throw(_("Previously Settled Before Migration cannot be negative."))
	if prev > flt(doc.opening_advance_amount) + _EPS:
		frappe.throw(_("Previously settled cannot exceed opening advance amount."))
	if remaining_at_cutover_amount(doc.opening_advance_amount, prev) < -_EPS:
		frappe.throw(_("Remaining opening balance at cutover cannot be negative."))


def enforce_immutable_submitted_amounts(doc: Document) -> None:
	if doc.docstatus != 1 or doc.is_new():
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	for field in ("opening_advance_amount", "previously_settled_before_migration"):
		if flt(doc.get(field)) != flt(before.get(field)):
			frappe.throw(
				_("{0} cannot be changed after submit. Cancel and amend instead.").format(
					_(doc.meta.get_label(field))
				),
				title=_("Immutable field"),
			)


def opening_advance_has_reserving_clearances(opening_advance: str) -> bool:
	return get_opening_advance_allocated_in_pm(opening_advance) > _EPS


def get_holder_opening_available_amount(holder: str, exclude_clearance_name: str | None = None) -> float:
	if not holder or not frappe.db.has_table("PM Opening Advance"):
		return 0.0
	names = frappe.get_all(
		"PM Opening Advance",
		filters={"holder": holder, "docstatus": 1, "status": "Submitted"},
		pluck="name",
	)
	total = 0.0
	for name in names:
		total += get_opening_advance_available_amount(name, exclude_clearance_name)
	return flt(total)


def opening_advance_passes_clearance_filters(
	opening_advance_name: str,
	*,
	employee: str,
	company: str,
	holder: str,
	clearance_petty: str,
	exclude_clearance_name: str | None = None,
	require_available: bool = True,
) -> tuple[bool, str]:
	if not opening_advance_name:
		return False, _("PM Opening Advance is empty")
	oa = frappe.db.get_value(
		"PM Opening Advance",
		opening_advance_name,
		[
			"docstatus",
			"status",
			"holder",
			"employee",
			"company",
			"petty_cash_account",
		],
		as_dict=True,
	)
	if not oa:
		return False, _("PM Opening Advance not found")
	if cint_docstatus(oa) != 1 or (oa.status or "").strip() != "Submitted":
		return False, _("PM Opening Advance must be submitted")
	if (oa.holder or "") != (holder or ""):
		return False, _("PM Opening Advance belongs to another PM Holder")
	if oa.company != company:
		return False, _("PM Opening Advance belongs to another company")
	if (oa.petty_cash_account or "").strip() != (clearance_petty or "").strip():
		return False, _("PM Opening Advance petty cash account does not match this clearance")
	if require_available:
		avail = get_opening_advance_available_amount(opening_advance_name, exclude_clearance_name)
		if avail <= _EPS:
			return False, _("No available opening balance on {0}").format(opening_advance_name)
	return True, ""


def allocation_row_funding_source_type(row: Document) -> str:
	ft = (getattr(row, "funding_source_type", None) or "").strip()
	if ft:
		return ft
	if (getattr(row, "pm_opening_advance", None) or "").strip():
		return FUNDING_SOURCE_OPENING_ADVANCE
	return FUNDING_SOURCE_PM_REQUEST


@frappe.whitelist()
def get_opening_advance_allocation_context(
	pm_opening_advance: str,
	pm_clearance: str | None = None,
	company: str | None = None,
	employee: str | None = None,
	holder: str | None = None,
	petty_cash_account: str | None = None,
) -> dict[str, Any]:
	if not pm_opening_advance:
		return {}
	if not frappe.has_permission("PM Opening Advance", "read", pm_opening_advance):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	exclude_clearance = (
		pm_clearance if pm_clearance and frappe.db.exists("PM Clearance", pm_clearance) else None
	)
	if exclude_clearance:
		from erpnext_extensions.petty_management.services.holder_service import (
			clearance_petty_cash_account,
			get_holder_petty_cash_account,
		)

		cl = frappe.get_doc("PM Clearance", pm_clearance)
		if not frappe.has_permission("PM Clearance", "read", doc=cl):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		company = cl.company
		employee = cl.employee
		holder = cl.holder or ""
		petty_cash_account = clearance_petty_cash_account(cl) or get_holder_petty_cash_account(holder)

	oa = frappe.get_doc("PM Opening Advance", pm_opening_advance)
	ok, msg = opening_advance_passes_clearance_filters(
		pm_opening_advance,
		employee=employee or oa.employee,
		company=company or oa.company,
		holder=holder or oa.holder,
		clearance_petty=(petty_cash_account or oa.petty_cash_account or "").strip(),
		exclude_clearance_name=exclude_clearance,
		require_available=False,
	)
	if not ok:
		frappe.throw(msg, title=_("Invalid PM Opening Advance"))

	prev = sum_prior_opening_allocations(pm_opening_advance, exclude_clearance)
	remaining = remaining_at_cutover_amount(oa.opening_advance_amount, oa.previously_settled_before_migration)
	available = flt(remaining) - flt(prev)
	return {
		"pm_opening_advance": pm_opening_advance,
		"request_amount": flt(oa.opening_advance_amount),
		"paid_amount": remaining,
		"previously_allocated_amount": prev,
		"available_amount": available,
		"employee": oa.employee,
		"holder": oa.holder,
		"petty_cash_account": oa.petty_cash_account,
		"company": oa.company,
		"opening_source_type": oa.opening_source_type,
		"has_available_balance": available > _EPS,
	}


def format_opening_advance_link_search_row(row: dict) -> list[str]:
	from frappe.utils import fmt_money

	currency = row.get("currency")
	opening_fmt = fmt_money(flt(row.get("opening_advance_amount")), currency=currency)
	avail_fmt = fmt_money(flt(row.get("available_amount")), currency=currency)
	emp_name = (row.get("employee_name") or "").strip()
	emp_code = (row.get("employee") or "").strip()
	if emp_name and emp_code:
		emp_line = f"{emp_name} | {emp_code}"
	else:
		emp_line = emp_name or emp_code
	return [
		row["name"],
		emp_line,
		f"{_('Available')}: {avail_fmt}",
		f"{_('Opening')}: {opening_fmt}",
	]


def _opening_advance_available_subquery(excl_alloc_sql: str) -> str:
	res_clause = clearance_reserves_pm_request_balance_sql("p")
	open_filt = opening_allocation_sql_filter("c")
	return f"""
		(
			(oa.opening_advance_amount - IFNULL(oa.previously_settled_before_migration, 0))
			- COALESCE((
				SELECT SUM(c.allocated_amount)
				FROM `tabPM Clearance Request Allocation` c
				INNER JOIN `tabPM Clearance` p
					ON p.name = c.parent AND c.parenttype = 'PM Clearance'
				WHERE c.parentfield = 'request_allocations'
					AND IFNULL(c.is_legacy_row, 0) = 0
					AND c.pm_opening_advance = oa.name
					AND {open_filt}
					AND {res_clause}
					{excl_alloc_sql}
			), 0)
		) AS available_amount
	"""


def pm_opening_advance_query_for_link(doctype, txt, searchfield, start, page_len, filters):
	"""Standard link search for PM Opening Advance (desk Link fields)."""
	if doctype != "PM Opening Advance":
		return []
	if isinstance(filters, str):
		filters = frappe.parse_json(filters) if filters else {}
	filters = filters or {}
	exclude_clearance = (filters.get("pm_clearance") or "").strip() or None

	from frappe.utils import cint

	values: dict[str, Any] = {
		"txt": f"%{txt}%",
		"start": cint(start),
		"page_len": cint(page_len),
	}
	excl_alloc_sql = ""
	if exclude_clearance:
		values["exclude_clearance"] = exclude_clearance
		excl_alloc_sql = " AND p.name != %(exclude_clearance)s "

	conditions = [
		"oa.docstatus = 1",
		"IFNULL(oa.status, '') = 'Submitted'",
	]
	if txt:
		conditions.append(
			"""(
				oa.name LIKE %(txt)s
				OR IFNULL(oa.employee, '') LIKE %(txt)s
				OR IFNULL(oa.employee_name, '') LIKE %(txt)s
				OR IFNULL(oa.reference_no, '') LIKE %(txt)s
				OR IFNULL(oa.opening_reconciliation_reference, '') LIKE %(txt)s
			)"""
		)
	for key in ("holder", "company", "employee"):
		if filters.get(key):
			conditions.append(f"oa.{key} = %({key})s")
			values[key] = filters[key]
	if (filters.get("petty_cash_account") or "").strip():
		conditions.append("IFNULL(oa.petty_cash_account, '') = %(petty)s")
		values["petty"] = filters["petty_cash_account"].strip()
	if cint(filters.get("exclude_blocked_holder")):
		conditions.append(
			"""EXISTS (
				SELECT 1 FROM `tabPM Holder` h
				WHERE h.name = oa.holder AND IFNULL(h.is_blocked, 0) = 0
			)"""
		)

	avail_sql = _opening_advance_available_subquery(excl_alloc_sql)
	where = " AND ".join(conditions)
	rows = frappe.db.sql(
		f"""
		SELECT
			oa.name,
			oa.opening_advance_amount,
			IFNULL(oa.employee_name, '') AS employee_name,
			IFNULL(oa.employee, '') AS employee,
			IFNULL(oa.currency, comp.default_currency) AS currency,
			{avail_sql}
		FROM `tabPM Opening Advance` oa
		LEFT JOIN `tabCompany` comp ON comp.name = oa.company
		WHERE {where}
		ORDER BY oa.opening_date DESC, oa.modified DESC
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		values,
		as_dict=True,
	)
	return [format_opening_advance_link_search_row(row) for row in rows]


def pm_opening_advance_query_for_pm_clearance(doctype, txt, searchfield, start, page_len, filters):
	if doctype != "PM Opening Advance":
		return []
	if isinstance(filters, str):
		filters = frappe.parse_json(filters) if filters else {}
	filters = dict(filters or {})
	holder = filters.get("holder")
	company = filters.get("company")
	petty = (filters.get("petty_cash_account") or "").strip()
	if not holder or not petty or not company:
		return []
	filters.update(
		{
			"holder": holder,
			"company": company,
			"petty_cash_account": petty,
			"exclude_blocked_holder": 1,
		}
	)
	return pm_opening_advance_query_for_link(doctype, txt, searchfield, start, page_len, filters)
