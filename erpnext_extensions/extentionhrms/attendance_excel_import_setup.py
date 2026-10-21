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


def ensure_attendance_excel_import_setup() -> dict:
	return {"client_script": sync_client_script()}


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
