# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: expand purchasing/asset monetary columns to DECIMAL(30,9) (v5.2.4)."""

from __future__ import annotations

import frappe

from erpnext_extensions.purchasing_decimal_precision_v524 import repair_purchasing_decimal_schema


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_purchasing_amount_precision_v524")
	logger.info("Starting expand_purchasing_amount_precision_v524")
	result = repair_purchasing_decimal_schema(run_completeness_guard=True)
	logger.info(
		"Completed expand_purchasing_amount_precision_v524: repaired=%s",
		result["repaired"],
	)
