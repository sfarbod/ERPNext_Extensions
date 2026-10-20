# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Authorized bulk import of attendance-device Employee Checkins.

The manual/delayed limit stays in force for ordinary inserts. This endpoint
opens :func:`device_import_context` only after the caller is authenticated
and holds both Employee Checkin create permission and an explicit import
role. Client fields such as ``skip_manual_limit`` are ignored.

``Employee Checkin.device_id`` is Location / Device ID (physical terminal).
The Excel employee code maps to ``Employee.attendance_device_id``, never to
``device_id``.

Duplicate identity is employee + timestamp (second precision) + log type,
matching HRMS ``validate_duplicate_log``. Repeating a batch is idempotent.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime

from erpnext_extensions.extentionhrms.employee_checkin_manual_limit import (
	device_import_context,
	in_device_import,
)

IMPORT_ROLES = ("System Manager", "Attendance Device Importer")
MAX_RECORDS = 200
MAX_EXISTING_LOOKUP = 2000
MAX_ASSIGNMENTS = 500
MAX_DEVICE_ID_LEN = 140
_LOG_TYPES = {"IN", "OUT"}
_RECORD_KEYS = {"employee", "time", "log_type", "device_id", "skip_auto_attendance"}
_DEVICE_ID_RE = re.compile(r"^[\w.\-/: #]+$", re.UNICODE)


@frappe.whitelist()
def import_device_checkins(records=None, device_assignments=None, location_device_id=None):
	"""Create device checkins for an authorized user.

	``records`` is a list of ``{employee, time, log_type, device_id, skip_auto_attendance}``.
	``device_id`` on each record, and ``location_device_id``, mean physical terminal /
	location only. They must not be the employee's biometric/personnel code.
	``device_assignments`` optionally fills empty ``attendance_device_id`` values.
	"""
	_authorize()
	batch_location = _sanitize_device_id(location_device_id, label="location_device_id")
	parsed = _parse_records(records, batch_location=batch_location)
	assignments = _parse_assignments(device_assignments)
	with device_import_context():
		outcome = _insert_records(parsed)
	if in_device_import():
		frappe.throw("Device import context leaked past the insert block.")
	if assignments:
		outcome["device_ids"] = _assign_empty_attendance_device_ids(assignments)
	else:
		outcome["device_ids"] = {"updated": [], "preserved": [], "failed": []}
	frappe.logger("employee_checkin_device_import").info(
		"user=%s created=%s duplicates=%s failed=%s device_ids=%s",
		frappe.session.user,
		len(outcome["created"]),
		len(outcome["duplicates"]),
		len(outcome["failed"]),
		len(outcome["device_ids"]["updated"]),
	)
	return outcome


@frappe.whitelist()
def assign_empty_attendance_device_ids(assignments=None):
	"""Set ``attendance_device_id`` only where it is empty.

	Existing non-empty values are preserved. Conflicts with another employee
	are reported and not written. Other Employee fields are not written.
	"""
	_authorize()
	return _assign_empty_attendance_device_ids(assignments)


@frappe.whitelist()
def report_employee_code_stored_as_device_id(limit=500):
	"""Read-only report: checkins whose device_id equals the employee's attendance_device_id.

	Does not modify data. Intended for reviewing historical imports before any
	optional cleanup migration.
	"""
	_authorize()
	limit = max(1, min(cint(limit) or 500, 5000))
	rows = frappe.db.sql(
		"""
		select c.name, c.employee, c.time, c.log_type, c.device_id, e.attendance_device_id
		from `tabEmployee Checkin` c
		inner join `tabEmployee` e on e.name = c.employee
		where ifnull(c.device_id, '') != ''
			and c.device_id = e.attendance_device_id
		order by c.time desc
		limit %s
		""",
		limit,
		as_dict=True,
	)
	total = frappe.db.sql(
		"""
		select count(*) from `tabEmployee Checkin` c
		inner join `tabEmployee` e on e.name = c.employee
		where ifnull(c.device_id, '') != ''
			and c.device_id = e.attendance_device_id
		"""
	)[0][0]
	return {"total": total, "returned": len(rows), "rows": rows}


