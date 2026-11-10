# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: length=30 metadata for manufacturing monetary fields (v5.7.21)."""

from __future__ import annotations

import frappe

from erpnext_extensions.manufacturing_decimal_precision_v5721 import verify_and_set_metadata


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_manufacturing_amount_decimal_metadata_v5721")
	logger.info("Starting set_manufacturing_amount_decimal_metadata_v5721")
	results = verify_and_set_metadata(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed set_manufacturing_amount_decimal_metadata_v5721: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Manufacturing metadata patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['doctype']}.{row['field']}" for row in errors)
		)
