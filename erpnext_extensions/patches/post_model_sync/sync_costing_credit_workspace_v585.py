# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Sync Costing Workspace + Sidebar links for Control Credit Allocation (v5.8.5 follow-up)."""

from __future__ import annotations

import json
from pathlib import Path

import frappe


def _load_json(rel: str) -> dict:
	app_path = Path(frappe.get_app_path("erpnext_extensions"))
	return json.loads((app_path / rel).read_text(encoding="utf-8"))


def _sync_workspace():
	data = _load_json("costing/workspace/costing/costing.json")
	if not frappe.db.exists("Workspace", "Costing"):
		return
	ws = frappe.get_doc("Workspace", "Costing")
	ws.links = []
	for link in data.get("links") or []:
		ws.append("links", link)
	ws.shortcuts = []
	for sc in data.get("shortcuts") or []:
		ws.append("shortcuts", sc)
	if data.get("content"):
		ws.content = data["content"]
	ws.flags.ignore_links = True
	ws.flags.ignore_validate = True
	ws.save(ignore_permissions=True)


def _sync_sidebar():
	# Sidebar DocType may be app-specific; only sync if present
	if not frappe.db.exists("DocType", "Sidebar"):
		return
	if not frappe.db.exists("Sidebar", "Costing"):
		return
	data = _load_json("costing/sidebar/costing/costing.json")
	sb = frappe.get_doc("Sidebar", "Costing")
	sb.items = []
	for item in data.get("items") or []:
		sb.append("items", item)
	sb.flags.ignore_links = True
	sb.flags.ignore_validate = True
	sb.save(ignore_permissions=True)


def execute():
	_sync_workspace()
	_sync_sidebar()
	frappe.clear_cache()
