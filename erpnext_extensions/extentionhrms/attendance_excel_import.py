# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Bulk helpers for the Attendance Details Excel importer.

Client-side Excel parsing and shift/schedule resolution stay in the Desk
Client Script. These endpoints remove N+1 Desk round-trips for:

* existing Attendance lookup
* shift / schedule / holiday preload
* batched create / draft-update / submitted-field update

Business field mapping remains in the Client Script (``applyValues``). The
batch importer persists the values the client already computed.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate

IMPORT_ROLES = ("System Manager", "HR Manager")
MAX_LOOKUP = 5000
MAX_BATCH = 100
MAX_PRELOAD_EMPLOYEES = 500

ALLOWED_SUBMITTED_FIELDS = (
	"status",
	"shift",
	"in_time",
	"out_time",
	"late_entry",
	"early_exit",
	"working_hours",
	"standard_working_hours",
	"custom_scheduled_day",
	"custom_is_holiday",
	"custom_shift_work_hours",
	"custom_presence_shortage_hours",
	"custom_work_shortage_hours",
	"custom_friday_work_hours",
	"custom_holiday_work_hours",
	"custom_mission_work_hours",
	"custom_night_work_hours",
	"custom_total_leave_hours",
	"custom_leave_hours",
	"custom_paid_leave_hours",
	"custom_unpaid_leave_hours",
	"custom_regular_work_hours",
	"custom_regular_overtime_hours",
	"custom_extra_presence_hours",
	"custom_approved_overtime_hours",
	"custom_missing_checkin",
	"custom_calculation_details",
)

# Excel is the source of truth for these fields on the Attendance Excel Import
# path only. ESPAD ATT AFTER SUBMIT / espad_attendance_calculate may overwrite
# hour fields from OTAR or punch math; re-apply after submit so Excel wins.
EXCEL_AUTHORITATIVE_FIELDS = (
	"status",
	"working_hours",
	"custom_approved_overtime_hours",
	"custom_regular_work_hours",
	"custom_regular_overtime_hours",
	"custom_extra_presence_hours",
	"custom_paid_leave_hours",
	"custom_unpaid_leave_hours",
	"custom_total_leave_hours",
	"custom_leave_hours",
	"custom_mission_work_hours",
	"custom_night_work_hours",
	"custom_work_shortage_hours",
	"custom_holiday_work_hours",
	"custom_friday_work_hours",
	"custom_calculation_details",
)


