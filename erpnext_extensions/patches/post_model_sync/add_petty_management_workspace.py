from __future__ import annotations

import json

import frappe
from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import (
	create_workspace_sidebar_for_workspaces,
)

from erpnext_extensions.petty_management.desk_workspace_config import (
	SIDEBAR_HOME_ICON,
	SIDEBAR_LINK_ICONS,
	SIDEBAR_SECTION_ICONS,
	WORKSPACE_CARD_ICONS,
	WORKSPACE_REPORT_LINKS,
	WORKSPACE_SETUP_LINKS,
	WORKSPACE_SHORTCUTS,
	WORKSPACE_TRANSACTION_LINKS,
)

MODULE_NAME = "Petty Management"
APP_NAME = "erpnext_extensions"

# Editor.js blocks for Desk workspace main area; card_name must match Card Break labels in links.
_PETTY_WORKSPACE_CONTENT = [
	{
		"id": "pm_ws_hdr",
		"type": "paragraph",
		"data": {
			"text": "<b>Petty Management</b> — funding, clearance, and reporting.",
			"col": 12,
		},
	},
	{"id": "pm_ws_setup", "type": "card", "data": {"card_name": "Setup", "col": 4}},
	{"id": "pm_ws_txn", "type": "card", "data": {"card_name": "Transactions", "col": 4}},
	{"id": "pm_ws_rpt", "type": "card", "data": {"card_name": "Reports", "col": 4}},
]

def _petty_workspace_content_json() -> str:
	return json.dumps(_PETTY_WORKSPACE_CONTENT, separators=(",", ":"))


def _link_row_val(row, key: str):
	if row is None:
		return None
	if isinstance(row, dict):
		return row.get(key)
	return getattr(row, key, None)


def _norm(val):
	if val is None:
		return ""
	return val


def _as_int(val) -> int:
	try:
		return int(val or 0)
	except (TypeError, ValueError):
		return 0

def _append_link(ws, row: dict):
	meta = frappe.get_meta("Workspace")
	if not meta.has_field("links"):
		return
	for existing in ws.links or []:
		if (
			_link_row_val(existing, "type") == row.get("type")
			and (_link_row_val(existing, "label") or "") == (row.get("label") or "")
			and (_link_row_val(existing, "link_type") or "") == (row.get("link_type") or "")
			and (_link_row_val(existing, "link_to") or "") == (row.get("link_to") or "")
		):
			return
	ws.append("links", row)


def _ensure_module_def():
	"""Create or update Module Def so it appears under erpnext_extensions on Desk."""
	if frappe.db.exists("Module Def", MODULE_NAME):
		doc = frappe.get_doc("Module Def", MODULE_NAME)
		changed = False
		if doc.app_name != APP_NAME:
			doc.app_name = APP_NAME
			changed = True
		if doc.custom:
			doc.custom = 0
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return

	frappe.get_doc(
		{
			"doctype": "Module Def",
			"module_name": MODULE_NAME,
			"app_name": APP_NAME,
			"custom": 0,
		}
	).insert(ignore_permissions=True)


def _ensure_workspace_sidebar():
	"""Ensure generic Workspace Sidebar rows exist (v16 also needs explicit items — see _sync_petty_workspace_sidebar)."""
	try:
		create_workspace_sidebar_for_workspaces()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "petty_management_workspace_sidebar")


