# Copyright (c) 2026, ERPNext Extensions contributors
"""Expose Reassign Job Cards on Manufacturing workspace and production sidebar.

Idempotent. Does not change manufacturing engines.
"""

from __future__ import annotations

import json

import frappe

PAGE = "reassign-job-cards"
LABEL = "Reassign Job Cards"
ROLES = ("System Manager", "Administrator")
CARD = "Repair Tools"


def execute():
	_ensure_page_roles()
	_ensure_manufacturing_workspace()
	_ensure_sidebar("Production Control")
	_ensure_sidebar("Manufacturing")


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


def _ensure_manufacturing_workspace():
	if not frappe.db.exists("Workspace", "Manufacturing"):
		return
	ws = frappe.get_doc("Workspace", "Manufacturing")
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
	else:
		if _move_link_into_card(ws, CARD):
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
				content.append({"id": "rjc_repair", "type": "card", "data": {"card_name": CARD, "col": 4}})
				content_changed = True
			if not any(
				b.get("type") == "shortcut" and (b.get("data") or {}).get("shortcut_name") == LABEL
				for b in content
				if isinstance(b, dict)
			):
				content.append({"id": "rjc_sc", "type": "shortcut", "data": {"shortcut_name": LABEL, "col": 3}})
				content_changed = True
			if content_changed:
				ws.content = json.dumps(content)
				changed = True
	if changed:
		ws.save(ignore_permissions=True)


def _move_link_into_card(ws, card_label: str) -> bool:
	links = list(ws.links or [])
	card_idx = None
	last_in_card = None
	in_card = False
	already_in_card = False
	for i, row in enumerate(links):
		if row.type == "Card Break":
			if in_card:
				break
			in_card = (row.label or "") == card_label
			if in_card:
				card_idx = i
				last_in_card = i
			continue
		if in_card:
			if (row.link_to or "") == PAGE:
				already_in_card = True
			last_in_card = i
	if already_in_card or card_idx is None:
		return False
	page_row = next((r for r in links if (r.link_to or "") == PAGE), None)
	if not page_row:
		return False
	ws.links.remove(page_row)
	insert_at = min(last_in_card + 1, len(ws.links))
	ws.links.insert(insert_at, page_row)
	for i, row in enumerate(ws.links, start=1):
		row.idx = i
	return True


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
