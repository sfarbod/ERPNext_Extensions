# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: expand stock repost monetary columns to DECIMAL(30,9) (v5.1.8)."""

from __future__ import annotations

import frappe

from erpnext_extensions.stock_repost_decimal_precision_v518 import (
	apply_decimal_schema_targets,
	assert_repost_field_classification_completeness,
	assert_repost_monetary_schema_targets,
)


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_stock_repost_amount_precision_v518")
	logger.info("Starting expand_stock_repost_amount_precision_v518")
	results = apply_decimal_schema_targets(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed expand_stock_repost_amount_precision_v518: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Stock repost schema patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['table']}.{row['field']}" for row in errors)
		)
	assert_repost_monetary_schema_targets(logger)
	assert_repost_field_classification_completeness()
