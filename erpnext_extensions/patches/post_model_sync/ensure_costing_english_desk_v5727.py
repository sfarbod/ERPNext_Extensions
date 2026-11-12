# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Migrate Costing Desk navigation from Persian بهای تمام شده to English Costing."""

from __future__ import annotations

import json

import frappe
from frappe.desk.doctype.desktop_icon.desktop_icon import clear_every_users_desktop_icons

from frappe.modules.utils import get_doc_path
from frappe.modules.import_file import import_file_by_path

from erpnext_extensions.desk_navigation import (
	assert_sidebar_link_targets_exist,
	sync_module_sidebar_items,
)

APP = "erpnext_extensions"
MODULE = "Costing"
DISPLAY = "Costing"
LEGACY_DISPLAY = "بهای تمام شده"
ICON = "calculator"
BG_COLOR = "blue"

# DocTypes referenced by Costing Workspace Sidebar / Sidebar Link To rows.
# Keep in sync with _SIDEBAR_ITEMS DocType targets.
REQUIRED_COSTING_DOCTYPES = (
	"Finished Goods Cost Allocation Settings",
	"Monthly FG Cost Calculation",
	"MFG Cost Expense Pool",
	"MFG Cost GL Line",
	"MFG Cost Batch Allocation",
	"MFG Cost Batch Expense Allocation",
	"MFG Cost Product Allocation",
	"MFG Cost Calculation Revision",
)

DESK_ROLES = [
	"System Manager",
	"Accounts Manager",
	"Accounts User",
	"Manufacturing Manager",
	"Auditor",
]

_SIDEBAR_ITEMS = [
	{"type": "Link", "label": "Home", "link_type": "Workspace", "link_to": DISPLAY, "icon": "home", "child": 0},
	{"type": "Section Break", "label": "Settings", "link_type": "DocType", "icon": "settings", "child": 0},
	{
		"type": "Link",
		"label": "Cost Allocation Settings",
		"link_type": "DocType",
		"link_to": "Finished Goods Cost Allocation Settings",
		"icon": "settings",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Account and Dimension Rules",
		"link_type": "DocType",
		"link_to": "Finished Goods Cost Allocation Settings",
		"icon": "list",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Item Primary UOMs",
		"link_type": "DocType",
		"link_to": "Finished Goods Cost Allocation Settings",
		"icon": "package",
		"child": 1,
	},
	{"type": "Section Break", "label": "Monthly Calculations", "link_type": "DocType", "icon": "calculator", "child": 0},
	{
		"type": "Link",
		"label": "Monthly FG Cost Calculations",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "file-plus",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Calculation Status",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "alert-triangle",
		"child": 1,
	},
	{"type": "Section Break", "label": "Reports & Details", "link_type": "DocType", "icon": "bar-chart-2", "child": 0},
	{
		"type": "Link",
		"label": "Expense Pools",
		"link_type": "DocType",
		"link_to": "MFG Cost Expense Pool",
		"icon": "layers",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "GL Entry Details",
		"link_type": "DocType",
		"link_to": "MFG Cost GL Line",
		"icon": "book-open",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Production Batches",
		"link_type": "DocType",
		"link_to": "MFG Cost Batch Allocation",
		"icon": "package",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Batch × Expense Pool Allocations",
		"link_type": "DocType",
		"link_to": "MFG Cost Batch Expense Allocation",
		"icon": "git-merge",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Product Details",
		"link_type": "DocType",
		"link_to": "MFG Cost Product Allocation",
		"icon": "box",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "UOM Exceptions",
		"link_type": "DocType",
		"link_to": "MFG Cost Product Allocation",
		"icon": "alert-circle",
		"child": 1,
	},
	{"type": "Section Break", "label": "Management & History", "link_type": "DocType", "icon": "archive", "child": 0},
	{
		"type": "Link",
		"label": "Calculation Revisions",
		"link_type": "DocType",
		"link_to": "MFG Cost Calculation Revision",
		"icon": "git-branch",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Calculation History",
		"link_type": "DocType",
		"link_to": "MFG Cost Calculation Revision",
		"icon": "history",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "Lock / Unlock Management",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "lock",
		"child": 1,
	},
]

