# Copyright (c) 2026, ERPNext Extensions contributors
"""Expose Stock Entry Dimension Repair on Stock / Accounts workspaces.

Idempotent. Does not change accounting engines.
"""

from __future__ import annotations

import json

import frappe

PAGE = "stock-entry-dimension-repair"
LABEL = "Stock Entry Dimension Repair"
ROLES = ("System Manager", "Administrator", "Accounts Manager")
CARD = "Repair Tools"


def execute():
	_ensure_page_roles()
	for ws_name in ("Stock", "Accounts", "Manufacturing"):
		_ensure_workspace(ws_name)
	for sb_name in ("Stock", "Accounts", "Manufacturing", "Production Control"):
		_ensure_sidebar(sb_name)


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
