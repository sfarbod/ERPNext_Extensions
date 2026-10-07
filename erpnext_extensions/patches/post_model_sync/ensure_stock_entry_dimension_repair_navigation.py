# Copyright (c) 2026, ERPNext Extensions contributors
"""Expose Stock Entry Dimension Repair on Stock / Accounts workspaces.

Idempotent. Ensures the standard Page document exists (import from app files
if migrate sync has not yet created it), then wires navigation once.
Does not change accounting engines.
"""

from __future__ import annotations

import json
from pathlib import Path

import frappe

PAGE = "stock-entry-dimension-repair"
LABEL = "Stock Entry Dimension Repair"
ROLES = ("System Manager", "Administrator", "Accounts Manager")
CARD = "Repair Tools"


def execute():
	_ensure_page_document()
	_ensure_page_roles()
	for ws_name in ("Stock", "Accounts", "Manufacturing"):
		_ensure_workspace(ws_name)
	for sb_name in ("Stock", "Accounts", "Manufacturing", "Production Control"):
		_ensure_sidebar(sb_name)


def _page_json_path() -> Path:
	# erpnext_extensions/patches/post_model_sync/this_file.py
	# → erpnext_extensions/erpnext_extensions/page/stock_entry_dimension_repair/...
	app_root = Path(__file__).resolve().parents[2]
	return (
		app_root
		/ "erpnext_extensions"
		/ "page"
		/ "stock_entry_dimension_repair"
		/ "stock_entry_dimension_repair.json"
	)


def _ensure_page_document():
	"""Create/sync the standard Page from filesystem if missing."""
	if frappe.db.exists("Page", PAGE):
		return
	path = _page_json_path()
	if not path.is_file():
		frappe.log_error(
			f"Stock Entry Dimension Repair page JSON missing: {path}",
			"ensure_stock_entry_dimension_repair_navigation",
		)
		return
	from frappe.modules.import_file import import_file_by_path

	import_file_by_path(str(path), force=True, ignore_version=True)
	frappe.clear_cache(doctype="Page")


def _ensure_page_roles():
	if not frappe.db.exists("Page", PAGE):
		return
	page = frappe.get_doc("Page", PAGE)
	changed = False
	if (page.title or "") != LABEL:
		page.title = LABEL
		changed = True
	have = {r.role for r in page.roles or []}
	for role in ROLES:
		if role not in have:
			page.append("roles", {"role": role})
			changed = True
	if changed:
		page.save(ignore_permissions=True)


def _ensure_workspace(name: str):
	if not frappe.db.exists("Workspace", name):
		return
	ws = frappe.get_doc("Workspace", name)
	changed = False
	if not any((r.type == "Card Break" and (r.label or "") == CARD) for r in (ws.links or [])):
		ws.append("links", {"type": "Card Break", "label": CARD})
		changed = True
	if not any((r.link_to or "") == PAGE for r in (ws.links or [])):
		ws.append(
			"links",
			{
				"type": "Link",
				"label": LABEL,
				"link_type": "Page",
				"link_to": PAGE,
				"icon": "tool",
			},
		)
		changed = True
	if frappe.get_meta("Workspace").has_field("shortcuts"):
		if not any((s.link_to or "") == PAGE for s in (ws.shortcuts or [])):
			ws.append(
				"shortcuts",
				{"type": "Page", "label": LABEL, "link_to": PAGE, "icon": "tool"},
			)
			changed = True
	if frappe.get_meta("Workspace").has_field("content"):
		try:
			content = json.loads(ws.content or "[]")
		except Exception:
			content = []
		if isinstance(content, list):
			content_changed = False
			if not any(
				(b.get("data") or {}).get("card_name") == CARD for b in content if isinstance(b, dict)
			):
				content.append(
					{"id": "sedim_repair", "type": "card", "data": {"card_name": CARD, "col": 4}}
				)
				content_changed = True
			if not any(
				b.get("type") == "shortcut" and (b.get("data") or {}).get("shortcut_name") == LABEL
				for b in content
				if isinstance(b, dict)
			):
				content.append(
					{"id": "sedim_sc", "type": "shortcut", "data": {"shortcut_name": LABEL, "col": 3}}
				)
				content_changed = True
			if content_changed:
				ws.content = json.dumps(content)
				changed = True
	if changed:
		ws.save(ignore_permissions=True)


def _ensure_sidebar(name: str):
	if not frappe.db.exists("Workspace Sidebar", name):
		return
	sb = frappe.get_doc("Workspace Sidebar", name)
	if any((it.link_to or "") == PAGE for it in (sb.items or [])):
		return
	sb.append(
		"items",
		{
			"type": "Link",
			"label": LABEL,
			"link_type": "Page",
			"link_to": PAGE,
			"icon": "tool",
			"child": 0,
			"indent": 0,
		},
	)
	sb.save(ignore_permissions=True)