_WORKSPACE_CONTENT = [
	{
		"id": "costing_hdr",
		"type": "header",
		"data": {"text": '<span class="h4"><b>Costing</b></span>', "col": 12},
	},
	{
		"id": "costing_intro",
		"type": "paragraph",
		"data": {
			"text": "Monthly finished-goods cost allocation based on solar calendar months.",
			"col": 12,
		},
	},
	{"id": "card_settings", "type": "card", "data": {"card_name": "Settings", "col": 6}},
	{"id": "card_calc", "type": "card", "data": {"card_name": "Monthly Calculations", "col": 6}},
	{"id": "card_reports", "type": "card", "data": {"card_name": "Reports & Details", "col": 6}},
	{"id": "card_mgmt", "type": "card", "data": {"card_name": "Management & History", "col": 6}},
]


def _workspace_content_json() -> str:
	return json.dumps(_WORKSPACE_CONTENT, separators=(",", ":"))


def _rename_or_absorb(doctype: str, legacy: str, target: str) -> str:
	"""Return canonical document name for ``target``, migrating ``legacy`` when present."""
	if legacy == target:
		return target if frappe.db.exists(doctype, target) else ""
	if frappe.db.exists(doctype, legacy):
		if frappe.db.exists(doctype, target):
			legacy_doc = frappe.get_doc(doctype, legacy)
			target_doc = frappe.get_doc(doctype, target)
			keep_target = True
			if doctype == "Desktop Icon":
				keep_target = not int(target_doc.hidden or 0) or int(legacy_doc.hidden or 0) == 0
			if keep_target:
				frappe.delete_doc(doctype, legacy, force=1, ignore_permissions=True)
				return target
			frappe.delete_doc(doctype, target, force=1, ignore_permissions=True)
		frappe.rename_doc(doctype, legacy, target, force=True, merge=False)
	return target if frappe.db.exists(doctype, target) else ""


def _hide_doc_when_exists(doctype: str, name: str) -> None:
	if not name or name == DISPLAY or not frappe.db.exists(doctype, name):
		return
	meta = frappe.get_meta(doctype)
	if meta.has_field("hidden"):
		frappe.db.set_value(doctype, name, "hidden", 1, update_modified=False)
	elif meta.has_field("is_hidden"):
		frappe.db.set_value(doctype, name, "is_hidden", 1, update_modified=False)


def _ensure_module_def() -> None:
	if not frappe.db.exists("Module Def", MODULE):
		frappe.get_doc(
			{"doctype": "Module Def", "module_name": MODULE, "app_name": APP, "custom": 0}
		).insert(ignore_permissions=True)
	else:
		doc = frappe.get_doc("Module Def", MODULE)
		changed = False
		if doc.app_name != APP:
			doc.app_name = APP
			changed = True
		if getattr(doc, "custom", 0):
			doc.custom = 0
			changed = True
		if changed:
			doc.save(ignore_permissions=True)

	meta = frappe.get_meta("Module Def")
	if meta.has_field("restrict_to_domain"):
		frappe.db.set_value("Module Def", MODULE, "restrict_to_domain", "", update_modified=False)

	for field, value in (("icon", ICON), ("color", BG_COLOR), ("module_icon", ICON)):
		if meta.has_field(field):
			frappe.db.set_value("Module Def", MODULE, field, value, update_modified=False)

	if frappe.db.exists("Module Def", LEGACY_DISPLAY):
		if not frappe.db.exists("DocType", {"module": LEGACY_DISPLAY}):
			frappe.delete_doc("Module Def", LEGACY_DISPLAY, force=1, ignore_permissions=True)


def _migrate_navigation_names() -> None:
	for doctype in ("Workspace", "Workspace Sidebar", "Sidebar", "Desktop Icon"):
		_rename_or_absorb(doctype, LEGACY_DISPLAY, DISPLAY)

	# Absorb module Sidebar rows that used the Persian title but module=Costing.
	if frappe.db.exists("DocType", "Sidebar"):
		for name in frappe.get_all("Sidebar", filters={"module": MODULE, "name": ["!=", DISPLAY]}, pluck="name"):
			if name == LEGACY_DISPLAY:
				_rename_or_absorb("Sidebar", LEGACY_DISPLAY, DISPLAY)
			else:
				_hide_doc_when_exists("Sidebar", name)


