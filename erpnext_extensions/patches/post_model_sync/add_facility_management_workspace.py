from __future__ import annotations

import json

import frappe
from erpnext_extensions.desk_navigation import (
	create_workspace_sidebars_if_available,
	sync_module_sidebar_items,
)

from erpnext_extensions.facility_management.desk_workspace_config import (
	SIDEBAR_HOME_ICON,
	SIDEBAR_LINK_ICONS,
	SIDEBAR_SECTION_ICONS,
	WORKSPACE_CARD_ICONS,
	WORKSPACE_CONFIGURATION_LINKS,
	WORKSPACE_REPORT_LINKS,
	WORKSPACE_TRANSACTION_LINKS,
)

MODULE_NAME = "Facility Management"
APP_NAME = "erpnext_extensions"

FACILITY_ACCOUNTING_DESKTOP_ROLES = (
	"Accounts User",
	"Accounts Manager",
	"System Manager",
)

_WORKSPACE_CONTENT = [
	{
		"id": "fm_ws_hdr",
		"type": "paragraph",
		"data": {
			"text": "<b>Facility Management</b> — banking facilities, repayments, and liability reporting.",
			"col": 12,
		},
	},
	{"id": "fm_ws_txn", "type": "card", "data": {"card_name": "Transactions", "col": 6}},
	{"id": "fm_ws_rpt", "type": "card", "data": {"card_name": "Reports", "col": 6}},
	{"id": "fm_ws_cfg", "type": "card", "data": {"card_name": "Configuration", "col": 6}},
]


def _content_json() -> str:
	return json.dumps(_WORKSPACE_CONTENT, separators=(",", ":"))


def _append_link(ws, row: dict):
	meta = frappe.get_meta("Workspace")
	if not meta.has_field("links"):
		return
	for existing in ws.links or []:
		if (
			getattr(existing, "type", None) == row.get("type")
			and (getattr(existing, "label", None) or "") == (row.get("label") or "")
			and (getattr(existing, "link_to", None) or "") == (row.get("link_to") or "")
		):
			return
	ws.append("links", row)


def _ensure_module_def():
	if frappe.db.exists("Module Def", MODULE_NAME):
		doc = frappe.get_doc("Module Def", MODULE_NAME)
		if doc.app_name != APP_NAME or doc.custom:
			doc.app_name = APP_NAME
			doc.custom = 0
			doc.save(ignore_permissions=True)
		return
	frappe.get_doc(
		{"doctype": "Module Def", "module_name": MODULE_NAME, "app_name": APP_NAME, "custom": 0}
	).insert(ignore_permissions=True)


def _desired_sidebar_items() -> list[dict]:
	items: list[dict] = [
		{
			"label": "Home",
			"type": "Link",
			"link_type": "Workspace",
			"link_to": MODULE_NAME,
			"icon": SIDEBAR_HOME_ICON,
		}
	]
	for section, links in (
		("Transactions", WORKSPACE_TRANSACTION_LINKS),
		("Reports", WORKSPACE_REPORT_LINKS),
		("Configuration", WORKSPACE_CONFIGURATION_LINKS),
	):
		items.append(
			{
				"label": section,
				"type": "Section Break",
				"collapsible": 1,
				"icon": SIDEBAR_SECTION_ICONS.get(section),
			}
		)
		for label, link_type, link_to, extra in links:
			items.append(
				{
					"label": label,
					"type": "Link",
					"link_type": link_type,
					"link_to": link_to,
					"child": 1,
					"icon": SIDEBAR_LINK_ICONS.get(label) or extra.get("icon"),
					**{k: v for k, v in extra.items() if k != "icon"},
				}
			)
	return items


def _sync_sidebar():
	if not frappe.db.exists("Workspace", MODULE_NAME):
		return
	sync_module_sidebar_items(
		MODULE_NAME,
		_desired_sidebar_items(),
		title=MODULE_NAME,
		header_icon="landmark",
		app=APP_NAME,
		standard=0,
	)


def _ensure_accounting_desktop_icon():
	"""Tile under ERPNext Accounting module page (parent_icon = Accounting)."""
	if not frappe.db.exists("Desktop Icon", "Accounting"):
		return
	has_nav = frappe.db.exists("Workspace Sidebar", MODULE_NAME) or frappe.db.exists(
		"Sidebar", MODULE_NAME
	)
	if not has_nav:
		return

	meta_di = frappe.get_meta("Desktop Icon")
	label = MODULE_NAME
	icon = (
		frappe.get_doc("Desktop Icon", label)
		if frappe.db.exists("Desktop Icon", label)
		else frappe.new_doc("Desktop Icon")
	)
	icon.label = label
	icon.icon_type = "Link"
	# Desktop Icon still links via the Workspace Sidebar archive name on Frappe 16.52.
	icon.link_type = "Workspace Sidebar"
	icon.link_to = MODULE_NAME
	icon.parent_icon = "Accounting"
	icon.standard = 1
	icon.hidden = 0
	if meta_di.has_field("icon"):
		icon.icon = "landmark"
	if meta_di.has_field("app"):
		icon.app = APP_NAME
	if meta_di.has_field("idx"):
		icon.idx = 9
	if meta_di.has_field("roles"):
		icon.set("roles", [])
		for role in FACILITY_ACCOUNTING_DESKTOP_ROLES:
			icon.append("roles", {"role": role})
	icon.save(ignore_permissions=True)


def execute():
	_ensure_module_def()
	meta = frappe.get_meta("Workspace")
	ws = (
		frappe.get_doc("Workspace", MODULE_NAME)
		if frappe.db.exists("Workspace", MODULE_NAME)
		else frappe.new_doc("Workspace")
	)
	ws.label = MODULE_NAME
	if meta.has_field("title"):
		ws.title = MODULE_NAME
	if meta.has_field("module"):
		ws.module = MODULE_NAME
	if meta.has_field("type"):
		ws.type = "Workspace"
	if meta.has_field("icon"):
		ws.icon = "landmark"
	if meta.has_field("public"):
		ws.public = 1
	if meta.has_field("content"):
		ws.content = _content_json()
	if meta.has_field("links"):
		ws.links = []
	_append_link(
		ws,
		{
			"type": "Card Break",
			"label": "Transactions",
			"icon": WORKSPACE_CARD_ICONS.get("Transactions"),
		},
	)
	for label, link_type, link_to, extra in WORKSPACE_TRANSACTION_LINKS:
		_append_link(
			ws, {"type": "Link", "label": label, "link_type": link_type, "link_to": link_to, **extra}
		)
	_append_link(
		ws,
		{"type": "Card Break", "label": "Reports", "icon": WORKSPACE_CARD_ICONS.get("Reports")},
	)
	for label, link_type, link_to, extra in WORKSPACE_REPORT_LINKS:
		_append_link(
			ws, {"type": "Link", "label": label, "link_type": link_type, "link_to": link_to, **extra}
		)
	_append_link(
		ws,
		{
			"type": "Card Break",
			"label": "Configuration",
			"icon": WORKSPACE_CARD_ICONS.get("Configuration"),
		},
	)
	for label, link_type, link_to, extra in WORKSPACE_CONFIGURATION_LINKS:
		_append_link(
			ws, {"type": "Link", "label": label, "link_type": link_type, "link_to": link_to, **extra}
		)
	ws.save(ignore_permissions=True)
	try:
		create_workspace_sidebars_if_available()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "facility_management_workspace_sidebar")
	_sync_sidebar()
	_ensure_accounting_desktop_icon()
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")
