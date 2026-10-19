# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Manual / delayed Employee Checkin limit.

Port of the database Server Script ``# Employee Checkin - Outside Shift Day Limit``
(Before Insert). A punch whose calendar date is not today is manual. Each
employee may have at most five manual punches in a Jalali period that runs
from the 26th of one month through the 25th of the next. IN and OUT each
count. Existing rows count when their creation date differs from their punch
date.

The database script runs inside Frappe ``safe_exec``. That sandbox exposes a
fresh empty ``frappe.flags``, so it cannot see a trusted context created by
application code. The policy therefore lives here, on ``Employee Checkin``
``before_insert``, and the database script is disabled by
``employee_checkin_import_setup``.

Trusted device imports call :func:`device_import_context` only after the
import API has authorized the user. The context is a ``contextvars`` token.
It is not a document field and not a request argument, and it is reset before
the API returns.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import date

import frappe
from frappe.utils import add_days, get_datetime, getdate, nowdate

MAX_MANUAL_CHECKINS = 5

_G_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_J_DAYS = (31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29)

_device_import_depth = contextvars.ContextVar("espad_device_checkin_import", default=0)


def in_device_import() -> bool:
	"""True only while :func:`device_import_context` is active on this task."""
	return _device_import_depth.get() > 0


@contextmanager
def device_import_context():
	"""Skip the manual-count restriction for inserts made in this block.

	Nested calls are counted. The token cannot be supplied by the client.
	"""
	token = _device_import_depth.set(_device_import_depth.get() + 1)
	try:
		yield
	finally:
		_device_import_depth.reset(token)


def before_insert(doc, method=None):
	enforce_manual_checkin_limit(doc)


