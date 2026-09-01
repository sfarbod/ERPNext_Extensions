# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: expand selling document monetary columns to DECIMAL(30,9) (v5.0.7)."""

from __future__ import annotations

import frappe

from erpnext_extensions.selling_documents_decimal_precision_v507 import (
	apply_decimal_schema_targets,
	assert_field_classification_completeness,
	assert_schema_targets,
)


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_selling_documents_amount_precision_v507")
	logger.info("Starting expand_selling_documents_amount_precision_v507")
	results = apply_decimal_schema_targets(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed expand_selling_documents_amount_precision_v507: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Selling documents schema patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['table']}.{row['field']}" for row in errors)
		)
	assert_schema_targets(logger)
	assert_field_classification_completeness()
