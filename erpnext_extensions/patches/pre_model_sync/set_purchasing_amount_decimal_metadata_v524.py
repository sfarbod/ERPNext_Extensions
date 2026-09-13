# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: length=30 metadata for purchasing/asset monetary fields (v5.2.4)."""

from __future__ import annotations

import frappe

from erpnext_extensions.purchasing_decimal_precision_v524 import verify_and_set_metadata


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_purchasing_amount_decimal_metadata_v524")
	logger.info("Starting set_purchasing_amount_decimal_metadata_v524")
	results = verify_and_set_metadata(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed set_purchasing_amount_decimal_metadata_v524: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Purchasing metadata patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['doctype']}.{row['field']}" for row in errors)
		)
