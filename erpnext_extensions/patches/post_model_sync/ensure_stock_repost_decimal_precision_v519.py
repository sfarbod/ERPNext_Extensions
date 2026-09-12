# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: repair stock repost DECIMAL(30,9) schema drift (v5.1.9).

One-shot upgrade path for sites that already executed v5.1.8 patches but later
lost Property Setters / had updatedb narrow columns back to DECIMAL(21,9).

Ongoing protection is provided by ``after_migrate`` calling the same repair.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.stock_repost_decimal_precision_v519 import repair_stock_repost_decimal_schema


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.ensure_stock_repost_decimal_precision_v519")
	logger.info("Starting ensure_stock_repost_decimal_precision_v519")
	result = repair_stock_repost_decimal_schema(run_completeness_guard=True)
	logger.info(
		"Completed ensure_stock_repost_decimal_precision_v519: repaired=%s",
		result["repaired"],
	)