def _desired_sidebar_items() -> list[dict]:
	"""Canonical Petty Management Workspace Sidebar items (order-sensitive).

	Only include fields that exist on Workspace Sidebar Item — extras such as
	``is_query_report`` belong to Workspace Link, not the sidebar child table.
	"""
	items: list[dict] = []

	def add_item(row: dict) -> None:
		items.append(row)

	def add_section(label: str) -> None:
		row = {
			"label": label,
			"type": "Section Break",
			"collapsible": 1,
			"keep_closed": 0,
		}
		if SIDEBAR_SECTION_ICONS.get(label):
			row["icon"] = SIDEBAR_SECTION_ICONS[label]
		add_item(row)

	def add_link(label: str, link_type: str, link_to: str, **extra) -> None:
		row = {
			"label": label,
			"type": "Link",
			"link_type": link_type,
			"link_to": link_to,
			"child": 1,
			"icon": SIDEBAR_LINK_ICONS.get(label) or extra.pop("icon", None),
		}
		# Ignore Workspace-Link-only extras (e.g. is_query_report).
		add_item(row)
	add_item(
		{
			"label": "Home",
			"type": "Link",
			"link_type": "Workspace",
			"link_to": MODULE_NAME,
			"icon": SIDEBAR_HOME_ICON,
		}
	)

	add_section("Setup")
	for label, link_type, link_to, _extra in WORKSPACE_SETUP_LINKS:
		add_link(label, link_type, link_to)

	add_section("Transactions")
	for label, link_type, link_to, _extra in WORKSPACE_TRANSACTION_LINKS:
		add_link(label, link_type, link_to)

	add_section("Reports")
	for label, link_type, link_to, extra in WORKSPACE_REPORT_LINKS:
		add_link(label, link_type, link_to, **extra)

	return items


def _sidebar_item_signature(items) -> list[tuple]:
	"""Compare only navigation-meaningful fields.

	Ignore framework defaults Frappe fills on save (e.g. Section Break.link_type=DocType,
	Link.collapsible=1, indent/show_arrow=0) so idempotent migrate does not re-export JSON.
	"""
	sig = []
	for it in items or []:
		row_type = _norm(_link_row_val(it, "type"))
		label = _norm(_link_row_val(it, "label"))
		icon = _norm(_link_row_val(it, "icon"))
		if row_type == "Section Break":
			sig.append(
				(
					"Section Break",
					label,
					icon,
					_as_int(_link_row_val(it, "collapsible")),
					_as_int(_link_row_val(it, "keep_closed")),
				)
			)
		else:
			sig.append(
				(
					row_type,
					label,
					_norm(_link_row_val(it, "link_type")),
					_norm(_link_row_val(it, "link_to")),
					_as_int(_link_row_val(it, "child")),
					icon,
				)
			)
	return sig
def _sync_petty_workspace_sidebar():
	"""Frappe v16: boot.get_sidebar_items reads Workspace Sidebar `items`; factory only adds Home + shortcuts.

	Rebuild Petty Management sidebar with Home, grouped sections, and workspace links so /app/petty-management
	left navigation is usable (center cards unchanged).

	Idempotent: skip save/export when effective content already matches (avoids developer_mode
	JSON churn of ``modified`` on every migrate).
	"""
	if not frappe.db.exists("Workspace", MODULE_NAME):
		return
	meta_sb = frappe.get_meta("Workspace Sidebar")
	if not meta_sb:
		return

	desired_items = _desired_sidebar_items()
	is_new = not frappe.db.exists("Workspace Sidebar", MODULE_NAME)

	if is_new:
		sb = frappe.new_doc("Workspace Sidebar")
		sb.title = MODULE_NAME
	else:
		sb = frappe.get_doc("Workspace Sidebar", MODULE_NAME)

	desired_header = "wallet" if meta_sb.has_field("header_icon") else None
	desired_module = MODULE_NAME if meta_sb.has_field("module") else None
	desired_standard = 1 if meta_sb.has_field("standard") else None
	desired_app = APP_NAME if meta_sb.has_field("app") else None

	if not is_new:
		same_items = _sidebar_item_signature(sb.items) == _sidebar_item_signature(desired_items)
		same_meta = True
		if meta_sb.has_field("header_icon") and _norm(sb.header_icon) != _norm(desired_header):
			same_meta = False
		if meta_sb.has_field("module") and _norm(sb.module) != _norm(desired_module):
			same_meta = False
		if meta_sb.has_field("standard") and int(sb.standard or 0) != int(desired_standard or 0):
			same_meta = False
		if meta_sb.has_field("app") and _norm(sb.app) != _norm(desired_app):
			same_meta = False
		if meta_sb.has_field("for_user") and sb.for_user:
			same_meta = False
		if same_items and same_meta:
			return

	if meta_sb.has_field("header_icon"):
		sb.header_icon = desired_header
	if meta_sb.has_field("module"):
		sb.module = desired_module
	if meta_sb.has_field("standard"):
		sb.standard = desired_standard
	if meta_sb.has_field("app"):
		sb.app = desired_app
	if meta_sb.has_field("for_user"):
		sb.for_user = None

	sb.items = []
	for idx, row in enumerate(desired_items):
		row = dict(row)
		row["idx"] = idx
		sb.append("items", row)

	sb.save(ignore_permissions=True)


