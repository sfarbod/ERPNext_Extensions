# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: length=30 for SI discount-calculation custom amounts (v5.7.9)."""

from __future__ import annotations

import frappe

from erpnext_extensions.selling_documents_decimal_precision_v507 import (
	V579_CRITICAL_SALES_INVOICE_FIELDS,
	V579_CRITICAL_SALES_INVOICE_ITEM_FIELDS,
	ensure_custom_field_length,
	verify_and_set_metadata,
)


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_sales_invoice_custom_amount_decimal_metadata_v579")
	logger.info("Starting set_sales_invoice_custom_amount_decimal_metadata_v579")
	for fieldname in V579_CRITICAL_SALES_INVOICE_FIELDS:
		ensure_custom_field_length("Sales Invoice", fieldname)
	for fieldname in V579_CRITICAL_SALES_INVOICE_ITEM_FIELDS:
		ensure_custom_field_length("Sales Invoice Item", fieldname)
	results = verify_and_set_metadata(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed set_sales_invoice_custom_amount_decimal_metadata_v579: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Sales Invoice custom amount metadata patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['doctype']}.{row['field']}" for row in errors)
		)
