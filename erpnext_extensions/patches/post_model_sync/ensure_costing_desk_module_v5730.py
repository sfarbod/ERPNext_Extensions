# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Ensure Costing appears as Desk tile بهای تمام شده with module Sidebar."""

from __future__ import annotations

import frappe
from frappe.desk.doctype.desktop_icon.desktop_icon import clear_every_users_desktop_icons

APP = "erpnext_extensions"
MODULE = "Costing"
DISPLAY = "بهای تمام شده"
ICON = "calculator"

SIDEBAR_ITEMS = [
	{"type": "Link", "label": "خانه", "link_type": "Workspace", "link_to": DISPLAY, "icon": "home", "child": 0},
	{"type": "Section Break", "label": "تنظیمات", "link_type": "DocType", "icon": "settings", "child": 0},
	{
		"type": "Link",
		"label": "تنظیمات تخصیص هزینه",
		"link_type": "DocType",
		"link_to": "Finished Goods Cost Allocation Settings",
		"icon": "settings",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "قوانین حساب‌ها و ابعاد",
		"link_type": "DocType",
		"link_to": "Finished Goods Cost Allocation Settings",
		"icon": "list",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "تنظیمات واحدهای اصلی و تبدیل UOM",
		"link_type": "DocType",
		"link_to": "Finished Goods Cost Allocation Settings",
		"icon": "package",
		"child": 1,
	},
	{"type": "Section Break", "label": "محاسبات ماهانه", "link_type": "DocType", "icon": "calculator", "child": 0},
	{
		"type": "Link",
		"label": "محاسبه بهای تمام‌شده محصولات نهایی",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "file-plus",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "فهرست محاسبات ماهانه",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "list",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "وضعیت محاسبات و مغایرت‌ها",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "alert-triangle",
		"child": 1,
	},
	{"type": "Section Break", "label": "گزارش‌ها و جزئیات", "link_type": "DocType", "icon": "bar-chart-2", "child": 0},
	{
		"type": "Link",
		"label": "استخرهای هزینه",
		"link_type": "DocType",
		"link_to": "MFG Cost Expense Pool",
		"icon": "layers",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "جزئیات اسناد حسابداری",
		"link_type": "DocType",
		"link_to": "MFG Cost GL Line",
		"icon": "book-open",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "تخصیص هزینه به بچ‌های تولید",
		"link_type": "DocType",
		"link_to": "MFG Cost Batch Allocation",
		"icon": "package",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "تخصیص هزینه به محصولات",
		"link_type": "DocType",
		"link_to": "MFG Cost Product Allocation",
		"icon": "box",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "تخصیص بچ × استخر",
		"link_type": "DocType",
		"link_to": "MFG Cost Batch Expense Allocation",
		"icon": "git-merge",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "تخصیص محصول × استخر",
		"link_type": "DocType",
		"link_to": "MFG Cost Product Expense Allocation",
		"icon": "git-merge",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "گزارش مغایرت‌ها و UOM",
		"link_type": "DocType",
		"link_to": "MFG Cost Product Allocation",
		"icon": "alert-circle",
		"child": 1,
	},
	{"type": "Section Break", "label": "مدیریت و سوابق", "link_type": "DocType", "icon": "archive", "child": 0},
	{
		"type": "Link",
		"label": "تاریخچه محاسبات",
		"link_type": "DocType",
		"link_to": "MFG Cost Calculation Revision",
		"icon": "history",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "نسخه‌های محاسبات",
		"link_type": "DocType",
		"link_to": "MFG Cost Calculation Revision",
		"icon": "git-branch",
		"child": 1,
	},
	{
		"type": "Link",
		"label": "وضعیت قفل و بازگشایی محاسبات",
		"link_type": "DocType",
		"link_to": "Monthly FG Cost Calculation",
		"icon": "lock",
		"child": 1,
	},
]


def _ensure_module_def() -> None:
	if not frappe.db.exists("Module Def", MODULE):
		doc = frappe.new_doc("Module Def")
		doc.module_name = MODULE
		doc.app_name = APP
		doc.insert(ignore_permissions=True)
	else:
		frappe.db.set_value("Module Def", MODULE, "app_name", APP, update_modified=False)

	# Empty Persian Module Def duplicates the tile/module namespace — remove if unused.
	if frappe.db.exists("Module Def", DISPLAY):
		if not frappe.db.exists("DocType", {"module": DISPLAY}):
			frappe.delete_doc("Module Def", DISPLAY, force=1, ignore_permissions=True)


