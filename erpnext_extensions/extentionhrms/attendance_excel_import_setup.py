# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Keep the Attendance Excel importer Client Script in sync with the app file."""

from __future__ import annotations

import os

import frappe

CLIENT_SCRIPT_NAME = "Employee Attendance Excel Import"
CLIENT_SCRIPT_PATH = os.path.join(
	"extentionhrms",
	"public",
	"js",
	"attendance_excel_import.js",
)

# Stock HRMS statuses — On Duty was briefly added for a rejected mission rule and must be removed.
STOCK_ATTENDANCE_STATUS_OPTIONS = "Present\nAbsent\nOn Leave\nHalf Day\nWork From Home"


def ensure_attendance_excel_import_setup() -> dict:
	return {
		"client_script": sync_client_script(),
		"status_options": remove_on_duty_status_option(),
		"ssa_default": ensure_source_shift_assignment_default_clear(),
		"ssa_invalid_links": repair_invalid_source_shift_assignment_links(),
	}


def ensure_source_shift_assignment_default_clear() -> str:
	"""Clear Attendance.custom_source_shift_assignment default when it is ``0``.

	A Link default of ``0`` is not a Shift Assignment name and breaks insert /
	submit with LinkValidationError. Empty default preserves optional Link
	semantics.
	"""
	name = frappe.db.get_value(
		"Custom Field",
		{"dt": "Attendance", "fieldname": "custom_source_shift_assignment"},
		"name",
	)
	if not name:
		return "missing"
	current = frappe.db.get_value("Custom Field", name, "default")
	if current in (None, ""):
		return "already_empty"
	if str(current).strip() != "0":
		return f"left_as_{current}"
	frappe.db.set_value("Custom Field", name, "default", "", update_modified=False)
	frappe.clear_cache(doctype="Attendance")
	return "cleared_zero_default"


def repair_invalid_source_shift_assignment_links() -> dict:
	"""Null out Attendance.custom_source_shift_assignment when stored as ``0``.

	Does not cancel or amend documents. Valid Shift Assignment names are left
	unchanged. Uses a direct DB update so submitted rows can be repaired without
	bypassing Link validation on subsequent saves.
	"""
	if not frappe.db.has_column("Attendance", "custom_source_shift_assignment"):
		return {"updated": 0, "status": "column_missing"}
	before = frappe.db.sql(
		"""
		select count(*) from `tabAttendance`
		where custom_source_shift_assignment = '0'
		"""
	)[0][0]
	if not before:
		return {"updated": 0, "status": "clean"}
	frappe.db.sql(
		"""
		update `tabAttendance`
		set custom_source_shift_assignment = NULL
		where custom_source_shift_assignment = '0'
		"""
	)
	frappe.clear_cache(doctype="Attendance")
	return {"updated": int(before), "status": "repaired"}


def remove_on_duty_status_option() -> str:
	"""Remove importer-only ``On Duty`` Property Setter if present; restore stock options."""
	changed = False
	for row in frappe.get_all(
		"Property Setter",
		filters={"doc_type": "Attendance", "field_name": "status", "property": "options"},
		fields=["name", "value"],
	):
		value = row.value or ""
		if "On Duty" not in value.split("\n"):
			continue
		# If the setter exists solely to inject On Duty, delete it so meta falls back to stock.
		if value.strip() == STOCK_ATTENDANCE_STATUS_OPTIONS + "\nOn Duty" or value.strip().endswith(
			"On Duty"
		):
			frappe.delete_doc("Property Setter", row.name, force=1, ignore_permissions=True)
			changed = True
		else:
			cleaned = "\n".join(line for line in value.split("\n") if line.strip() != "On Duty")
			frappe.db.set_value("Property Setter", row.name, "value", cleaned)
			changed = True
	if changed:
		frappe.clear_cache(doctype="Attendance")
		return "removed"
	return "unchanged"


def sync_client_script() -> str:
	path = os.path.join(frappe.get_app_path("erpnext_extensions"), CLIENT_SCRIPT_PATH)
	with open(path, encoding="utf-8") as handle:
		content = handle.read()
	if not frappe.db.exists("Client Script", CLIENT_SCRIPT_NAME):
		doc = frappe.get_doc(
			{
				"doctype": "Client Script",
				"name": CLIENT_SCRIPT_NAME,
				"dt": "Attendance",
				"view": "List",
				"enabled": 1,
				"module": "HR",
				"script": content,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.clear_cache(doctype="Client Script")
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
	frappe.clear_cache(doctype="Client Script")
	return "updated"