def _ensure_desktop_icon():
	"""Standard Desktop Icon rows are only created at site install; add one so /desk home shows the module tile.

	Idempotent: skip save/export when the tile already matches the canonical definition.
	"""
	if not frappe.db.exists("Workspace", MODULE_NAME):
		return

	meta_di = frappe.get_meta("Desktop Icon")
	label = MODULE_NAME
	is_new = not frappe.db.exists("Desktop Icon", label)

	if is_new:
		icon = frappe.new_doc("Desktop Icon")
		icon.label = label
	else:
		icon = frappe.get_doc("Desktop Icon", label)

	desired = {
		"icon_type": "Link",
		"link_type": "Workspace Sidebar",
		"link_to": MODULE_NAME,
		"standard": 1,
		"hidden": 0,
	}
	if meta_di.has_field("icon"):
		desired["icon"] = "wallet"
	if meta_di.has_field("app"):
		desired["app"] = APP_NAME

	if not is_new:
		same = True
		for field, val in desired.items():
			cur = icon.get(field)
			if field in ("standard", "hidden"):
				if int(cur or 0) != int(val or 0):
					same = False
					break
			elif _norm(cur) != _norm(val):
				same = False
				break
		# idx: only require a value when empty; do not churn idx alone
		if same:
			return

	for field, val in desired.items():
		icon.set(field, val)
	if meta_di.has_field("idx") and not icon.idx:
		icon.idx = 100

	icon.save(ignore_permissions=True)


def _desired_workspace_links() -> list[dict]:
	links: list[dict] = []
	links.append(
		{
			"type": "Card Break",
			"label": "Setup",
			"icon": WORKSPACE_CARD_ICONS.get("Setup", "settings"),
		}
	)
	for label, link_type, link_to, extra in WORKSPACE_SETUP_LINKS:
		links.append(
			{"type": "Link", "label": label, "link_type": link_type, "link_to": link_to, **extra}
		)
	links.append(
		{
			"type": "Card Break",
			"label": "Transactions",
			"icon": WORKSPACE_CARD_ICONS.get("Transactions", "repeat"),
		}
	)
	for label, link_type, link_to, extra in WORKSPACE_TRANSACTION_LINKS:
		links.append(
			{"type": "Link", "label": label, "link_type": link_type, "link_to": link_to, **extra}
		)
	links.append(
		{
			"type": "Card Break",
			"label": "Reports",
			"icon": WORKSPACE_CARD_ICONS.get("Reports", "bar-chart-2"),
		}
	)
	for label, link_type, link_to, extra in WORKSPACE_REPORT_LINKS:
		links.append(
			{"type": "Link", "label": label, "link_type": link_type, "link_to": link_to, **extra}
		)
	return links


