# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: length=30 metadata for stock repost monetary fields (v5.1.8)."""

from __future__ import annotations

import frappe

from erpnext_extensions.stock_repost_decimal_precision_v518 import verify_and_set_metadata


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_stock_repost_amount_decimal_metadata_v518")
	logger.info("Starting set_stock_repost_amount_decimal_metadata_v518")
	results = verify_and_set_metadata(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed set_stock_repost_amount_decimal_metadata_v518: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Stock repost metadata patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['doctype']}.{row['field']}" for row in errors)
		)