def _ensure_workspace() -> None:
	_migrate_navigation_names()
	meta = frappe.get_meta("Workspace")
	is_new = not frappe.db.exists("Workspace", DISPLAY)
	ws = frappe.new_doc("Workspace") if is_new else frappe.get_doc("Workspace", DISPLAY)

	for field, val in (
		("title", DISPLAY),
		("label", DISPLAY),
		("module", MODULE),
		("icon", ICON),
		("type", "Workspace"),
		("public", 1),
		("is_hidden", 0),
		("app", APP),
	):
		if meta.has_field(field):
			ws.set(field, val)
	if meta.has_field("for_user"):
		ws.for_user = None
	if meta.has_field("indicator_color"):
		ws.indicator_color = BG_COLOR
	if meta.has_field("content"):
		ws.content = _workspace_content_json()

	if meta.has_field("links"):
		ws.set("links", [])
		cards = [
			("Settings", "settings", [
				("Cost Allocation Settings", "Finished Goods Cost Allocation Settings"),
				("Account and Dimension Rules", "Finished Goods Cost Allocation Settings"),
				("Item Primary UOMs", "Finished Goods Cost Allocation Settings"),
			]),
			("Monthly Calculations", "calculator", [
				("Monthly FG Cost Calculations", "Monthly FG Cost Calculation"),
				("Calculation Status", "MFG Cost Account Dimension Summary"),
			]),
			("Reports & Details", "bar-chart-2", [
				("Expense Pools", "MFG Cost Expense Pool"),
				("GL Entry Details", "MFG Cost GL Line"),
				("Production Batches", "MFG Cost Batch Allocation"),
				("Batch × Expense Pool Allocations", "MFG Cost Batch Expense Allocation"),
				("Product Details", "MFG Cost Product Allocation"),
				("UOM Exceptions", "MFG Cost Product Allocation"),
			]),
			("Management & History", "archive", [
				("Calculation Revisions", "MFG Cost Calculation Revision"),
				("Calculation History", "MFG Cost Calculation Revision"),
				("Lock / Unlock Management", "Monthly FG Cost Calculation"),
			]),
		]
		for card_label, card_icon, links in cards:
			ws.append(
				"links",
				{"type": "Card Break", "label": card_label, "icon": card_icon, "link_type": "DocType"},
			)
			for label, link_to in links:
				ws.append(
					"links",
					{"type": "Link", "label": label, "link_type": "DocType", "link_to": link_to},
				)

	if meta.has_field("shortcuts"):
		ws.set("shortcuts", [])
		for label, link_to, color in (
			("Monthly Calculations", "Monthly FG Cost Calculation", "Blue"),
			("Cost Allocation Settings", "Finished Goods Cost Allocation Settings", "Gray"),
			("Expense Pools", "MFG Cost Expense Pool", "Green"),
		):
			ws.append(
				"shortcuts",
				{"type": "DocType", "label": label, "link_to": link_to, "doc_view": "List", "color": color},
			)

	ws.flags.ignore_links = True
	if is_new:
		ws.insert(ignore_permissions=True, ignore_links=True)
	else:
		ws.save(ignore_permissions=True)


def _ensure_costing_doctypes() -> None:
	"""Force-sync Costing DocTypes required by sidebar Link To before navigation writes.

	Production failure mode: model sync progress can report complete while individual
	Costing DocTypes are still absent (or not yet visible to Dynamic Link validation).
	The earlier Persian desk patch used ignore_links=True and therefore masked the gap;
	Workspace Sidebar sync must not.
	"""
	import os

	missing_files: list[str] = []
	for dt in REQUIRED_COSTING_DOCTYPES:
		try:
			path = get_doc_path(MODULE, "DocType", dt)
		except Exception:
			path = ""
		if not path or not os.path.exists(path):
			missing_files.append(dt)
			continue
		import_file_by_path(path, force=True)

	# Clear negative DB value caches that Dynamic Link validation may have primed.
	frappe.clear_cache()
	still_missing = [dt for dt in REQUIRED_COSTING_DOCTYPES if not frappe.db.exists("DocType", dt)]
	if missing_files or still_missing:
		frappe.throw(
			"Costing DocTypes required for Desk navigation are not installed. "
			f"Missing files: {missing_files or '—'}; missing DocType rows: {still_missing or '—'}. "
			"Re-run bench migrate after verifying erpnext_extensions Costing doctype JSON files.",
			frappe.ValidationError,
		)


def _hide_legacy_sidebars() -> None:
	"""Hide leftover Persian-titled Sidebar shells after English Costing is canonical."""
	if not frappe.db.exists("DocType", "Sidebar"):
		return
	for name in frappe.get_all(
		"Sidebar",
		filters={"module": MODULE, "name": ["!=", DISPLAY]},
		pluck="name",
	):
		if name == LEGACY_DISPLAY or name.startswith(LEGACY_DISPLAY):
			_hide_doc_when_exists("Sidebar", name)