def enforce_manual_checkin_limit(doc):
	"""Apply the historical manual-checkin cap unless a device import is active."""
	if not doc.employee:
		frappe.throw("Employee is required.")
	if not doc.time:
		frappe.throw("Check-in Time is required.")

	# Device imports still pass through HRMS validate() after this hook.
	# Only the manual/delayed count is skipped, and only inside the context.
	if in_device_import():
		return

	checkin_datetime = get_datetime(doc.time)
	checkin_date = getdate(checkin_datetime)
	today = getdate(nowdate())
	if checkin_date == today:
		return

	period_start, period_end, start_jy, start_jm, end_jy, end_jm = period_bounds(checkin_date)
	manual_count = _count_manual_in_period(doc, period_start, period_end)
	if manual_count < MAX_MANUAL_CHECKINS:
		return

	frappe.throw(
		_limit_message(
			employee=doc.employee,
			checkin_date=checkin_date,
			today=today,
			start_jy=start_jy,
			start_jm=start_jm,
			end_jy=end_jy,
			end_jm=end_jm,
			manual_count=manual_count,
		)
	)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
	"""Gregorian Y-M-D to Jalali Y-M-D. Same integer algorithm as the legacy script."""
	gy2 = gy - 1600
	gm2 = gm - 1
	gd2 = gd - 1
	g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
	for i in range(gm2):
		g_day_no += _G_DAYS[i]
	leap_g = (gy2 % 4 == 0 and gy2 % 100 != 0) or gy2 % 400 == 0
	if gm2 > 1 and leap_g:
		g_day_no += 1
	g_day_no += gd2

	j_day_no = g_day_no - 79
	j_np = j_day_no // 12053
	j_day_no = j_day_no % 12053
	jalali_year = 979 + 33 * j_np + 4 * (j_day_no // 1461)
	j_day_no = j_day_no % 1461
	if j_day_no >= 366:
		jalali_year += (j_day_no - 1) // 365
		j_day_no = (j_day_no - 1) % 365

	i = 0
	while i < 11 and j_day_no >= _J_DAYS[i]:
		j_day_no -= _J_DAYS[i]
		i += 1
	return jalali_year, i + 1, j_day_no + 1


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
	"""Jalali Y-M-D to a Gregorian date. Same integer algorithm as the legacy script."""
	jy2 = jy - 979
	jm2 = jm - 1
	jd2 = jd - 1
	j_day_no = 365 * jy2 + (jy2 // 33) * 8 + ((jy2 % 33) + 3) // 4
	for i in range(jm2):
		j_day_no += _J_DAYS[i]
	j_day_no += jd2
	g_day_no = j_day_no + 79

	gy = 1600 + 400 * (g_day_no // 146097)
	g_day_no = g_day_no % 146097
	leap = True
	if g_day_no >= 36525:
		g_day_no -= 1
		gy += 100 * (g_day_no // 36524)
		g_day_no = g_day_no % 36524
		if g_day_no >= 365:
			g_day_no += 1
		else:
			leap = False
	gy += 4 * (g_day_no // 1461)
	g_day_no = g_day_no % 1461
	if g_day_no >= 366:
		leap = False
		g_day_no -= 1
		gy += g_day_no // 365
		g_day_no = g_day_no % 365

	i = 0
	while i < 12:
		month_days = _G_DAYS[i]
		if i == 1 and leap:
			month_days += 1
		if g_day_no < month_days:
			break
		g_day_no -= month_days
		i += 1
	return date(gy, i + 1, g_day_no + 1)


def period_bounds(checkin_date: date) -> tuple[date, date, int, int, int, int]:
	"""Return the Jalali 26→25 period that contains ``checkin_date``.

	The tuple is ``(start, end, start_year, start_month, end_year, end_month)``.
	``start`` and ``end`` are inclusive Gregorian dates.
	"""
	jy, jm, jd = gregorian_to_jalali(checkin_date.year, checkin_date.month, checkin_date.day)
	if jd >= 26:
		start_jy, start_jm = jy, jm
		if jm == 12:
			end_jy, end_jm = jy + 1, 1
		else:
			end_jy, end_jm = jy, jm + 1
	else:
		end_jy, end_jm = jy, jm
		if jm == 1:
			start_jy, start_jm = jy - 1, 12
		else:
			start_jy, start_jm = jy, jm - 1
	start = jalali_to_gregorian(start_jy, start_jm, 26)
	end = jalali_to_gregorian(end_jy, end_jm, 25)
	return start, end, start_jy, start_jm, end_jy, end_jm


def _count_manual_in_period(doc, period_start: date, period_end: date) -> int:
	period_start_dt = get_datetime(f"{period_start} 00:00:00")
	period_after_end = get_datetime(f"{add_days(period_end, 1)} 00:00:00")
	existing_logs = frappe.get_all(
		"Employee Checkin",
		filters=[
			["employee", "=", doc.employee],
			["time", ">=", period_start_dt],
			["time", "<", period_after_end],
		],
		fields=["name", "time", "creation", "log_type"],
		order_by="time asc",
		limit_page_length=10000,
	)
	manual_count = 0
	for existing in existing_logs:
		if doc.name and existing.name == doc.name:
			continue
		if not existing.time or not existing.creation:
			continue
		if getdate(existing.time) != getdate(existing.creation):
			manual_count += 1
	return manual_count


def _limit_message(*, employee, checkin_date, today, start_jy, start_jm, end_jy, end_jm, manual_count) -> str:
	return (
		"امکان ثبت این تردد وجود ندارد."
		"<br><br>"
		"این تردد مربوط به تاریخی غیر از "
		"امروز است و به عنوان "
		"<b>تردد دستی / با تأخیر</b> "
		"محسوب می‌شود."
		"<br><br>"
		"<b>کارمند:</b> %s"
		"<br>"
		"<b>تاریخ تردد:</b> %s"
		"<br>"
		"<b>تاریخ ثبت:</b> %s"
		"<br><br>"
		"<b>دوره شمسی:</b> "
		"%04d/%02d/26 تا "
		"%04d/%02d/25"
		"<br>"
		"<b>تعداد تردد دستی قبلی:</b> %s"
		"<br>"
		"<b>حداکثر مجاز:</b> %s"
		"<br><br>"
		"این تردد، مورد شماره "
		"<b>%s</b> است و اجازه ثبت ندارد."
	) % (
		employee,
		str(checkin_date),
		str(today),
		start_jy,
		start_jm,
		end_jy,
		end_jm,
		manual_count,
		MAX_MANUAL_CHECKINS,
		manual_count + 1,
	)