def _workspace_links_signature(links) -> list[tuple]:
	"""Semantic Workspace.links compare (ignore Card Break link_type defaults)."""
	sig = []
	for row in links or []:
		row_type = _norm(_link_row_val(row, "type"))
		label = _norm(_link_row_val(row, "label"))
		icon = _norm(_link_row_val(row, "icon"))
		if row_type == "Card Break":
			sig.append(("Card Break", label, icon))
		else:
			sig.append(
				(
					row_type,
					label,
					_norm(_link_row_val(row, "link_type")),
					_norm(_link_row_val(row, "link_to")),
					icon,
					_as_int(_link_row_val(row, "is_query_report")),
				)
			)
	return sig

def _ensure_workspace_shortcuts(ws) -> bool:
	meta = frappe.get_meta("Workspace")
	changed = False
	if not meta.has_field("shortcuts"):
		return changed
	existing = {(r.link_to or ""): r for r in (ws.get("shortcuts") or [])}
	for label, link_to, link_type in WORKSPACE_SHORTCUTS:
		if link_to in existing:
			continue
		ws.append(
			"shortcuts",
			{
				"type": link_type,
				"label": label,
				"link_to": link_to,
				"doc_view": "List",
			},
		)
		changed = True
	return changed


def _sync_petty_workspace() -> None:
	"""Create/update the Petty Management Workspace only when content differs."""
	meta = frappe.get_meta("Workspace")
	is_new = not frappe.db.exists("Workspace", MODULE_NAME)

	if is_new:
		ws = frappe.new_doc("Workspace")
		ws.label = MODULE_NAME
	else:
		ws = frappe.get_doc("Workspace", MODULE_NAME)

	desired_content = _petty_workspace_content_json() if meta.has_field("content") else None
	desired_links = _desired_workspace_links() if meta.has_field("links") else []

	if not is_new:
		same = True
		checks = [
			("title", MODULE_NAME),
			("label", MODULE_NAME),
			("module", MODULE_NAME),
			("type", "Workspace"),
			("icon", "wallet"),
		]
		for field, val in checks:
			if meta.has_field(field) and _norm(ws.get(field)) != _norm(val):
				same = False
				break
		if same and meta.has_field("public") and int(ws.get("public") or 0) != 1:
			same = False
		if same and meta.has_field("is_hidden") and int(ws.get("is_hidden") or 0) != 0:
			same = False
		if same and meta.has_field("for_user") and _norm(ws.get("for_user")) != "":
			same = False
		if same and meta.has_field("content") and (ws.content or "") != desired_content:
			same = False
		if same and meta.has_field("links"):
			if _workspace_links_signature(ws.links) != _workspace_links_signature(desired_links):
				same = False
		# shortcuts: ensure missing ones are added; if any missing, not same
		if same and meta.has_field("shortcuts"):
			existing = {(r.link_to or "") for r in (ws.get("shortcuts") or [])}
			for _label, link_to, _lt in WORKSPACE_SHORTCUTS:
				if link_to not in existing:
					same = False
					break
		if same:
			return

	if meta.has_field("title"):
		ws.title = MODULE_NAME
	if meta.has_field("label"):
		ws.label = MODULE_NAME
	if meta.has_field("module"):
		ws.module = MODULE_NAME
	if meta.has_field("type"):
		ws.type = "Workspace"
	if meta.has_field("icon"):
		ws.icon = "wallet"
	for fn, val in (("public", 1), ("is_hidden", 0)):
		if meta.has_field(fn):
			ws.set(fn, val)
	if meta.has_field("for_user"):
		ws.set("for_user", "")
	if meta.has_field("sequence_id") and not ws.get("sequence_id"):
		ws.sequence_id = 90
	if meta.has_field("content"):
		ws.content = desired_content

	if meta.has_field("links"):
		ws.links = []
		for row in desired_links:
			_append_link(ws, row)

	_ensure_workspace_shortcuts(ws)
	ws.save(ignore_permissions=True)


def _clear_desk_caches():
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")


def execute():
	_ensure_module_def()
	_sync_petty_workspace()
	_ensure_workspace_sidebar()
	_sync_petty_workspace_sidebar()
	_ensure_desktop_icon()
	_clear_desk_caches()
	frappe.db.commit()
