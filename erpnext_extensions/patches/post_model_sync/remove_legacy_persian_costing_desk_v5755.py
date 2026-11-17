# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Permanently remove obsolete Persian Costing Desk navigation (v5.7.55).

Deletes exclusive ``بهای تمام شده`` Desk records when present. Preserves the
English ``Costing`` Module Def, Workspace, Workspace Sidebar, Sidebar, and
Desktop Icon. Idempotent and safe when legacy records are already absent.
"""

from __future__ import annotations

import frappe
from frappe.desk.doctype.desktop_icon.desktop_icon import clear_every_users_desktop_icons

LEGACY = "بهای تمام شده"
ENGLISH = "Costing"
APP = "erpnext_extensions"


def _safe_delete(doctype: str, name: str) -> bool:
	"""Delete ``name`` when it exists and is not the English Costing record."""
	if not name or name == ENGLISH:
		return False
	if not frappe.db.exists(doctype, name):
		return False
	# Never delete shared English Costing navigation.
	if name == ENGLISH:
		return False
	frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
	return True


def _delete_exclusive_desktop_icons() -> list[str]:
	deleted: list[str] = []
	if _safe_delete("Desktop Icon", LEGACY):
		deleted.append(LEGACY)

	# Stray tiles that still point at the Persian label / workspace only.
	for row in frappe.get_all(
		"Desktop Icon",
		filters={"app": APP},
		fields=["name", "label", "link_to"],
	):
		if row.name == ENGLISH:
			continue
		if row.label == LEGACY or row.link_to == LEGACY:
			if _safe_delete("Desktop Icon", row.name):
				deleted.append(row.name)
	return deleted


def _delete_legacy_module_def() -> bool:
	"""Remove Persian Module Def only when no DocTypes still belong to it."""
	if not frappe.db.exists("Module Def", LEGACY):
		return False
	if frappe.db.exists("DocType", {"module": LEGACY}):
		frappe.logger("erpnext_extensions").warning(
			"Skipping Module Def %s deletion: DocTypes still reference it", LEGACY
		)
		return False
	frappe.delete_doc("Module Def", LEGACY, force=1, ignore_permissions=True)
	return True


def execute():
	frappe.flags.in_patch = True
	deleted = {
		"workspace": _safe_delete("Workspace", LEGACY),
		"workspace_sidebar": _safe_delete("Workspace Sidebar", LEGACY),
		"sidebar": _safe_delete("Sidebar", LEGACY),
		"desktop_icons": _delete_exclusive_desktop_icons(),
		"module_def": _delete_legacy_module_def(),
	}

	# Clear Desk caches so the Persian tile disappears immediately.
	clear_every_users_desktop_icons()
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")
	frappe.db.commit()
	return deleted


def ensure_now():
	"""Diagnostic helper for Development verification."""
	result = execute()
	return {
		"deleted": result,
		"english_workspace": bool(frappe.db.exists("Workspace", ENGLISH)),
		"english_desktop_icon": bool(frappe.db.exists("Desktop Icon", ENGLISH)),
		"english_sidebar": bool(frappe.db.exists("Sidebar", ENGLISH)),
		"english_module_def": bool(frappe.db.exists("Module Def", ENGLISH)),
		"legacy_workspace": bool(frappe.db.exists("Workspace", LEGACY)),
		"legacy_desktop_icon": bool(frappe.db.exists("Desktop Icon", LEGACY)),
		"legacy_sidebar": bool(frappe.db.exists("Sidebar", LEGACY)),
		"legacy_module_def": bool(frappe.db.exists("Module Def", LEGACY)),
	}
