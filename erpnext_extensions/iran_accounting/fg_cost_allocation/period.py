# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Persian business month resolution via persian_calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import getdate

from persian_calendar.calendar.engine import CalendarEngine
from persian_calendar.calendar.period_engine import BusinessPeriodEngine
from persian_calendar.calendar.period_labels import format_period_label


@dataclass(frozen=True)
class ResolvedPeriod:
	company: str
	persian_year: int
	persian_month: int
	persian_month_name: str
	period_key: str
	start_date: date
	end_date: date
	calendar_system: str


_PERSIAN_MONTH_NAMES = {
	1: "فروردین",
	2: "اردیبهشت",
	3: "خرداد",
	4: "تیر",
	5: "مرداد",
	6: "شهریور",
	7: "مهر",
	8: "آبان",
	9: "آذر",
	10: "دی",
	11: "بهمن",
	12: "اسفند",
}


def resolve_persian_month(company: str, persian_year: int, persian_month: int) -> ResolvedPeriod:
	"""Resolve Jalali year/month to Gregorian date range using BusinessPeriodEngine."""
	if persian_month < 1 or persian_month > 12:
		frappe.throw(_("Persian Month must be between 1 and 12."))

	fy = frappe.db.get_value(
		"Fiscal Year",
		{"name": str(persian_year), "disabled": 0},
		["year_start_date", "year_end_date"],
		as_dict=True,
	)
	if not fy:
		# Fall back: locate fiscal year covering approximate Jalali year start
		fy = frappe.db.sql(
			"""
			SELECT name, year_start_date, year_end_date
			FROM `tabFiscal Year`
			WHERE disabled = 0
			  AND YEAR(year_start_date) BETWEEN %s AND %s
			ORDER BY year_start_date
			LIMIT 1
			""",
			(int(persian_year) - 622, int(persian_year) - 620),
			as_dict=True,
		)
		fy = fy[0] if fy else None
	if not fy:
		frappe.throw(_("Fiscal Year for Persian year {0} not found.").format(persian_year))

	periods = BusinessPeriodEngine.generate(
		start_date=fy.year_start_date,
		end_date=fy.year_end_date,
		periodicity="Monthly",
		company=company,
	)
	match = None
	for p in periods:
		if int(p.year) == int(persian_year) and int(p.period_number) == int(persian_month):
			match = p
			break
	if not match:
		# Alternative: filter by period_number only within FY if year encoding differs
		for p in periods:
			if int(p.period_number) == int(persian_month):
				match = p
				break
	if not match:
		frappe.throw(
			_("Could not resolve Persian month {0}/{1} for company {2}.").format(
				persian_month, persian_year, company
			)
		)

	try:
		label = format_period_label(match) or ""
	except Exception:
		label = ""
	if not label:
		label = f"{_PERSIAN_MONTH_NAMES.get(int(persian_month), persian_month)} {persian_year}"

	cal = CalendarEngine.for_company(company)
	return ResolvedPeriod(
		company=company,
		persian_year=int(persian_year),
		persian_month=int(persian_month),
		persian_month_name=label,
		period_key=match.key,
		start_date=getdate(match.from_date),
		end_date=getdate(match.to_date),
		calendar_system=getattr(match, "calendar_system", None) or cal.name,
	)


def list_fiscal_year_months(company: str, persian_year: int) -> list[ResolvedPeriod]:
	"""Return all monthly periods for a fiscal/Persian year."""
	out = []
	for month in range(1, 13):
		try:
			out.append(resolve_persian_month(company, persian_year, month))
		except Exception:
			break
	return out