def _ensure_sidebars() -> None:
	_ensure_costing_doctypes()
	# Workspace Home link target
	if not frappe.db.exists("Workspace", DISPLAY):
		frappe.throw(
			f"Workspace {DISPLAY} must exist before Costing sidebar sync.",
			frappe.ValidationError,
		)
	assert_sidebar_link_targets_exist(_SIDEBAR_ITEMS)
	sync_module_sidebar_items(
		MODULE,
		_SIDEBAR_ITEMS,
		title=DISPLAY,
		header_icon=ICON,
		app=APP,
		standard=1,
	)
	_hide_legacy_sidebars()


def _ensure_desktop_icon() -> None:
	_migrate_navigation_names()
	meta = frappe.get_meta("Desktop Icon")
	is_new = not frappe.db.exists("Desktop Icon", DISPLAY)
	icon = frappe.new_doc("Desktop Icon") if is_new else frappe.get_doc("Desktop Icon", DISPLAY)

	icon.label = DISPLAY
	icon.standard = 1
	icon.hidden = 0
	icon.icon_type = "Link"
	icon.link_type = "Workspace Sidebar"
	icon.link_to = DISPLAY
	icon.icon = ICON
	icon.app = APP
	icon.bg_color = BG_COLOR
	icon.idx = 28
	icon.restrict_removal = 0
	if meta.has_field("module"):
		icon.module = MODULE
	icon.set("roles", [])
	for role in DESK_ROLES:
		icon.append("roles", {"role": role})

	icon.flags.ignore_links = True
	if is_new:
		icon.insert(ignore_permissions=True, ignore_links=True)
	else:
		icon.save(ignore_permissions=True)

	for legacy_name in (LEGACY_DISPLAY,):
		if frappe.db.exists("Desktop Icon", legacy_name):
			frappe.delete_doc("Desktop Icon", legacy_name, force=1, ignore_permissions=True)

	# Drop stray duplicate tiles pointing at the same module or legacy label.
	for row in frappe.get_all(
		"Desktop Icon",
		filters={"app": APP, "name": ["not in", [DISPLAY]]},
		fields=["name", "label", "link_to", "hidden"],
	):
		if row.label in (LEGACY_DISPLAY, DISPLAY) or row.link_to in (LEGACY_DISPLAY, DISPLAY):
			if row.name != DISPLAY:
				frappe.delete_doc("Desktop Icon", row.name, force=1, ignore_permissions=True)

	clear_every_users_desktop_icons()
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")


def execute():
	frappe.flags.in_patch = True
	_ensure_module_def()
	_migrate_navigation_names()
	_ensure_workspace()
	_ensure_sidebars()
	_ensure_desktop_icon()
	frappe.db.commit()


def ensure_now():
	execute()
	sidebar_name = frappe.db.exists("Sidebar", DISPLAY) or frappe.db.get_value(
		"Sidebar", {"module": MODULE}, "name"
	)
	item_count = frappe.db.count("Sidebar Item", {"parent": sidebar_name}) if sidebar_name else 0
	return {
		"module_def_costing": bool(frappe.db.exists("Module Def", MODULE)),
		"duplicate_persian_module_def": bool(frappe.db.exists("Module Def", LEGACY_DISPLAY)),
		"workspace": frappe.db.get_value(
			"Workspace", DISPLAY, ["name", "module", "icon", "public", "is_hidden", "title", "label"], as_dict=True
		),
		"workspace_sidebar": frappe.db.get_value(
			"Workspace Sidebar",
			DISPLAY,
			["name", "title", "module", "header_icon", "app", "standard"],
			as_dict=True,
		)
		if frappe.db.exists("Workspace Sidebar", DISPLAY)
		else None,
		"sidebar": frappe.db.get_value(
			"Sidebar", sidebar_name, ["name", "title", "module", "header_icon", "app", "standard"], as_dict=True
		)
		if sidebar_name
		else None,
		"sidebar_item_count": item_count,
		"desktop_icon": frappe.db.get_value(
			"Desktop Icon",
			DISPLAY,
			["name", "label", "hidden", "link_type", "link_to", "icon", "standard", "app", "bg_color"],
			as_dict=True,
		),
		"legacy_desktop_icon": frappe.db.exists("Desktop Icon", LEGACY_DISPLAY),
	}
