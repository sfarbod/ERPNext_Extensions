# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: verify/repair Sales Order amount_eligible_for_commission to DECIMAL(30,9)."""

from __future__ import annotations

import frappe

from erpnext_extensions.sales_order_commission_decimal_precision import (
	apply_decimal_schema_target,
	assert_schema_target,
)


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_sales_order_amount_eligible_for_commission_precision")
	logger.info("Starting expand_sales_order_amount_eligible_for_commission_precision")
	results = apply_decimal_schema_target(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info(
		"Completed expand_sales_order_amount_eligible_for_commission_precision: %s rows",
		len(results),
	)
	if errors:
		raise RuntimeError(
			"Sales Order commission schema patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['table']}.{row['field']}" for row in errors)
		)
	assert_schema_target(logger)
