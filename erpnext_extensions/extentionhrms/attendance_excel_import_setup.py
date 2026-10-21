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
	}


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
