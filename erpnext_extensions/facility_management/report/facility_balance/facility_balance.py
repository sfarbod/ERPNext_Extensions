# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Facility Balance Query Report (v5.7.49).

Read-only. Source of truth: Facility contract amounts + opening paid fields +
submitted Facility Repayment rows (not GL).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, getdate

from erpnext_extensions.facility_management.facility_monetary import parse_facility_amount
from erpnext_extensions.facility_management.facility_report_filters import apply_facility_filters_to_sql

_MONEY_EPS = Decimal("0.000000001")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	# English source strings are intentionally specific so fa.csv mappings are not
	# overridden by older generic translations (e.g. Profit Amount → مبلغ بهره).
	return [
		{
			"label": _("Facility Number"),
			"fieldname": "facility",
			"fieldtype": "Link",
			"options": "Facility",
			"width": 140,
		},
		{"label": _("Facility Name"), "fieldname": "facility_name", "fieldtype": "Data", "width": 220},
		{
			"label": _("Facility Principal Amount"),
			"fieldname": "principal_amount",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Facility Profit Amount"),
			"fieldname": "profit_amount",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Opening Principal Balance"),
			"fieldname": "opening_principal_balance",
			"fieldtype": "Currency",
			"width": 150,
		},
		{
			"label": _("Opening Profit Balance"),
			"fieldname": "opening_profit_balance",
			"fieldtype": "Currency",
			"width": 150,
		},
		{
			"label": _("Profit Paid During Period"),
			"fieldname": "period_paid_profit",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("Principal Paid During Period"),
			"fieldname": "period_paid_principal",
			"fieldtype": "Currency",
			"width": 160,
		},
		{
			"label": _("Facility Remaining Principal"),
			"fieldname": "remaining_principal",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Facility Remaining Profit"),
			"fieldname": "remaining_profit",
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"label": _("Facility Remaining Total"),
			"fieldname": "remaining_total",
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"label": _("Last Payment Date"),
			"fieldname": "last_payment_date",
			"fieldtype": "Date",
			"width": 130,
		},
		{
			"label": _("Remaining Installments"),
			"fieldname": "remaining_installments",
			"fieldtype": "Int",
			"width": 130,
		},
		{
			"label": _("Facility Maturity Date"),
			"fieldname": "maturity_date",
			"fieldtype": "Date",
			"width": 120,
		},
	]


def _resolve_period(filters: frappe._dict) -> tuple[Any, Any]:
	"""Return (from_date, to_date). ``as_on_date`` remains an alias for to_date."""
	to_date = filters.get("to_date") or filters.get("as_on_date")
	from_date = filters.get("from_date")
	to_date = getdate(to_date) if to_date else None
	from_date = getdate(from_date) if from_date else None
	if from_date and to_date and from_date > to_date:
		frappe.throw(_("From Date cannot be after To Date"))
	return from_date, to_date


def _money(val) -> Decimal:
	return parse_facility_amount(val)


def _to_float(val: Decimal) -> float:
	return float(val)


def get_data(filters):
	filters = frappe._dict(filters or {})
	if not filters.get("company"):
		frappe.throw(_("Company is required"))

	from_date, to_date = _resolve_period(filters)

	conditions = ["company = %(company)s"]
	params: dict[str, Any] = {"company": filters.company}
	if filters.get("bank"):
		conditions.append("bank = %(bank)s")
		params["bank"] = filters.bank
	if filters.get("status"):
		conditions.append("status = %(status)s")
		params["status"] = filters.status
	apply_facility_filters_to_sql(conditions, params, filters)

	facilities = frappe.db.sql(
		f"""
		SELECT
			name,
			facility_name,
			principal_amount,
			profit_amount,
			opening_paid_principal_amount,
			opening_paid_profit_amount,
			maturity_date,
			status,
			is_opening_facility,
			installment_count
		FROM `tabFacility`
		WHERE {" AND ".join(conditions)}
		ORDER BY name
		""",
		params,
		as_dict=True,
	)
	if not facilities:
		return []

	names = [r.name for r in facilities]
	repay_agg = _batch_repayment_aggregates(names, from_date=from_date, to_date=to_date)
	schedules = _batch_schedules(names)

	data = []
	for fac in facilities:
		data.append(
			_build_row(
				fac,
				repay_agg.get(fac.name) or {},
				schedules.get(fac.name) or [],
				from_date=from_date,
				to_date=to_date,
			)
		)
	return data