def _ensure_workspace() -> None:
	"""Keep public Workspace for Home link; do not edit archived navigation body via ORM."""
	if not frappe.db.exists("Workspace", DISPLAY):
		frappe.get_doc(
			{
				"doctype": "Workspace",
				"name": DISPLAY,
				"label": DISPLAY,
				"title": DISPLAY,
				"module": MODULE,
				"icon": ICON,
				"public": 1,
				"is_hidden": 0,
				"content": "[]",
				"type": "Workspace",
				"app": APP,
			}
		).insert(ignore_permissions=True, ignore_links=True)
	else:
		frappe.db.sql(
			"""
			UPDATE `tabWorkspace`
			SET module=%s, icon=%s, public=1, is_hidden=0, title=%s, label=%s, app=%s
			WHERE name=%s
			""",
			(MODULE, ICON, DISPLAY, DISPLAY, APP, DISPLAY),
		)


def _ensure_sidebar() -> None:
	"""Create/replace the live v16 Sidebar (module navigation shell)."""
	# Prefer a sidebar named after the display title; module=Costing for visibility.
	existing = frappe.db.exists("Sidebar", DISPLAY) or frappe.db.exists("Sidebar", {"module": MODULE, "title": DISPLAY})
	if existing:
		doc = frappe.get_doc("Sidebar", existing)
	else:
		# Also absorb an English-named Costing shell if present
		if frappe.db.exists("Sidebar", MODULE):
			doc = frappe.get_doc("Sidebar", MODULE)
			# rename title to Persian display via db to keep one shell
			frappe.rename_doc("Sidebar", MODULE, DISPLAY, force=True, merge=False)
			doc = frappe.get_doc("Sidebar", DISPLAY)
		else:
			doc = frappe.new_doc("Sidebar")
			doc.title = DISPLAY

	doc.module = MODULE
	doc.app = APP
	doc.header_icon = ICON
	doc.set("items", [])
	for row in SIDEBAR_ITEMS:
		doc.append("items", row)
	doc.flags.ignore_links = True
	if doc.is_new():
		doc.insert(ignore_permissions=True, ignore_links=True)
	else:
		doc.save(ignore_permissions=True)
	# standard=1 requires an on-disk fixture under costing/sidebar/; set after save.
	frappe.db.set_value("Sidebar", doc.name, "standard", 1, update_modified=False)


def _ensure_desktop_icon() -> None:
	roles = [
		"System Manager",
		"Accounts Manager",
		"Accounts User",
		"Manufacturing Manager",
		"Auditor",
	]
	if frappe.db.exists("Desktop Icon", DISPLAY):
		doc = frappe.get_doc("Desktop Icon", DISPLAY)
	else:
		doc = frappe.new_doc("Desktop Icon")
		doc.label = DISPLAY

	doc.standard = 1
	doc.hidden = 0
	doc.icon_type = "Link"
	doc.link_type = "Workspace Sidebar"
	doc.link_to = DISPLAY
	doc.icon = ICON
	doc.app = APP
	doc.bg_color = "blue"
	doc.idx = 28
	doc.restrict_removal = 0
	doc.set("roles", [])
	for role in roles:
		doc.append("roles", {"role": role})
	doc.flags.ignore_links = True
	if doc.is_new():
		doc.insert(ignore_permissions=True, ignore_links=True)
	else:
		doc.save(ignore_permissions=True)

	# Hide a leftover English "Costing" tile if one was auto-seeded
	if frappe.db.exists("Desktop Icon", MODULE) and MODULE != DISPLAY:
		frappe.db.set_value("Desktop Icon", MODULE, "hidden", 1, update_modified=False)

	clear_every_users_desktop_icons()


def execute():
	frappe.flags.in_patch = True
	_ensure_module_def()
	_ensure_workspace()
	_ensure_sidebar()
	_ensure_desktop_icon()
	frappe.db.commit()


def ensure_now():
	execute()
	sidebar_name = frappe.db.exists("Sidebar", DISPLAY) or frappe.db.get_value("Sidebar", {"module": MODULE}, "name")
	item_count = frappe.db.count("Sidebar Item", {"parent": sidebar_name}) if sidebar_name else 0
	return {
		"module_def_costing": bool(frappe.db.exists("Module Def", MODULE)),
		"duplicate_persian_module_def": bool(frappe.db.exists("Module Def", DISPLAY)),
		"workspace": frappe.db.get_value(
			"Workspace", DISPLAY, ["name", "module", "icon", "public", "is_hidden"], as_dict=True
		),
		"sidebar": frappe.db.get_value(
			"Sidebar", sidebar_name, ["name", "title", "module", "header_icon", "app"], as_dict=True
		)
		if sidebar_name
		else None,
		"sidebar_item_count": item_count,
		"desktop_icon": frappe.db.get_value(
			"Desktop Icon",
			DISPLAY,
			["name", "label", "hidden", "link_type", "link_to", "icon", "standard", "app"],
			as_dict=True,
		),
	}
