# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: expand SI discount-calculation custom amounts to DECIMAL(30,9) (v5.7.9)."""

from __future__ import annotations

import frappe

from erpnext_extensions.selling_documents_decimal_precision_v507 import (
	repair_selling_documents_decimal_schema,
)


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_sales_invoice_custom_amount_precision_v579")
	logger.info("Starting expand_sales_invoice_custom_amount_precision_v579")
	result = repair_selling_documents_decimal_schema(run_completeness_guard=True)
	logger.info(
		"Completed expand_sales_invoice_custom_amount_precision_v579: repaired=%s",
		result["repaired"],
	)
