# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Make the device-import policy survive migrate and a database restore.

The manual-limit Server Script cannot see a server-side import context, because
Frappe runs it inside ``safe_exec`` with an empty ``frappe.flags``. After this
setup:

* Role ``Attendance Device Importer`` exists.
* The known legacy Server Script is disabled so the policy is not applied twice.
* Client Script ``Employee Checkin Excel Import V1.0.1`` is rewritten from the
  version-controlled JavaScript file.

A restore that brings the original Server Script back is corrected on the next
``after_migrate``, but only when the script still matches the known hash or
already carries the retirement marker. A different customization is left in
place and reported.
"""

from __future__ import annotations

import hashlib
import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from erpnext_extensions.extentionhrms.employee_checkin_manual_limit import (
	DEFAULT_MAX_MANUAL_CHECKINS,
	ENABLE_FIELD,
	MAX_FIELD,
	clear_manual_checkin_policy_cache,
)

LEGACY_SERVER_SCRIPT = "# Employee Checkin - Outside Shift Day Limit"
LEGACY_SHA256 = "0f5676dcf01eea441343adee8043eab76a377a625602beae9b4d6438e3042c2e"
RETIRED_MARK = "RETIRED BY erpnext_extensions.extentionhrms.employee_checkin_manual_limit"
IMPORT_ROLE = "Attendance Device Importer"
CLIENT_SCRIPT_NAME = "Employee Checkin Excel Import V1.0.1"

# Frappe's Meta.sort_fields() walks past Tab Breaks when placing a custom
# Section Break after the last field of a tab, so a Section after
# ``allow_geolocation_tracking`` lands under Tenure. A Heading field does not
# trigger that rule and stays on Shift and Attendance.
HR_SETTINGS_HEADING = "espad_manual_checkin_heading"
OBSOLETE_HR_SETTINGS_FIELDS = ("espad_manual_checkin_section",)
HR_SETTINGS_CUSTOM_FIELDS = {
	"HR Settings": [
		{
			"fieldname": HR_SETTINGS_HEADING,
			"label": "Manual Attendance Checkin Policy",
			"fieldtype": "Heading",
			"insert_after": "allow_geolocation_tracking",
		},
		{
			"fieldname": ENABLE_FIELD,
			"label": "Enable Manual Checkin Limit",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": HR_SETTINGS_HEADING,
			"permlevel": 0,
			"description": (
				"Cap historical manual Employee Checkins per employee. The counting "
				"period is Jalali: from the 26th of one month through the 25th of the "
				"next. Check-ins dated today are not counted. Authorized attendance "
				"device imports (Excel importer or native add_log with the Attendance "
				"Device Importer role) do not consume this quota. Only a System "
				"Manager can change this policy."
			),
		},
		{
			"fieldname": MAX_FIELD,
			"label": "Maximum Manual Checkins per Period",
			"fieldtype": "Int",
			"default": str(DEFAULT_MAX_MANUAL_CHECKINS),
			"insert_after": ENABLE_FIELD,
			"permlevel": 0,
			"mandatory_depends_on": f"eval:doc.{ENABLE_FIELD}",
			"depends_on": f"eval:doc.{ENABLE_FIELD}",
			"description": (
				f"Default {DEFAULT_MAX_MANUAL_CHECKINS}. Maximum historical manual "
				"check-ins allowed per employee in each Jalali 26–25 period while "
				"the limit is enabled."
			),
		},
	]
}


def after_migrate():
	ensure_device_import_setup()


def ensure_device_import_setup() -> dict:
	result = {
		"role": ensure_import_role(),
		"hr_settings_fields": ensure_hr_settings_fields(),
		"hr_settings_permlevel": ensure_hr_settings_permlevel(),
		"server_script": retire_legacy_server_script(),
		"client_script": sync_client_script(),
		"duplicate_index": ensure_duplicate_index(),
	}
	clear_manual_checkin_policy_cache()
	return result


def ensure_hr_settings_fields() -> str:
	"""Install/repair Manual Attendance Checkin Policy fields on HR Settings.

	Removes the obsolete Section Break that Desk rendered under the Tenure tab
	and recreates a Heading + fields after ``allow_geolocation_tracking``.
	"""
	_remove_obsolete_hr_settings_fields()
	create_custom_fields(HR_SETTINGS_CUSTOM_FIELDS, ignore_validate=True, update=True)
	_repair_hr_settings_field_placement()
	_ensure_hr_settings_defaults()
	frappe.clear_cache(doctype="HR Settings")
	return "ensured"


def _remove_obsolete_hr_settings_fields() -> None:
	for fieldname in OBSOLETE_HR_SETTINGS_FIELDS:
		name = f"HR Settings-{fieldname}"
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=1)


def _ensure_hr_settings_defaults() -> None:
	def _stored(fieldname: str) -> bool:
		return bool(
			frappe.db.sql(
				"select 1 from tabSingles where doctype=%s and field=%s limit 1",
				("HR Settings", fieldname),
			)
		)

	if not _stored(ENABLE_FIELD):
		frappe.db.set_single_value("HR Settings", ENABLE_FIELD, 1, update_modified=False)
	if not _stored(MAX_FIELD):
		frappe.db.set_single_value(
			"HR Settings", MAX_FIELD, DEFAULT_MAX_MANUAL_CHECKINS, update_modified=False
		)
	else:
		current_max = frappe.db.get_single_value("HR Settings", MAX_FIELD, cache=False)
		if current_max in (None, "") or int(current_max or 0) < 1:
			frappe.db.set_single_value(
				"HR Settings", MAX_FIELD, DEFAULT_MAX_MANUAL_CHECKINS, update_modified=False
			)


def _repair_hr_settings_field_placement() -> None:
	"""Keep Heading/fields anchored after geolocation with permlevel 0."""
	specs = [
		(HR_SETTINGS_HEADING, "allow_geolocation_tracking", "Manual Attendance Checkin Policy"),
		(ENABLE_FIELD, HR_SETTINGS_HEADING, "Enable Manual Checkin Limit"),
		(MAX_FIELD, ENABLE_FIELD, "Maximum Manual Checkins per Period"),
	]
	for fieldname, insert_after, label in specs:
		name = f"HR Settings-{fieldname}"
		if not frappe.db.exists("Custom Field", name):
			continue
		doc = frappe.get_doc("Custom Field", name)
		doc.insert_after = insert_after
		doc.label = label
		if fieldname in (ENABLE_FIELD, MAX_FIELD):
			doc.permlevel = 0
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)


def ensure_hr_settings_permlevel() -> str:
	"""Ensure System Manager can open/edit HR Settings when Custom DocPerm is active.

	This site already uses Custom DocPerm for HR Settings. A prior setup step
	added only permlevel-1 rights for System Manager, which can hide ordinary
	form fields. Policy fields are now permlevel 0; write access is still
	restricted to System Manager in ``validate_hr_settings_manual_limit``.
	"""
	# Remove the obsolete permlevel-1-only Custom DocPerm we may have created.
	obsolete = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": "HR Settings", "role": "System Manager", "permlevel": 1},
		"name",
	)
	if obsolete:
		frappe.delete_doc("Custom DocPerm", obsolete, ignore_permissions=True, force=1)

	existing = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": "HR Settings", "role": "System Manager", "permlevel": 0},
		"name",
	)
	values = {
		"read": 1,
		"write": 1,
		"create": 1,
		"delete": 0,
		"share": 1,
		"email": 1,
		"print": 1,
	}
	if existing:
		frappe.db.set_value("Custom DocPerm", existing, values, update_modified=False)
		frappe.clear_cache(doctype="HR Settings")
		return "updated"
	doc = frappe.get_doc(
		{
			"doctype": "Custom DocPerm",
			"parent": "HR Settings",
			"parenttype": "DocType",
			"parentfield": "permissions",
			"role": "System Manager",
			"permlevel": 0,
			**values,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.clear_cache(doctype="HR Settings")
	return "created"


def ensure_duplicate_index() -> str:
	"""Stop two concurrent imports from inserting the same punch.

	HRMS rejects duplicates in Python. Two transactions can both pass that
	check. A unique key makes the second insert fail inside the database.
	"""
	constraint = "espad_checkin_emp_time_log"
	exists = frappe.db.sql(
		"""
		select CONSTRAINT_NAME from information_schema.TABLE_CONSTRAINTS
		where table_schema = database()
			and table_name = 'tabEmployee Checkin'
			and constraint_type = 'UNIQUE'
			and CONSTRAINT_NAME = %s
		""",
		constraint,
	)
	if exists:
		return "exists"
	duplicates = frappe.db.sql(
		"""
		select count(*) from (
			select employee, time, log_type
			from `tabEmployee Checkin`
			group by employee, time, log_type
			having count(*) > 1
		) t
		"""
	)[0][0]
	if duplicates:
		# Do not delete rows and do not abort migrate. Imports still reject
		# duplicates in application code; the index is added only on a clean table.
		frappe.log_error(
			title="Employee Checkin duplicate index skipped",
			message=(
				f"{duplicates} Employee Checkin groups share employee, time and log type. "
				"The unique index was not created. No rows were deleted."
			),
		)
		return "skipped_duplicates"
	frappe.db.add_unique("Employee Checkin", ["employee", "time", "log_type"], constraint)
	return "created"


def ensure_import_role() -> str:
	if frappe.db.exists("Role", IMPORT_ROLE):
		return "exists"
	role = frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": IMPORT_ROLE,
			"desk_access": 1,
			"is_custom": 1,
			"disabled": 0,
		}
	)
	role.insert(ignore_permissions=True)
	return "created"


def retire_legacy_server_script() -> str:
	if not frappe.db.exists("Server Script", LEGACY_SERVER_SCRIPT):
		return "missing"
	script = frappe.db.get_value("Server Script", LEGACY_SERVER_SCRIPT, "script") or ""
	disabled = frappe.db.get_value("Server Script", LEGACY_SERVER_SCRIPT, "disabled")
	digest = hashlib.sha256(script.encode()).hexdigest()
	known = digest == LEGACY_SHA256 or RETIRED_MARK in script
	if not known:
		frappe.log_error(
			title="Employee Checkin manual-limit script was not retired",
			message=(
				f"{LEGACY_SERVER_SCRIPT} does not match the known production script "
				f"(sha256 {digest}). It was left unchanged so a local customization "
				"is not disabled automatically. The application hook still enforces "
				"the same limit; disable the database script before using device import."
			),
		)
		return "hash_mismatch"
	if disabled and RETIRED_MARK in script:
		_clear_server_script_cache()
		return "already"
	if RETIRED_MARK not in script:
		script = (
			f"# {RETIRED_MARK}\n"
			"# This database script stays disabled. The policy is enforced by\n"
			"# erpnext_extensions.extentionhrms.employee_checkin_manual_limit.\n\n"
			+ script
		)
		frappe.db.set_value(
			"Server Script",
			LEGACY_SERVER_SCRIPT,
			"script",
			script,
			update_modified=False,
		)
	frappe.db.set_value(
		"Server Script",
		LEGACY_SERVER_SCRIPT,
		"disabled",
		1,
		update_modified=False,
	)
	_clear_server_script_cache()
	return "disabled"


def sync_client_script() -> str:
	path = os.path.join(
		frappe.get_app_path("erpnext_extensions"),
		"extentionhrms",
		"public",
		"js",
		"employee_checkin_excel_import.js",
	)
	with open(path, encoding="utf-8") as handle:
		content = handle.read()
	if not frappe.db.exists("Client Script", CLIENT_SCRIPT_NAME):
		doc = frappe.get_doc(
			{
				"doctype": "Client Script",
				"name": CLIENT_SCRIPT_NAME,
				"dt": "Employee Checkin",
				"view": "List",
				"enabled": 1,
				"module": "HR",
				"script": content,
			}
		)
		doc.insert(ignore_permissions=True)
		_clear_client_script_cache()
		return "created"
	current, enabled = frappe.db.get_value(
		"Client Script", CLIENT_SCRIPT_NAME, ["script", "enabled"]
	)
	if current == content and enabled:
		return "unchanged"
	frappe.db.set_value(
		"Client Script",
		CLIENT_SCRIPT_NAME,
		{"script": content, "enabled": 1},
		update_modified=True,
	)
	_clear_client_script_cache()
	return "updated"


def _clear_server_script_cache():
	try:
		frappe.client_cache.delete_value("server_script_map")
	except Exception:
		frappe.clear_cache()


def _clear_client_script_cache():
	frappe.clear_cache(doctype="Client Script")
