# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: expand manufacturing monetary columns to DECIMAL(30,9) (v5.7.21)."""

from __future__ import annotations

import frappe

from erpnext_extensions.manufacturing_decimal_precision_v5721 import repair_manufacturing_decimal_schema


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_manufacturing_amount_precision_v5721")
	logger.info("Starting expand_manufacturing_amount_precision_v5721")
	result = repair_manufacturing_decimal_schema(run_completeness_guard=True)
	logger.info(
		"Completed expand_manufacturing_amount_precision_v5721: repaired=%s",
		result["repaired"],
	)