def _authorize():
	if frappe.session.user in (None, "", "Guest"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	roles = set(frappe.get_roles())
	if not roles.intersection(IMPORT_ROLES):
		frappe.throw(
			_("Only System Manager or HR Manager may import Attendance details."),
			frappe.PermissionError,
		)
	if not frappe.has_permission("Attendance", "create"):
		frappe.throw(_("Not permitted to create Attendance."), frappe.PermissionError)


def _load_json_list(value, label: str) -> list:
	if value in (None, ""):
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(_("{0} must be a JSON list.").format(label), frappe.ValidationError)
	if not isinstance(value, list):
		frappe.throw(_("{0} must be a list.").format(label), frappe.ValidationError)
	return value


def _load_json_dict(value, label: str) -> dict:
	if value in (None, ""):
		return {}
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			frappe.throw(_("{0} must be a JSON object.").format(label), frappe.ValidationError)
	if not isinstance(value, dict):
		frappe.throw(_("{0} must be an object.").format(label), frappe.ValidationError)
	return value


def _normalize_personnel_code(value) -> str:
	"""Match Client Script ``codeKey``: Persian/Arabic digits → ASCII, strip zeros."""
	text = cstr(value)
	if not text:
		return ""
	fa = "۰۱۲۳۴۵۶۷۸۹"
	ar = "٠١٢٣٤٥٦٧٨٩"
	out = []
	for ch in text:
		if ch in fa:
			out.append(str(fa.index(ch)))
		elif ch in ar:
			out.append(str(ar.index(ch)))
		else:
			out.append(ch)
	compact = "".join(out).replace(" ", "").strip()
	if not compact:
		return ""
	if compact.isdigit():
		return compact.lstrip("0") or "0"
	return compact


def minutes_to_hours(minutes=0) -> float:
	"""Match Client Script ``hours(minutes)``: round to 6 decimal places."""
	return round(max(0.0, float(minutes or 0)) / 60.0 * 1_000_000) / 1_000_000


def approved_overtime_hours_from_components(
	night_approved_minutes=0,
	mission_approved_minutes=0,
	holiday_approved_minutes=0,
	normal_approved_minutes=0,
) -> float:
	"""Excel Approved OT for the importer: Normal Approved + Holiday Approved only.

	Night Approved and Mission Approved arguments are accepted for call-site
	compatibility but are intentionally excluded from the sum.
	Unauthorized overtime is never included.
	"""
	_ = (night_approved_minutes, mission_approved_minutes)  # excluded by design
	total_minutes = float(holiday_approved_minutes or 0) + float(normal_approved_minutes or 0)
	return minutes_to_hours(total_minutes)


def work_shortage_hours_from_components(
	unauthorized_exit_minutes=0,
	early_minutes=0,
	late_minutes=0,
) -> float:
	"""Excel-authoritative shortage: خروج غیرمجاز + تعجیل + تأخیر (minutes → hours).

	Does not include absence and is independent of shift start/end.
	"""
	total_minutes = (
		float(unauthorized_exit_minutes or 0)
		+ float(early_minutes or 0)
		+ float(late_minutes or 0)
	)
	return minutes_to_hours(total_minutes)


def unauthorized_normal_ot_hours(normal_unauthorized_minutes=0) -> float:
	"""Excel عادی غیرمجاز → hours for BOTH regular_work and extra_presence."""
	return minutes_to_hours(normal_unauthorized_minutes)


def excel_business_status(
	*,
	paid_leave_minutes=0,
	unpaid_leave_minutes=0,
	mission_minutes=0,
	absence_minutes=0,
	presence_minutes=0,
	worked_minutes=0,
	has_in_out: bool = False,
	fallback_status: str = "Present",
) -> dict:
	"""Central Excel→Attendance status contract (shared with Client Script).

	Returns ``{status, review, reason}``. When ``review`` is True, ``status`` is a
	defensive fallback only — the importer must not silently write conflicting rows.

	Precedence (no punch dependency):
	1. Leave + mission / leave + absence / mission + absence → Review
	2. Working hours (حضور) > 0 AND total leave hours > 0 → Present
	   (paid/unpaid leave hours are still mapped; status is not On Leave)
	3. Leave > 0 with zero working hours → On Leave
	4. Absence > 0 → Absent
	5. Mission > 0 → Present
	6. Presence / worked / punches → Present
	7. Otherwise fallback (default Present); blank absence never forces Absent.
	"""
	paid = float(paid_leave_minutes or 0)
	unpaid = float(unpaid_leave_minutes or 0)
	mission = float(mission_minutes or 0)
	absence = float(absence_minutes or 0)
	presence = float(presence_minutes or 0)
	leave = paid > 0 or unpaid > 0

	if leave and mission > 0:
		return {
			"status": "Absent",
			"review": True,
			"reason": "Leave and mission conflict",
		}
	if leave and absence > 0:
		return {
			"status": "Absent",
			"review": True,
			"reason": "Leave and absence conflict",
		}
	if mission > 0 and absence > 0:
		return {
			"status": "Absent",
			"review": True,
			"reason": "Mission and absence conflict",
		}
	# Partial leave day: Excel حضور + leave hours → Present (retain leave fields).
	if leave and presence > 0:
		return {"status": "Present", "review": False, "reason": None}
	if leave:
		return {"status": "On Leave", "review": False, "reason": None}
	if absence > 0:
		return {"status": "Absent", "review": False, "reason": None}
	if mission > 0:
		return {"status": "Present", "review": False, "reason": None}
	if presence > 0 or float(worked_minutes or 0) > 0 or has_in_out:
		return {"status": "Present", "review": False, "reason": None}
	return {
		"status": fallback_status or "Present",
		"review": False,
		"reason": None,
	}


def build_excel_authoritative_payload(
	*,
	paid_leave_minutes=0,
	unpaid_leave_minutes=0,
	night_total_minutes=0,
	mission_minutes=0,
	unauthorized_exit_minutes=0,
	early_minutes=0,
	late_minutes=0,
	absence_minutes=0,
	holiday_ot_approved_minutes=0,
	normal_ot_approved_minutes=0,
	normal_ot_unauthorized_minutes=0,
	presence_minutes=0,
	worked_minutes=0,
	has_in_out: bool = False,
	fallback_status: str = "Present",
) -> dict:
	"""Single Excel→Attendance field contract used by tests and repair tooling.

	Mirrors Client Script ``applyValues`` hour mapping + ``excelImportStatus``.
	"""
	classification = excel_business_status(
		paid_leave_minutes=paid_leave_minutes,
		unpaid_leave_minutes=unpaid_leave_minutes,
		mission_minutes=mission_minutes,
		absence_minutes=absence_minutes,
		presence_minutes=presence_minutes,
		worked_minutes=worked_minutes,
		has_in_out=has_in_out,
		fallback_status=fallback_status,
	)
	paid_h = minutes_to_hours(paid_leave_minutes)
	unpaid_h = minutes_to_hours(unpaid_leave_minutes)
	unauth_h = unauthorized_normal_ot_hours(normal_ot_unauthorized_minutes)
	approved_h = approved_overtime_hours_from_components(
		0, 0, holiday_ot_approved_minutes, normal_ot_approved_minutes
	)
	return {
		"custom_paid_leave_hours": paid_h,
		"custom_unpaid_leave_hours": unpaid_h,
		"custom_total_leave_hours": minutes_to_hours(
			float(paid_leave_minutes or 0) + float(unpaid_leave_minutes or 0)
		),
		"custom_leave_hours": minutes_to_hours(
			float(paid_leave_minutes or 0) + float(unpaid_leave_minutes or 0)
		),
		"custom_night_work_hours": minutes_to_hours(night_total_minutes),
		"custom_mission_work_hours": minutes_to_hours(mission_minutes),
		"custom_work_shortage_hours": work_shortage_hours_from_components(
			unauthorized_exit_minutes, early_minutes, late_minutes
		),
		"custom_holiday_work_hours": minutes_to_hours(holiday_ot_approved_minutes),
		"custom_regular_work_hours": unauth_h,
		"custom_extra_presence_hours": unauth_h,
		"custom_regular_overtime_hours": unauth_h,
		"custom_approved_overtime_hours": approved_h,
		"working_hours": minutes_to_hours(presence_minutes),
		"status": classification["status"],
		"review": classification["review"],
		"review_reason": classification["reason"],
	}


def excel_status_from_absence(
	absence_minutes=0,
	fallback_status: str = "Present",
	*,
	presence_minutes=0,
	worked_minutes=0,
	has_in_out: bool = False,
	mission_minutes=0,
	paid_leave_minutes=0,
	unpaid_leave_minutes=0,
) -> str:
	"""Compatibility wrapper around :func:`excel_business_status`."""
	return excel_business_status(
		paid_leave_minutes=paid_leave_minutes,
		unpaid_leave_minutes=unpaid_leave_minutes,
		mission_minutes=mission_minutes,
		absence_minutes=absence_minutes,
		presence_minutes=presence_minutes,
		worked_minutes=worked_minutes,
		has_in_out=has_in_out,
		fallback_status=fallback_status,
	)["status"]


def expected_gregorian_from_jalali_ymd(year: int, month: int, day: int) -> str:
	"""Reference Jalali→Gregorian used by importer regression tests.

	Matches the Desk Client Script ``convertDate`` result for valid Jalali Y/M/D
	(verified: 1405/07/25 → 2026-10-17).
	"""
	import jdatetime

	return jdatetime.date(int(year), int(month), int(day)).togregorian().isoformat()


def mission_absence_conflict(mission_minutes=0, absence_minutes=0) -> bool:
	"""True when Excel has both positive mission and positive absence."""
	return float(mission_minutes or 0) > 0 and float(absence_minutes or 0) > 0


def leave_business_conflict(
	paid_leave_minutes=0,
	unpaid_leave_minutes=0,
	mission_minutes=0,
	absence_minutes=0,
) -> bool:
	"""True when Excel leave conflicts with mission or absence."""
	leave = float(paid_leave_minutes or 0) > 0 or float(unpaid_leave_minutes or 0) > 0
	if not leave:
		return False
	return float(mission_minutes or 0) > 0 or float(absence_minutes or 0) > 0


@frappe.whitelist()
def get_personnel_code_index():
	"""Return ``{normalized_code: [employee_name, ...]}`` for readable Employees.

	Uses ``get_all`` so ``custom_personal_code`` (permlevel 1) is available for
	matching, then filters each row with ``has_permission(Employee, read)``.
	"""
	_authorize()
	rows = frappe.get_all(
		"Employee",
		fields=["name", "custom_personal_code"],
		filters={"custom_personal_code": ["is", "set"]},
		limit_page_length=0,
	)
	index: dict[str, list[str]] = {}
	for row in rows:
		if not frappe.has_permission("Employee", "read", row.name):
			continue
		key = _normalize_personnel_code(row.custom_personal_code)
		if not key:
			continue
		index.setdefault(key, []).append(row.name)
	return {"index": index, "employees": sum(len(v) for v in index.values())}


@frappe.whitelist()
def find_existing_attendance(keys=None):
	"""Return existing non-cancelled Attendance for (employee, date) keys.

	Does not return cancelled rows (docstatus=2). On Leave rows are flagged so
	the client can skip them without overwriting leave.
	"""
	_authorize()
	raw = _load_json_list(keys, "keys")
	if len(raw) > MAX_LOOKUP:
		frappe.throw(
			_("An Attendance lookup accepts at most {0} keys.").format(MAX_LOOKUP),
			frappe.ValidationError,
		)

	pairs = []
	employees = []
	dates = []
	for index, item in enumerate(raw):
		if not isinstance(item, dict):
			frappe.throw(_("Key {0} is not an object.").format(index + 1), frappe.ValidationError)
		employee = cstr(item.get("employee")).strip()
		try:
			day = getdate(item.get("attendance_date") or item.get("date"))
		except Exception:
			continue
		if not employee or not day:
			continue
		day_s = day.strftime("%Y-%m-%d")
		pairs.append((employee, day_s))
		if employee not in employees:
			employees.append(employee)
		if day_s not in dates:
			dates.append(day_s)

	if not pairs:
		return {"rows": [], "checked": 0}

	readable = {
		name: bool(frappe.has_permission("Employee", "read", name)) for name in employees
	}
	employees = [e for e in employees if readable.get(e)]
	if not employees:
		return {"rows": [], "checked": 0}

	found = frappe.db.sql(
		"""
		select name, employee, attendance_date, docstatus, status,
			shift, custom_scheduled_day
		from `tabAttendance`
		where employee in %(employees)s
			and attendance_date in %(dates)s
			and docstatus != 2
		""",
		{"employees": employees, "dates": dates},
		as_dict=True,
	)
	wanted = set(pairs)
	rows = []
	for row in found:
		day_s = getdate(row.attendance_date).strftime("%Y-%m-%d")
		if (row.employee, day_s) not in wanted:
			continue
		if not readable.get(row.employee):
			continue
		rows.append(
			{
				"name": row.name,
				"employee": row.employee,
				"attendance_date": day_s,
				"docstatus": int(row.docstatus or 0),
				"status": cstr(row.status),
				"shift": cstr(row.shift) if row.shift else None,
				"custom_scheduled_day": row.custom_scheduled_day,
			}
		)
	return {"rows": rows, "checked": len(pairs)}


@frappe.whitelist()
def preload_attendance_import_context(employees=None):
	"""Bulk-load shift / schedule / holiday context for Excel import.

	Returns JSON-serializable structures the Client Script uses to resolve
	historical rotations without per-row ``frappe.client.get`` calls.
	"""
	_authorize()
	employees = [cstr(e).strip() for e in _load_json_list(employees, "employees") if cstr(e).strip()]
	if len(employees) > MAX_PRELOAD_EMPLOYEES:
		frappe.throw(
			_("At most {0} employees can be preloaded per request.").format(MAX_PRELOAD_EMPLOYEES),
			frappe.ValidationError,
		)
	employees = [e for e in employees if frappe.has_permission("Employee", "read", e)]
	if not employees:
		return {
			"employees": {},
			"assignments": {},
			"shift_types": {},
			"schedule_assignments": {},
			"schedules": {},
			"holiday_lists": {},
		}

	employee_map = {}
	for row in frappe.get_all(
		"Employee",
		filters={"name": ["in", employees]},
		fields=["name", "company", "custom_personal_code", "employee_name", "status"],
		limit_page_length=len(employees),
	):
		employee_map[row.name] = row

	assignments_by_employee: dict[str, list] = {e: [] for e in employees}
	ssa_names = set()
	shift_names = set()
	for row in frappe.get_all(
		"Shift Assignment",
		filters={"employee": ["in", employees], "docstatus": 1, "status": "Active"},
		fields=[
			"name",
			"employee",
			"shift_type",
			"start_date",
			"end_date",
			"shift_schedule_assignment",
		],
		order_by="employee asc, start_date asc",
		limit_page_length=50000,
	):
		assignments_by_employee.setdefault(row.employee, []).append(
			{
				"name": row.name,
				"shift_type": row.shift_type,
				"start_date": str(row.start_date) if row.start_date else None,
				"end_date": str(row.end_date) if row.end_date else None,
				"shift_schedule_assignment": row.shift_schedule_assignment,
			}
		)
		if row.shift_type:
			shift_names.add(row.shift_type)
		if row.shift_schedule_assignment:
			ssa_names.add(row.shift_schedule_assignment)

	schedule_assignments = {}
	schedule_names = set()
	for name in ssa_names:
		if not frappe.has_permission("Shift Schedule Assignment", "read", name):
			continue
		doc = frappe.get_doc("Shift Schedule Assignment", name)
		schedule_assignments[name] = {
			"name": doc.name,
			"shift_schedule": doc.shift_schedule,
			"create_shifts_after": str(doc.create_shifts_after) if doc.create_shifts_after else None,
		}
		if doc.shift_schedule:
			schedule_names.add(doc.shift_schedule)

	schedules = {}
	for name in schedule_names:
		if not frappe.has_permission("Shift Schedule", "read", name):
			continue
		doc = frappe.get_doc("Shift Schedule", name)
		schedules[name] = {
			"name": doc.name,
			"custom_use_custom_rotation": cint(doc.get("custom_use_custom_rotation")),
			"custom_rotation_type": cstr(doc.get("custom_rotation_type") or "Weekly"),
			"custom_repeat_every": cint(doc.get("custom_repeat_every")),
			"custom_repeat_on_days_copy": [
				{
					"day": cstr(r.day),
					"custom_sequence": cint(r.get("custom_sequence")),
					"custom_week_no": cint(r.get("custom_week_no")),
					"custom_day_type": cstr(r.get("custom_day_type") or "Work"),
					"custom_shift_type": cstr(r.get("custom_shift_type") or ""),
				}
				for r in (doc.get("custom_repeat_on_days_copy") or [])
			],
			"repeat_on_days": [{"day": cstr(r.day)} for r in (doc.get("repeat_on_days") or [])],
		}
		for row in schedules[name]["custom_repeat_on_days_copy"]:
			if row["custom_shift_type"]:
				shift_names.add(row["custom_shift_type"])

	shift_types = {}
	holiday_list_names = set()
	for name in shift_names:
		if not frappe.has_permission("Shift Type", "read", name):
			continue
		doc = frappe.get_doc("Shift Type", name)
		shift_types[name] = {
			"name": doc.name,
			"start_time": cstr(doc.start_time),
			"end_time": cstr(doc.end_time),
			"holiday_list": doc.holiday_list,
			"custom_apply_official_holidays": cint(doc.get("custom_apply_official_holidays")),
		}
		if doc.holiday_list:
			holiday_list_names.add(doc.holiday_list)

	holiday_lists = {}
	for name in holiday_list_names:
		if not frappe.has_permission("Holiday List", "read", name):
			continue
		doc = frappe.get_doc("Holiday List", name)
		holiday_lists[name] = [
			str(h.holiday_date) for h in (doc.holidays or []) if h.holiday_date
		]

	return {
		"employees": employee_map,
		"assignments": assignments_by_employee,
		"shift_types": shift_types,
		"schedule_assignments": schedule_assignments,
		"schedules": schedules,
		"holiday_lists": holiday_lists,
	}


@frappe.whitelist()
def import_attendance_details(records=None):
	"""Persist a batch of Attendance rows prepared by the Excel importer.

	Each record::

	    {
	      "employee": "...",
	      "attendance_date": "YYYY-MM-DD",
	      "mode": "create" | "update_draft" | "update_submitted",
	      "name": optional existing Attendance name,
	      "company": required for create,
	      "values": { fieldname: value, ... }
	    }
	"""
	_authorize()
	raw = _load_json_list(records, "records")
	if len(raw) > MAX_BATCH:
		frappe.throw(
			_("An Attendance import batch accepts at most {0} records.").format(MAX_BATCH),
			frappe.ValidationError,
		)

	created = []
	updated = []
	failed = []
	skipped = []

	for index, item in enumerate(raw):
		if not isinstance(item, dict):
			failed.append({"index": index, "error": "Record is not an object.", "category": "Validation error"})
			continue
		try:
			outcome = _persist_one(item)
		except Exception as exc:
			frappe.clear_messages()
			failed.append(
				{
					"index": index,
					"employee": cstr(item.get("employee")),
					"attendance_date": cstr(item.get("attendance_date")),
					"error": " ".join(cstr(exc).split())[:1000],
					"category": "Import error",
					"row": item.get("row"),
				}
			)
			continue
		bucket = outcome.get("bucket")
		if bucket == "created":
			created.append(outcome)
		elif bucket == "updated":
			updated.append(outcome)
		else:
			skipped.append(outcome)

	return {
		"created": created,
		"updated": updated,
		"failed": failed,
		"skipped": skipped,
	}


def _persist_one(item: dict) -> dict:
	employee = cstr(item.get("employee")).strip()
	day = getdate(item.get("attendance_date"))
	mode = cstr(item.get("mode") or "create").strip()
	values = _load_json_dict(item.get("values"), "values")
	row_no = item.get("row")

	if not employee or not day:
		frappe.throw(_("employee and attendance_date are required"), frappe.ValidationError)
	if not frappe.has_permission("Employee", "read", employee):
		frappe.throw(_("Not permitted for Employee {0}").format(employee), frappe.PermissionError)
	if not values:
		frappe.throw(_("values must be a nonempty object"), frappe.ValidationError)

	day_s = day.strftime("%Y-%m-%d")
	existing = frappe.get_all(
		"Attendance",
		filters={"employee": employee, "attendance_date": day_s, "docstatus": ["!=", 2]},
		fields=["name", "docstatus", "status"],
		limit_page_length=3,
	)
	if len(existing) > 1:
		frappe.throw(_("Duplicate Attendance in ERP for {0} on {1}").format(employee, day_s))

	# Protect HRMS leave docs from non-leave Excel overwrites. Excel rows that
	# still carry paid/unpaid leave hours may update On Leave → Present
	# (partial leave day) or reimport On Leave.
	if existing and existing[0].status == "On Leave":
		excel_has_leave = (
			float(values.get("custom_paid_leave_hours") or 0) > 0
			or float(values.get("custom_unpaid_leave_hours") or 0) > 0
		)
		if not excel_has_leave and cstr(values.get("status")) != "On Leave":
			return {
				"bucket": "skipped",
				"employee": employee,
				"attendance_date": day_s,
				"name": existing[0].name,
				"reason": "Existing Leave Attendance",
				"row": row_no,
			}

	if mode == "update_submitted" or (existing and int(existing[0].docstatus) == 1):
		if not existing or int(existing[0].docstatus) != 1:
			frappe.throw(_("Submitted Attendance not found for update"), frappe.ValidationError)
		name = cstr(item.get("name") or existing[0].name)
		saved = _update_submitted(name, employee, day_s, values)
		return {
			"bucket": "updated",
			"employee": employee,
			"attendance_date": day_s,
			"name": saved.name,
			"docstatus": 1,
			"row": row_no,
		}

	if existing:
		doc = frappe.get_doc("Attendance", existing[0].name)
		if int(doc.docstatus) != 0:
			frappe.throw(_("Attendance {0} is not a Draft").format(doc.name))
		doc.check_permission("write")
		_apply_values(doc, values)
		if doc.employee != employee or str(doc.attendance_date) != day_s:
			frappe.throw(_("Attendance employee/date scope changed"))
		doc.save()
		doc.submit()
		_reapply_excel_authoritative_fields(doc.name, values)
		return {
			"bucket": "updated",
			"employee": employee,
			"attendance_date": day_s,
			"name": doc.name,
			"docstatus": int(doc.docstatus),
			"row": row_no,
		}

	company = cstr(item.get("company") or "").strip() or frappe.db.get_value("Employee", employee, "company")
	if not company:
		frappe.throw(_("Employee company missing"))
	if not frappe.has_permission("Attendance", "create"):
		frappe.throw(_("Not permitted to create Attendance"), frappe.PermissionError)

	doc = frappe.get_doc(
		{
			"doctype": "Attendance",
			"employee": employee,
			"attendance_date": day_s,
			"company": company,
			"status": values.get("status") or "Absent",
		}
	)
	_apply_values(doc, values)
	doc.insert()
	doc.submit()
	_reapply_excel_authoritative_fields(doc.name, values)
	return {
		"bucket": "created",
		"employee": employee,
		"attendance_date": day_s,
		"name": doc.name,
		"docstatus": int(doc.docstatus),
		"row": row_no,
	}


def _apply_values(doc, values: dict) -> None:
	for field, value in values.items():
		if field in ("doctype", "name", "employee", "attendance_date", "company"):
			continue
		if field.startswith("_"):
			continue
		doc.set(field, value)


def _reapply_excel_authoritative_fields(name: str, values: dict) -> None:
	"""Restore Excel-mapped hours after After-Submit hooks may overwrite them."""
	restore = {
		k: values[k]
		for k in EXCEL_AUTHORITATIVE_FIELDS
		if k in values and k in ALLOWED_SUBMITTED_FIELDS
	}
	if not restore:
		return
	att = frappe.get_doc("Attendance", name)
	if int(att.docstatus) != 1:
		return
	att.check_permission("write")
	changed = False
	for field, value in restore.items():
		if att.get(field) != value:
			att.set(field, value)
			changed = True
	if not changed:
		return
	att.flags.ignore_validate_update_after_submit = True
	att.save()


def _update_submitted(name: str, employee: str, day_s: str, values: dict):
	roles = set(frappe.get_roles())
	if not roles.intersection(IMPORT_ROLES):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	att = frappe.get_doc("Attendance", name)
	if int(att.docstatus) != 1:
		frappe.throw(_("Only submitted Attendance may use submitted update mode"))
	if att.employee != employee or str(att.attendance_date) != day_s:
		frappe.throw(_("Attendance employee/date scope mismatch"))
	att.check_permission("write")
	# Allow Excel leave-hour rows to correct On Leave → Present (partial leave).
	# Block wiping leave when Excel carries neither paid nor unpaid leave hours.
	if att.status == "On Leave":
		excel_has_leave = (
			float(values.get("custom_paid_leave_hours") or 0) > 0
			or float(values.get("custom_unpaid_leave_hours") or 0) > 0
		)
		if not excel_has_leave and cstr(values.get("status")) != "On Leave":
			frappe.throw(_("On Leave Attendance requires separate review"))

	filtered = {k: v for k, v in values.items() if k in ALLOWED_SUBMITTED_FIELDS}
	if "standard_working_hours" not in filtered:
		frappe.throw(_("standard_working_hours is required for submitted updates"))
	for field, value in filtered.items():
		att.set(field, value)
	# Bypass submit validation for already-submitted docs; mirror the legacy
	# Server Script which writes permitted fields without cancel/amend.
	att.flags.ignore_validate_update_after_submit = True
	att.save()
	return att
