# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.2: the Daily Production Log approach (4.7.2 - 4.8.1) is dropped. Remove what it added
to a site: the two DocTypes (and their tables), the *Daily Production* Module Def and the
``Batch.custom_is_placeholder_lot`` custom field. Job Cards, Stock Entries and Batches the
runner created are ordinary documents and are left untouched. Idempotent."""

from __future__ import annotations

import frappe

DOCTYPES = ("Daily Production Log", "Daily Production Log Document")
MODULE = "Daily Production"
CUSTOM_FIELD = "Batch-custom_is_placeholder_lot"


def execute():
	for doctype in DOCTYPES:
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True, ignore_missing=True)

	if frappe.db.exists("Custom Field", CUSTOM_FIELD):
		frappe.delete_doc("Custom Field", CUSTOM_FIELD, force=True, ignore_permissions=True)

	if frappe.db.exists("Module Def", MODULE):
		frappe.delete_doc("Module Def", MODULE, force=True, ignore_permissions=True)

	frappe.clear_cache(doctype="Batch")
