# Copyright (c) 2026, ERPNext Extensions contributors
"""Desk navigation helpers spanning Frappe Workspace Sidebar → Sidebar migration.

Frappe 16.52 retires ``Workspace Sidebar`` as a runtime store (inert archive) and
introduces ``Sidebar`` / ``Custom Sidebar``. Migrate-time writes to the archive
remain allowed under ``frappe.flags.in_migrate``; runtime navigation reads
``Sidebar``.

These helpers keep after_migrate patches idempotent on both generations without
globally disabling Frappe guards.
"""

from __future__ import annotations

from typing import Any

import frappe


def create_workspace_sidebars_if_available() -> bool:
	"""Call legacy factory when present. Returns True if invoked."""
	try:
		from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import (
			create_workspace_sidebar_for_workspaces,
		)
	except ImportError:
		return False
	create_workspace_sidebar_for_workspaces()
	return True


def workspace_sidebar_writable() -> bool:
	"""True when Workspace Sidebar still accepts writes (pre-archive or migrate)."""
	if not frappe.db.exists("DocType", "Workspace Sidebar"):
		return False
	# Archive validate allows migrate/patch/install; refuse otherwise on 16.52+.
	if any(frappe.flags.get(f) for f in ("in_migrate", "in_patch", "in_install")):
		return True
	# Older Frappe: class still behaves as DeskViews editor.
	try:
		from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import WorkspaceSidebar

		return not hasattr(WorkspaceSidebar, "refuse_new_rows")
	except Exception:
		return False


def has_modern_sidebar() -> bool:
	return bool(frappe.db.exists("DocType", "Sidebar"))


def _row_get(row: Any, key: str, default=None):
	if row is None:
		return default
	if hasattr(row, "get"):
		val = row.get(key)
		return default if val is None else val
	return getattr(row, key, default)


def sync_module_sidebar_items(
	module: str,
	items: list[dict],
	*,
	title: str | None = None,
	header_icon: str | None = None,
	app: str | None = None,
	standard: int = 0,
) -> None:
	"""Idempotently write navigation items to legacy and/or modern sidebar stores."""
	title = title or module
	if workspace_sidebar_writable():
		_sync_workspace_sidebar(
			module,
			items,
			title=title,
			header_icon=header_icon,
			app=app,
			standard=standard,
		)
	if has_modern_sidebar():
		_sync_modern_sidebar(
			module,
			items,
			title=title,
			header_icon=header_icon,
			app=app,
			standard=standard,
		)


def _sync_workspace_sidebar(
	module: str,
	items: list[dict],
	*,
	title: str,
	header_icon: str | None,
	app: str | None,
	standard: int,
) -> None:
	meta = frappe.get_meta("Workspace Sidebar")
	is_new = not frappe.db.exists("Workspace Sidebar", title)
	sb = frappe.new_doc("Workspace Sidebar") if is_new else frappe.get_doc("Workspace Sidebar", title)
	if meta.has_field("title"):
		sb.title = title
	if meta.has_field("module"):
		sb.module = module
	if meta.has_field("header_icon") and header_icon is not None:
		sb.header_icon = header_icon
	if meta.has_field("app") and app is not None:
		sb.app = app
	if meta.has_field("standard"):
		sb.standard = standard
	if meta.has_field("for_user"):
		sb.for_user = None
	sb.set("items", [])
	for idx, row in enumerate(items):
		payload = dict(row)
		payload["idx"] = idx
		sb.append("items", payload)
	sb.save(ignore_permissions=True)


def _sync_modern_sidebar(
	module: str,
	items: list[dict],
	*,
	title: str,
	header_icon: str | None,
	app: str | None,
	standard: int,
) -> None:
	"""Write ``Sidebar`` rows used by Frappe 16.52+ module navigation."""
	from frappe.desk.doctype.sidebar.sidebar import SIDEBAR_ITEM_FIELDS, get_sidebar

	doc = get_sidebar(module)
	is_new = doc is None
	if is_new:
		doc = frappe.new_doc("Sidebar")
		doc.module = module
		doc.title = title
	else:
		# Preserve an existing standard ERPNext-owned sidebar title/icon when
		# we only append module-local navigation (Payments → Accounts).
		if not doc.title:
			doc.title = title

	if header_icon and (is_new or not doc.header_icon):
		doc.header_icon = header_icon
	if app and (is_new or not doc.app):
		doc.app = app
	# Never flip a shipped standard sidebar to custom=standard without a file
	# path; non-standard site rows are fine for extension modules.
	if is_new:
		doc.standard = cint_safe(standard)

	doc.set("items", [])
	for idx, row in enumerate(items):
		payload = {field: row.get(field) for field in SIDEBAR_ITEM_FIELDS if field in row}
		payload["idx"] = idx
		# label is required for Section Break / Spacer readability
		if "label" in row:
			payload["label"] = row["label"]
		doc.append("items", payload)
	doc.save(ignore_permissions=True)


def cint_safe(val) -> int:
	try:
		return int(val or 0)
	except (TypeError, ValueError):
		return 0


def get_payments_navigation_targets() -> list[str]:
	"""Module names that may host Cheque/Guarantee Payments navigation."""
	targets = []
	if has_modern_sidebar():
		# ERPNext ships Accounts; legacy Workspace Sidebar used Payments.
		for name in ("Accounts", "Payments"):
			if frappe.db.exists("Sidebar", name) or frappe.db.exists(
				"Module Def", name
			):
				targets.append(name)
	if "Payments" not in targets and frappe.db.exists("DocType", "Workspace Sidebar"):
		targets.append("Payments")
	return targets or ["Accounts", "Payments"]