@frappe.whitelist()
def find_existing_checkin_keys(records=None):
	"""Return which punch keys already exist (pre-import duplicate detection).

	Authorized import callers only. Accepts a list of
	``{employee, time, log_type}`` (at most ``MAX_EXISTING_LOOKUP`` per call).
	Returns only matching keys — never full Employee Checkin documents.

	Uses the same identity as HRMS / :func:`import_device_checkins`
	(employee + second-precision time + log_type). Employee User Permissions
	are enforced: keys for unreadable employees are omitted.
	"""
	_authorize()
	raw = _load_json_list(records, "records")
	if len(raw) > MAX_EXISTING_LOOKUP:
		frappe.throw(
			_("An existing-check lookup accepts at most {0} records.").format(MAX_EXISTING_LOOKUP),
			frappe.ValidationError,
		)

	normalized = []
	employees = []
	for index, item in enumerate(raw):
		if not isinstance(item, dict):
			frappe.throw(_("Record {0} is not an object.").format(index + 1), frappe.ValidationError)
		employee = cstr(item.get("employee")).strip()
		log_type = cstr(item.get("log_type")).strip()
		if not employee or log_type not in _LOG_TYPES:
			continue
		try:
			when = get_datetime(item.get("time")).replace(microsecond=0)
		except Exception:
			continue
		if employee not in employees:
			employees.append(employee)
		normalized.append(
			{
				"employee": employee,
				"time": when,
				"time_key": when.strftime("%Y-%m-%d %H:%M:%S"),
				"log_type": log_type,
			}
		)

	if not normalized:
		return {"keys": [], "checked": 0, "existing": 0}

	readable = _readable_employees(employees)
	normalized = [row for row in normalized if readable.get(row["employee"])]
	if not normalized:
		return {"keys": [], "checked": 0, "existing": 0}

	found = _existing_keys(normalized)
	requested = {
		(row["employee"], row["time_key"], row["log_type"]) for row in normalized
	}
	keys = [
		{"employee": employee, "time": time_key, "log_type": log_type}
		for (employee, time_key, log_type) in found
		if (employee, time_key, log_type) in requested
	]
	return {"keys": keys, "checked": len(normalized), "existing": len(keys)}


def _assign_empty_attendance_device_ids(assignments) -> dict:
	rows = _parse_assignments(assignments)
	if not rows:
		return {"updated": [], "preserved": [], "failed": []}

	names = list(OrderedDict.fromkeys(row["employee"] for row in rows))
	current = {
		row.name: row
		for row in frappe.get_all(
			"Employee",
			filters={"name": ["in", names]},
			fields=["name", "attendance_device_id", "status"],
			limit_page_length=len(names),
		)
	}
	# Map existing device IDs to owners for conflict detection.
	codes = list({row["personnel_code"] for row in rows})
	owners = {}
	if codes:
		for row in frappe.get_all(
			"Employee",
			filters={"attendance_device_id": ["in", codes]},
			fields=["name", "attendance_device_id"],
			limit_page_length=len(codes) + 50,
		):
			owners[cstr(row.attendance_device_id).strip()] = row.name

	updated = []
	preserved = []
	failed = []
	for row in rows:
		employee = row["employee"]
		code = row["personnel_code"]
		found = current.get(employee)
		if not found:
			failed.append(
				{
					"employee": employee,
					"error": f"Employee {employee} does not exist.",
					"category": "Invalid Employee",
				}
			)
			continue
		existing = cstr(found.attendance_device_id).strip()
		if existing:
			if existing != code:
				failed.append(
					{
						"employee": employee,
						"error": (
							f"Employee {employee} already has Attendance Device ID "
							f"'{existing}'; refused to replace with '{code}'."
						),
						"category": "Conflicting attendance_device_id",
					}
				)
			else:
				preserved.append(employee)
			continue
		owner = owners.get(code)
		if owner and owner != employee:
			failed.append(
				{
					"employee": employee,
					"error": (
						f"Attendance Device ID '{code}' is already assigned to {owner}."
					),
					"category": "Conflicting attendance_device_id",
				}
			)
			continue
		if not frappe.has_permission("Employee", "write", employee):
			failed.append(
				{
					"employee": employee,
					"error": f"Not permitted to set Attendance Device ID for {employee}.",
					"category": "Permission error",
				}
			)
			continue
		frappe.db.set_value(
			"Employee",
			employee,
			"attendance_device_id",
			code,
			update_modified=True,
		)
		found.attendance_device_id = code
		owners[code] = employee
		updated.append(employee)
	return {"updated": updated, "preserved": preserved, "failed": failed}


