# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Manual / delayed Employee Checkin limit.

Port of the database Server Script ``# Employee Checkin - Outside Shift Day Limit``
(Before Insert). A punch whose calendar date is not today is manual. Each
employee may have at most N manual punches in a Jalali period that runs from
the 26th of one month through the 25th of the next. IN and OUT each count.
Existing rows count when their creation date differs from their punch date.

N and the enable flag are read from HR Settings custom fields (defaults:
enabled, maximum 5). Missing or invalid values never mean unlimited access.

Trusted device punches skip only this count:

* Authorized offline Excel import enters :func:`device_import_context` after
  role checks in ``employee_checkin_import``.
* Native ``add_log_based_on_employee_field`` requests are exempt only when the
  session user holds the ``Attendance Device Importer`` role. Method name
  alone is never enough. Desk / ``insert`` / ``insert_many`` stay under quota.

The database Server Script cannot see application context (empty
``frappe.flags`` in ``safe_exec``), so the policy lives here.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import date

import frappe
from frappe.utils import add_days, cint, get_datetime, getdate, nowdate

DEFAULT_MAX_MANUAL_CHECKINS = 5
ENABLE_FIELD = "espad_enable_manual_checkin_limit"
MAX_FIELD = "espad_max_manual_checkins"
POLICY_CACHE_KEY = "espad_manual_checkin_policy"
IMPORT_ROLE = "Attendance Device Importer"
NATIVE_DEVICE_API_MARKER = "add_log_based_on_employee_field"

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


def get_manual_checkin_policy() -> dict:
	"""Return ``{enabled: bool, maximum: int}`` with safe defaults.

	Missing fields, nulls, zero, or negative maxima never disable the limit by
	accident. Only an explicit Enable = 0 turns the quota off.
	"""
	# Request-local cache only. Avoids a Singles read on every punch in the same
	# request without retaining Redis values across test rollbacks.
	local_policy = getattr(frappe.local, "_espad_manual_checkin_policy", None)
	if isinstance(local_policy, dict) and "enabled" in local_policy and "maximum" in local_policy:
		return local_policy

	# Check fields cast missing Singles rows to 0. Inspect Singles so a missing
	# row means "use default enabled", not "explicitly disabled".
	if not _hr_settings_value_stored(ENABLE_FIELD):
		enabled = True
	else:
		enabled_raw = frappe.db.get_single_value("HR Settings", ENABLE_FIELD, cache=False)
		if enabled_raw in (None, ""):
			enabled = True
		else:
			enabled = bool(cint(enabled_raw))

	if not _hr_settings_value_stored(MAX_FIELD):
		maximum = DEFAULT_MAX_MANUAL_CHECKINS
	else:
		max_raw = frappe.db.get_single_value("HR Settings", MAX_FIELD, cache=False)
		maximum = cint(max_raw) if max_raw not in (None, "") else DEFAULT_MAX_MANUAL_CHECKINS
		if maximum < 1:
			maximum = DEFAULT_MAX_MANUAL_CHECKINS

	policy = {"enabled": enabled, "maximum": maximum}
	frappe.local._espad_manual_checkin_policy = policy
	return policy


def _hr_settings_value_stored(fieldname: str) -> bool:
	# ``frappe.db.exists("Singles", filters)`` is unreliable for this table.
	return bool(
		frappe.db.sql(
			"select 1 from tabSingles where doctype=%s and field=%s limit 1",
			("HR Settings", fieldname),
		)
	)


def clear_manual_checkin_policy_cache(doc=None, method=None):
	if hasattr(frappe.local, "_espad_manual_checkin_policy"):
		delattr(frappe.local, "_espad_manual_checkin_policy")
	try:
		frappe.cache.delete_value(POLICY_CACHE_KEY)
	except Exception:
		pass


def validate_hr_settings_manual_limit(doc, method=None):
	"""Only System Manager may change the quota fields; keep values sane."""
	changed = doc.has_value_changed(ENABLE_FIELD) or doc.has_value_changed(MAX_FIELD)
	if not changed:
		return
	if "System Manager" not in frappe.get_roles():
		frappe.throw(
			"Only a System Manager can change the manual Employee Checkin limit settings.",
			frappe.PermissionError,
		)
	if cint(doc.get(ENABLE_FIELD)) and cint(doc.get(MAX_FIELD)) < 1:
		frappe.throw(
			f"{MAX_FIELD} must be at least 1 when the manual checkin limit is enabled.",
			frappe.ValidationError,
		)


def enforce_manual_checkin_limit(doc):
	"""Apply the historical manual-checkin cap unless a trusted device path is active."""
	if not doc.employee:
		frappe.throw("Employee is required.")
	if not doc.time:
		frappe.throw("Check-in Time is required.")

	if _is_trusted_device_punch():
		return

	policy = get_manual_checkin_policy()
	if not policy["enabled"]:
		return

	checkin_datetime = get_datetime(doc.time)
	checkin_date = getdate(checkin_datetime)
	today = getdate(nowdate())
	if checkin_date == today:
		return

	period_start, period_end, start_jy, start_jm, end_jy, end_jm = period_bounds(checkin_date)
	manual_count = _count_manual_in_period(doc, period_start, period_end)
	maximum = policy["maximum"]
	if manual_count < maximum:
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
			maximum=maximum,
		)
	)


def _is_trusted_device_punch() -> bool:
	"""Exempt only authorized offline import context or authorized native device API."""
	if in_device_import():
		return True
	if not _request_is_native_device_api():
		return False
	# Role boundary: Importer role required. System Manager alone is not enough
	# here so Desk historical inserts by administrators stay under quota.
	return IMPORT_ROLE in frappe.get_roles()


def _request_is_native_device_api() -> bool:
	"""True when this request targets the HRMS biometric add_log method.

	Path/cmd alone never grants exemption; callers still need IMPORT_ROLE.
	"""
	markers = []
	req = getattr(frappe.local, "request", None)
	if req is not None:
		markers.append(cstr_path(getattr(req, "path", "")))
	form = getattr(frappe.local, "form_dict", None) or {}
	markers.append(cstr_path(form.get("cmd")))
	combined = " ".join(markers)
	return NATIVE_DEVICE_API_MARKER in combined


def cstr_path(value) -> str:
	return "" if value is None else str(value)


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


def _limit_message(
	*, employee, checkin_date, today, start_jy, start_jm, end_jy, end_jm, manual_count, maximum
) -> str:
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
		maximum,
		manual_count + 1,
	)
