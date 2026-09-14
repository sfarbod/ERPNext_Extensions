# Copyright (c) 2026, ERPNext Extensions contributors
"""Make Historical Repair reachable from Stock workspace, sidebar, and page roles.

Idempotent. Does not change repair engines or accounting policy.
"""

from __future__ import annotations

import json

import frappe

PAGE = "historical-repair"
LABEL = "Historical Repair"
ROLES = (
	"Stock Manager",
	"Manufacturing Manager",
	"Accounts Manager",
	"System Manager",
	"Administrator",
)


def execute():
	_ensure_page_roles()
	_ensure_stock_workspace()
	_ensure_sidebar("Warehouse Control")
	_ensure_sidebar("Production Control")
	_ensure_stock_tools_sidebar()


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


def _ensure_stock_workspace():
	if not frappe.db.exists("Workspace", "Stock"):
		return
	ws = frappe.get_doc("Workspace", "Stock")
	changed = False
	if _ensure_workspace_card_link(ws, "Tools"):
		changed = True
	if not any((r.link_to or "") == PAGE for r in (ws.links or [])):
		if not any((r.type == "Card Break" and (r.label or "") == "Maintenance") for r in (ws.links or [])):
			ws.append("links", {"type": "Card Break", "label": "Maintenance"})
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
	elif _ensure_workspace_card_link(ws, "Maintenance"):
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
				(b.get("data") or {}).get("card_name") == "Maintenance" for b in content if isinstance(b, dict)
			):
				content.append({"id": "hr_maint", "type": "card", "data": {"card_name": "Maintenance", "col": 4}})
				content_changed = True
			if not any(
				b.get("type") == "shortcut" and (b.get("data") or {}).get("shortcut_name") == LABEL
				for b in content
				if isinstance(b, dict)
			):
				insert_at = 0
				for i, block in enumerate(content):
					if isinstance(block, dict) and block.get("type") in ("chart", "number_card"):
						insert_at = i + 1
				content.insert(
					insert_at,
					{"id": "hr_sc", "type": "shortcut", "data": {"shortcut_name": LABEL, "col": 3}},
				)
				content_changed = True
			if content_changed:
				ws.content = json.dumps(content)
				changed = True
	if changed:
		ws.save(ignore_permissions=True)


def _ensure_workspace_card_link(ws, card_label: str) -> bool:
	links = list(ws.links or [])
	card_idx = None
	last_in_card = None
	in_card = False
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
				return False
			last_in_card = i
	if card_idx is None:
		return False
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
	new = ws.links[-1]
	ws.links.remove(new)
	ws.links.insert(last_in_card + 1, new)
	for i, row in enumerate(ws.links, start=1):
		row.idx = i
	return True


def _ensure_stock_tools_sidebar():
	if not frappe.db.exists("Workspace Sidebar", "Stock"):
		return
	sb = frappe.get_doc("Workspace Sidebar", "Stock")
	if any((it.link_to or "") == PAGE for it in (sb.items or [])):
		return
	insert_at = len(sb.items or [])
	for i, it in enumerate(sb.items or []):
		if it.label == "Tools" and it.type == "Section Break":
			insert_at = i + 1
			j = i + 1
			while j < len(sb.items) and sb.items[j].type != "Section Break":
				insert_at = j + 1
				j += 1
			break
	sb.append(
		"items",
		{
			"type": "Link",
			"label": LABEL,
			"link_type": "Page",
			"link_to": PAGE,
			"icon": "tool",
			"child": 1,
			"indent": 0,
		},
	)
	new = sb.items[-1]
	sb.items.remove(new)
	sb.items.insert(insert_at, new)
	for i, it in enumerate(sb.items, start=1):
		it.idx = i
	sb.save(ignore_permissions=True)


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