def _authorize():
	if frappe.session.user in (None, "", "Guest"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	roles = set(frappe.get_roles())
	if not roles.intersection(IMPORT_ROLES):
		frappe.throw(
			_("Not permitted to import attendance device records."),
			frappe.PermissionError,
		)
	if not frappe.has_permission("Employee Checkin", "create"):
		frappe.throw(
			_("Not permitted to create Employee Checkin."),
			frappe.PermissionError,
		)


def _sanitize_device_id(value, label="device_id") -> str | None:
	text = cstr(value).strip()
	if not text:
		return None
	if len(text) > MAX_DEVICE_ID_LEN:
		frappe.throw(
			_("{0} is too long (max {1} characters).").format(label, MAX_DEVICE_ID_LEN),
			frappe.ValidationError,
		)
	if not _DEVICE_ID_RE.match(text):
		frappe.throw(
			_("{0} contains invalid characters.").format(label),
			frappe.ValidationError,
		)
	return text


def _parse_records(records, batch_location: str | None = None) -> list[dict]:
	records = _load_json_list(records, "records")
	if len(records) > MAX_RECORDS:
		frappe.throw(
			_("A device import batch accepts at most {0} records.").format(MAX_RECORDS),
			frappe.ValidationError,
		)
	parsed = []
	for index, raw in enumerate(records):
		if not isinstance(raw, dict):
			frappe.throw(_("Record {0} is not an object.").format(index + 1), frappe.ValidationError)
		row = {key: raw.get(key) for key in _RECORD_KEYS}
		# Per-record device_id is optional Location / Device ID only.
		row_device = _sanitize_device_id(row.get("device_id"), label=f"records[{index}].device_id")
		row["device_id"] = row_device or batch_location
		parsed.append(row)
	return parsed


def _parse_assignments(assignments) -> list[dict]:
	assignments = _load_json_list(assignments, "device_assignments")
	if len(assignments) > MAX_ASSIGNMENTS:
		frappe.throw(
			_("At most {0} device-id assignments are accepted per request.").format(MAX_ASSIGNMENTS),
			frappe.ValidationError,
		)
	out = []
	for index, raw in enumerate(assignments):
		if not isinstance(raw, dict):
			frappe.throw(_("Assignment {0} is not an object.").format(index + 1), frappe.ValidationError)
		employee = cstr(raw.get("employee")).strip()
		code = cstr(raw.get("personnel_code")).strip()
		if not employee or not code:
			continue
		if len(code) > MAX_DEVICE_ID_LEN:
			frappe.throw(_("Personnel code for {0} is too long.").format(employee), frappe.ValidationError)
		out.append({"employee": employee, "personnel_code": code})
	return out


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


def _insert_records(parsed: list[dict]) -> dict:
	created = []
	duplicates = []
	failed = []
	if not parsed:
		return {"created": created, "duplicates": duplicates, "failed": failed}

	employees = []
	for row in parsed:
		employee = cstr(row.get("employee")).strip()
		if employee and employee not in employees:
			employees.append(employee)

	employee_rows = {}
	if employees:
		for row in frappe.get_all(
			"Employee",
			filters={"name": ["in", employees]},
			fields=["name", "status", "company", "attendance_device_id"],
			limit_page_length=len(employees),
		):
			employee_rows[row.name] = row

	readable = _readable_employees(employees, employee_rows=employee_rows)

	normalized = []
	for row in parsed:
		employee = cstr(row.get("employee")).strip()
		log_type = cstr(row.get("log_type")).strip()
		try:
			when = get_datetime(row.get("time")).replace(microsecond=0)
		except Exception:
			failed.append(
				{
					"employee": employee,
					"time": cstr(row.get("time")),
					"log_type": log_type,
					"error": "Invalid timestamp.",
					"category": "Invalid timestamp",
				}
			)
			continue
		device_id = row.get("device_id")
		# Never store the employee's attendance_device_id as Location / Device ID.
		emp_code = cstr(employee_rows.get(employee) and employee_rows[employee].attendance_device_id).strip()
		if device_id and emp_code and device_id == emp_code:
			device_id = None
		normalized.append(
			{
				"employee": employee,
				"time": when,
				"time_key": when.strftime("%Y-%m-%d %H:%M:%S"),
				"log_type": log_type,
				"device_id": device_id,
				"skip_auto_attendance": 1 if cint(row.get("skip_auto_attendance")) else 0,
			}
		)

	existing = _existing_keys(normalized)
	_lock_employees(sorted({row["employee"] for row in normalized if row["employee"]}))
	existing = _existing_keys(normalized)
	for index, row in enumerate(normalized):
		key = (row["employee"], row["time_key"], row["log_type"])
		problem = _row_problem(row, employee_rows, readable)
		if problem:
			failed.append(problem)
			continue
		if key in existing:
			duplicates.append(
				{
					"employee": row["employee"],
					"time": row["time_key"],
					"log_type": row["log_type"],
					"name": existing[key],
				}
			)
			continue

		savepoint = f"device_checkin_{index}"
		frappe.db.savepoint(savepoint)
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Employee Checkin",
					"employee": row["employee"],
					"time": row["time"],
					"log_type": row["log_type"],
					"device_id": row["device_id"] or None,
					"skip_auto_attendance": row["skip_auto_attendance"],
				}
			)
			doc.insert()
		except Exception as exc:
			original = exc
			savepoint_intact = True
			try:
				frappe.db.rollback(save_point=savepoint)
			except Exception:
				savepoint_intact = False
				frappe.db.rollback()
			frappe.clear_messages()
			message = _exception_text(original)
			if _is_duplicate_error(message):
				duplicates.append(
					{
						"employee": row["employee"],
						"time": row["time_key"],
						"log_type": row["log_type"],
						"name": None,
					}
				)
				existing[key] = None
			else:
				failed.append(
					{
						"employee": row["employee"],
						"time": row["time_key"],
						"log_type": row["log_type"],
						"error": message,
						"category": _failure_category(message),
					}
				)
			if not savepoint_intact:
				created = [item for item in created if _key_exists(item)]
				return {"created": created, "duplicates": duplicates, "failed": failed}
		else:
			frappe.db.release_savepoint(savepoint)
			existing[key] = doc.name
			created.append(
				{
					"employee": row["employee"],
					"time": row["time_key"],
					"log_type": row["log_type"],
					"name": doc.name,
				}
			)

	return {"created": created, "duplicates": duplicates, "failed": failed}


