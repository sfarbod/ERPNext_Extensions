# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: durable length=30 metadata for Sales Order amount_eligible_for_commission."""

from __future__ import annotations

import frappe

from erpnext_extensions.sales_order_commission_decimal_precision import verify_and_set_metadata


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.set_sales_order_amount_eligible_for_commission_decimal_metadata")
	logger.info("Starting set_sales_order_amount_eligible_for_commission_decimal_metadata")
	results = verify_and_set_metadata(logger)
	errors = [row for row in results if row.get("status") == "error"]
	logger.info("Completed set_sales_order_amount_eligible_for_commission_decimal_metadata: %s rows", len(results))
	if errors:
		raise RuntimeError(
			"Sales Order commission metadata patch encountered unexpected errors:\n"
			+ "\n".join(f"{row['doctype']}.{row['field']}" for row in errors)
		)
