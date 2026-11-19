# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Ensure SCR unit-rate Currency fields keep allocation decimals on IRR Desk forms.

IRR Currency number_format is ``#,###`` (0 decimals). Without an explicit field
``precision``, Desk formats ``service_cost_per_qty`` / ``rate`` as integers
(e.g. 72,021 / 1,794,247), which disagrees with amount = rate × qty using the
true fractional allocation (145,122,080 / received_qty).
"""

from __future__ import annotations

import frappe

TARGET_PRECISION = "9"

FIELDS = (
	("Subcontracting Receipt Item", "service_cost_per_qty"),
	("Subcontracting Receipt Item", "rate"),
	("Subcontracting Receipt Item", "additional_cost_per_qty"),
	("Subcontracting Receipt Item", "secondary_items_cost_per_qty"),
)


def _ensure_precision(doctype: str, fieldname: str) -> str:
	filters = {
		"doc_type": doctype,
		"field_name": fieldname,
		"property": "precision",
	}
	existing = frappe.db.get_value("Property Setter", filters, "name")
	if existing:
		current = str(frappe.db.get_value("Property Setter", existing, "value") or "")
		if current != TARGET_PRECISION:
			frappe.db.set_value("Property Setter", existing, "value", TARGET_PRECISION)
			return "updated"
		return "unchanged"

	frappe.get_doc(
		{
			"doctype": "Property Setter",
			"doctype_or_field": "DocField",
			"doc_type": doctype,
			"field_name": fieldname,
			"property": "precision",
			"property_type": "Data",
			"value": TARGET_PRECISION,
		}
	).insert(ignore_permissions=True)
	return "created"


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_scr_unit_rate_display_precision")
	for doctype, fieldname in FIELDS:
		action = _ensure_precision(doctype, fieldname)
		logger.info("%s.%s precision=%s (%s)", doctype, fieldname, TARGET_PRECISION, action)
	frappe.clear_cache(doctype="Subcontracting Receipt Item")
	frappe.clear_cache(doctype="Subcontracting Receipt")