def _batch_repayment_aggregates(
	facility_names: list[str], *, from_date, to_date
) -> dict[str, dict[str, Any]]:
	"""One query: before-period, during-period, to-date totals + last payment date."""
	if not facility_names:
		return {}

	params: list[Any] = [tuple(facility_names)]
	extra = ["facility IN %s", "docstatus = 1"]
	select_params: list[Any] = []

	if to_date:
		extra.append("posting_date <= %s")
		params.append(to_date)

	if from_date:
		before_p = "COALESCE(SUM(CASE WHEN posting_date < %s THEN principal_amount ELSE 0 END), 0)"
		before_pr = "COALESCE(SUM(CASE WHEN posting_date < %s THEN profit_amount ELSE 0 END), 0)"
		period_p = "COALESCE(SUM(CASE WHEN posting_date >= %s THEN principal_amount ELSE 0 END), 0)"
		period_pr = "COALESCE(SUM(CASE WHEN posting_date >= %s THEN profit_amount ELSE 0 END), 0)"
		select_params.extend([from_date, from_date, from_date, from_date])
	else:
		# No period start: opening uses facility opening_paid only; all scanned repayments are in-period.
		before_p = "0"
		before_pr = "0"
		period_p = "COALESCE(SUM(principal_amount), 0)"
		period_pr = "COALESCE(SUM(profit_amount), 0)"

	to_date_p = "COALESCE(SUM(principal_amount), 0)"
	to_date_pr = "COALESCE(SUM(profit_amount), 0)"
	last_pay = "MAX(posting_date)"

	where = " AND ".join(extra)
	# SELECT placeholders are bound before WHERE placeholders by the DB driver
	# when using a single params tuple — build SQL with numbered-style binding via ordered list.
	# MariaDB/%s style consumes left-to-right across the full statement.
	# Place SELECT case params first, then WHERE params.
	bind = list(select_params) + params
	rows = frappe.db.sql(
		f"""
		SELECT
			facility,
			{before_p} AS repaid_before_principal,
			{before_pr} AS repaid_before_profit,
			{period_p} AS period_paid_principal,
			{period_pr} AS period_paid_profit,
			{to_date_p} AS repaid_to_date_principal,
			{to_date_pr} AS repaid_to_date_profit,
			{last_pay} AS last_payment_date
		FROM `tabFacility Repayment`
		WHERE {where}
		GROUP BY facility
		""",
		tuple(bind),
		as_dict=True,
	)
	return {r.facility: r for r in rows}


