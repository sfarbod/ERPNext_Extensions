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

LEGACY_SERVER_SCRIPT = "# Employee Checkin - Outside Shift Day Limit"
LEGACY_SHA256 = "0f5676dcf01eea441343adee8043eab76a377a625602beae9b4d6438e3042c2e"
RETIRED_MARK = "RETIRED BY erpnext_extensions.extentionhrms.employee_checkin_manual_limit"
IMPORT_ROLE = "Attendance Device Importer"
CLIENT_SCRIPT_NAME = "Employee Checkin Excel Import V1.0.1"


def after_migrate():
	ensure_device_import_setup()


def ensure_device_import_setup() -> dict:
	result = {
		"role": ensure_import_role(),
		"server_script": retire_legacy_server_script(),
		"client_script": sync_client_script(),
		"duplicate_index": ensure_duplicate_index(),
	}
	return result


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
		frappe.throw(
			f"Cannot add the checkin duplicate key because {duplicates} duplicate groups already exist."
		)
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
