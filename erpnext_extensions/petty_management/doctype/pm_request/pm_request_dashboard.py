# Copyright (c) 2026, ERPNext Extensions contributors
"""Native Frappe dashboard Connections configuration for PM Request."""

from __future__ import annotations

import json

import frappe
from frappe import _

_CONNECTION_SPECS = (
	("payment_entries", "payment_entry", "Payment Entry"),
	("clearances", "clearance", "PM Clearance"),
	("journal_entries", "journal_entry", "Journal Entry"),
)


def get_data():
	return {
		"fieldname": "name",
		"method": (
			"erpnext_extensions.petty_management.doctype.pm_request.pm_request_dashboard"
			".get_pm_request_connection_counts"
		),
		"transactions": [
			{"label": _("Funding"), "items": ["Payment Entry"]},
			{"label": _("Settlement"), "items": ["PM Clearance"]},
			{"label": _("Accounting"), "items": ["Journal Entry"]},
		],
	}


@frappe.whitelist()
@frappe.read_only()
def get_pm_request_connection_counts(doctype: str, name: str, items=None):
	"""Dashboard count adapter — reuses authoritative connections payload."""
	from erpnext_extensions.petty_management.services.request_connections_service import (
		build_pm_request_connections_payload,
	)

	doc = frappe.get_doc(doctype, name)
	frappe.has_permission(doctype, "read", doc=doc, throw=True)
	payload = build_pm_request_connections_payload(doc)

	if items and isinstance(items, str):
		items = json.loads(items)
	allowed = set(items) if items else None

	internal_links_found = []
	for key, name_field, link_doctype in _CONNECTION_SPECS:
		if allowed and link_doctype not in allowed:
			continue
		rows = payload.get(key) or []
		names = [row[name_field] for row in rows if row.get(name_field)]
		internal_links_found.append(
			{
				"doctype": link_doctype,
				"count": len(names),
				"open_count": 0,
				"names": names,
			}
		)

	return {
		"count": {
			"internal_links_found": internal_links_found,
			"external_links_found": [],
		}
	}