def _batch_schedules(facility_names: list[str]) -> dict[str, list[dict]]:
	if not facility_names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT parent, idx, due_date, principal_amount, profit_amount
		FROM `tabFacility Repayment Schedule`
		WHERE parent IN %s
		ORDER BY parent, idx, due_date
		""",
		(tuple(facility_names),),
		as_dict=True,
	)
	out: dict[str, list[dict]] = defaultdict(list)
	for r in rows:
		out[r.parent].append(r)
	return out


def _count_remaining_installments(
	schedule_rows: list[dict],
	*,
	principal: Decimal,
	opening_paid_principal: Decimal,
	repaid_to_date_principal: Decimal,
	remaining_principal: Decimal | None = None,
	remaining_profit: Decimal | None = None,
	installment_count: int | None = None,
) -> int:
	"""Count schedule rows not fully covered by payments allocated to the schedule.

	Opening/migrated Facilities often store only remaining installment rows.
	Payments that belong before the first schedule row (≈ opening paid) are
	excluded from schedule allocation.

	When no schedule rows exist, fall back to Facility.installment_count if any
	principal/profit remains outstanding (common for some Debt Purchase rows).
	"""
	if not schedule_rows:
		has_remaining = (remaining_principal or Decimal("0")) > _MONEY_EPS or (
			remaining_profit or Decimal("0")
		) > _MONEY_EPS
		if has_remaining and installment_count:
			return max(int(installment_count), 0)
		return 1 if has_remaining else 0

	sched_principal = sum((_money(r.get("principal_amount")) for r in schedule_rows), Decimal("0"))
	covered_before_schedule = principal - sched_principal
	if covered_before_schedule < 0:
		covered_before_schedule = Decimal("0")

	total_paid = opening_paid_principal + repaid_to_date_principal
	paid_for_schedule = total_paid - covered_before_schedule
	if paid_for_schedule < 0:
		paid_for_schedule = Decimal("0")

	remaining = 0
	consumed = Decimal("0")
	for row in schedule_rows:
		row_p = _money(row.get("principal_amount"))
		if row_p <= 0 and _money(row.get("profit_amount")) <= 0:
			continue
		if consumed + row_p <= paid_for_schedule + _MONEY_EPS:
			consumed += row_p
			continue
		# Partial or unpaid installment still counts as outstanding.
		remaining += 1
		consumed += row_p
	return remaining


def _build_row(
	fac,
	agg: dict[str, Any],
	schedule_rows: list[dict],
	*,
	from_date,
	to_date,
) -> dict[str, Any]:
	principal = _money(fac.principal_amount)
	profit = _money(fac.profit_amount)
	opening_paid_p = _money(fac.opening_paid_principal_amount)
	opening_paid_pr = _money(fac.opening_paid_profit_amount)

	repaid_before_p = _money(agg.get("repaid_before_principal"))
	repaid_before_pr = _money(agg.get("repaid_before_profit"))
	period_paid_p = _money(agg.get("period_paid_principal"))
	period_paid_pr = _money(agg.get("period_paid_profit"))
	repaid_to_date_p = _money(agg.get("repaid_to_date_principal"))
	repaid_to_date_pr = _money(agg.get("repaid_to_date_profit"))

	# Period opening = remaining immediately before from_date
	# (contract − facility opening paid − repayments before period).
	paid_before_p = opening_paid_p + repaid_before_p
	paid_before_pr = opening_paid_pr + repaid_before_pr
	opening_principal_balance = principal - paid_before_p
	opening_profit_balance = profit - paid_before_pr

	# Ending remaining as of to_date (or all repayments if to_date empty).
	paid_to_date_p = opening_paid_p + repaid_to_date_p
	paid_to_date_pr = opening_paid_pr + repaid_to_date_pr
	remaining_principal = principal - paid_to_date_p
	remaining_profit = profit - paid_to_date_pr
	remaining_total = remaining_principal + remaining_profit

	remaining_installments = _count_remaining_installments(
		schedule_rows,
		principal=principal,
		opening_paid_principal=opening_paid_p,
		repaid_to_date_principal=repaid_to_date_p,
		remaining_principal=remaining_principal,
		remaining_profit=remaining_profit,
		installment_count=int(fac.installment_count or 0),
	)

	row = {
		"facility": fac.name,
		"facility_name": fac.facility_name,
		"principal_amount": _to_float(principal),
		"profit_amount": _to_float(profit),
		"opening_principal_balance": _to_float(opening_principal_balance),
		"opening_profit_balance": _to_float(opening_profit_balance),
		"period_paid_profit": _to_float(period_paid_pr),
		"period_paid_principal": _to_float(period_paid_p),
		"remaining_principal": _to_float(remaining_principal),
		"remaining_profit": _to_float(remaining_profit),
		"remaining_total": _to_float(remaining_total),
		"last_payment_date": agg.get("last_payment_date"),
		"remaining_installments": remaining_installments,
		"maturity_date": fac.maturity_date,
		# Compat keys for existing live E2E helpers (not displayed columns).
		"paid_principal": _to_float(paid_to_date_p),
		"paid_profit": _to_float(paid_to_date_pr),
		"status": fac.status,
	}
	return row
