# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: length=30 metadata for selling document monetary fields (v5.0.7)."""

from __future__ import annotations

import frappe

from erpnext_extensions.selling_documents_decimal_precision_v507 import verify_and_set_metadata


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_selling_documents_amount_decimal_metadata_v507")
	logger.info("Starting set_selling_documents_amount_decimal_metadata_v507")
	results = verify_and_set_metadata(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed set_selling_documents_amount_decimal_metadata_v507: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Selling documents metadata patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['doctype']}.{row['field']}" for row in errors)
		)