def _row_problem(row, employee_rows, readable) -> dict | None:
	employee = row["employee"]
	base = {"employee": employee, "time": row["time_key"], "log_type": row["log_type"]}
	if not employee or employee not in employee_rows:
		return {**base, "error": f"Employee {employee or '-'} does not exist.", "category": "Invalid Employee"}
	if not readable.get(employee):
		return {
			**base,
			"error": f"Not permitted to import checkins for {employee}.",
			"category": "Permission error",
		}
	status = employee_rows[employee].status
	if status != "Active":
		return {**base, "error": f"Employee {employee} is not Active ({status}).", "category": "Inactive Employee"}
	if row["log_type"] not in _LOG_TYPES:
		return {**base, "error": "Invalid log_type. Expected IN or OUT.", "category": "Validation error"}
	return None


def _readable_employees(employees: list[str], employee_rows: dict | None = None) -> dict:
	"""Map employee name → whether the caller may read that Employee."""
	readable = {}
	rows = employee_rows
	if rows is None:
		rows = {}
		if employees:
			for row in frappe.get_all(
				"Employee",
				filters={"name": ["in", employees]},
				fields=["name"],
				limit_page_length=len(employees),
			):
				rows[row.name] = row
	for employee in employees:
		readable[employee] = bool(rows.get(employee)) and bool(
			frappe.has_permission("Employee", "read", employee)
		)
	return readable


def _existing_keys(rows: list[dict]) -> dict:
	employees = list({row["employee"] for row in rows if row["employee"]})
	times = list({row["time"] for row in rows})
	found = {}
	if not employees or not times:
		return found
	for existing in frappe.db.sql(
		"""
		select name, employee, time, log_type
		from `tabEmployee Checkin`
		where employee in %(employees)s
			and time in %(times)s
		""",
		{"employees": employees, "times": times},
		as_dict=True,
	):
		when = get_datetime(existing.time).replace(microsecond=0)
		key = (existing.employee, when.strftime("%Y-%m-%d %H:%M:%S"), cstr(existing.log_type).strip())
		found[key] = existing.name
	return found


def _lock_employees(employees: list[str]) -> list[str]:
	locked = []
	for employee in employees:
		name = f"espad_ck_{employee}"[:64]
		got = frappe.db.sql("select get_lock(%s, 20)", name)
		if not got or int(got[0][0] or 0) != 1:
			_unlock(locked)
			frappe.throw(
				_("Could not lock Employee {0} for device import. Retry this batch.").format(employee),
				frappe.ValidationError,
			)
		locked.append(name)
	if locked:
		names = list(locked)

		def release(held=names):
			_unlock(held)

		frappe.db.after_commit.add(release)
		frappe.db.after_rollback.add(release)
	return locked


def _key_exists(item: dict) -> bool:
	return bool(
		frappe.db.exists(
			"Employee Checkin",
			{"employee": item["employee"], "time": item["time"], "log_type": item["log_type"]},
		)
	)


def _unlock(locks: list[str]):
	for name in locks:
		frappe.db.sql("select release_lock(%s)", name)


def _exception_text(exc: Exception) -> str:
	text = cstr(getattr(exc, "message", None) or exc)
	return " ".join(text.split())[:1000] or exc.__class__.__name__


def _is_duplicate_error(message: str) -> bool:
	lower = message.lower()
	return (
		"already has a log" in lower
		or "duplicate" in lower
		or "record has changed since last read" in lower
		or "1062" in lower
	)


def _failure_category(message: str) -> str:
	lower = message.lower()
	if "not active" in lower or "inactive" in lower:
		return "Inactive Employee"
	if "does not exist" in lower:
		return "Invalid Employee"
	if "not permitted" in lower or "permission" in lower:
		return "Permission error"
	if "timestamp" in lower or "datetime" in lower:
		return "Invalid timestamp"
	if "تردد دستی" in message or "حداکثر مجاز" in message:
		return "Manual check-in limit"
	return "Validation error"
