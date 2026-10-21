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

# Stock HRMS omits On Duty; Excel mission days require it on the Select field.
ATTENDANCE_STATUS_OPTIONS = (
	"Present\nAbsent\nOn Leave\nHalf Day\nWork From Home\nOn Duty"
)


def ensure_attendance_excel_import_setup() -> dict:
	return {
		"client_script": sync_client_script(),
		"status_options": ensure_on_duty_status_option(),
	}


def ensure_on_duty_status_option() -> str:
	"""Idempotently add ``On Duty`` to Attendance.status Select options."""
	name = "Attendance-status-options"
	if frappe.db.exists("Property Setter", name):
		current = frappe.db.get_value("Property Setter", name, "value") or ""
		if "On Duty" in current.split("\n"):
			return "unchanged"
		frappe.db.set_value("Property Setter", name, "value", ATTENDANCE_STATUS_OPTIONS)
		frappe.clear_cache(doctype="Attendance")
		return "updated"
	# Also accept an existing differently-named options setter.
	existing = frappe.get_all(
		"Property Setter",
		filters={"doc_type": "Attendance", "field_name": "status", "property": "options"},
		fields=["name", "value"],
		limit=1,
	)
	if existing:
		if "On Duty" in (existing[0].value or "").split("\n"):
			return "unchanged"
		frappe.db.set_value("Property Setter", existing[0].name, "value", ATTENDANCE_STATUS_OPTIONS)
		frappe.clear_cache(doctype="Attendance")
		return "updated"
	doc = frappe.get_doc(
		{
			"doctype": "Property Setter",
			"doctype_or_field": "DocField",
			"doc_type": "Attendance",
			"field_name": "status",
			"property": "options",
			"property_type": "Text",
			"value": ATTENDANCE_STATUS_OPTIONS,
			"name": name,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.clear_cache(doctype="Attendance")
	return "created"


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
