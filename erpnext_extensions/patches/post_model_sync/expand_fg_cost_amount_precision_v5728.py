# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Widen FG cost allocation money columns beyond DECIMAL(21,9)."""

from __future__ import annotations

import frappe


TABLES = {
	"Monthly FG Cost Calculation": [
		"total_identified_expenses",
		"total_allocated_expenses",
		"unallocated_amount",
		"excluded_gl_amount",
	],
	"MFG Cost Expense Pool": [
		"monthly_debit",
		"monthly_credit",
		"monthly_net_expense",
		"allocated_amount",
		"unallocated_amount",
	],
	"MFG Cost Batch Allocation": ["total_allocated_expense"],
	"MFG Cost Batch Expense Allocation": [
		"pool_monthly_net_expense",
		"allocated_amount",
	],
	"MFG Cost Product Allocation": [
		"batch_allocated_expense",
		"product_allocated_expense",
		"unit_cost_on_actual",
		"unit_cost_on_normalized",
	],
	"MFG Cost Product Expense Allocation": [
		"pool_monthly_net_expense",
		"batch_pool_allocated_amount",
		"product_pool_allocated_amount",
	],
	"MFG Cost GL Line": ["debit", "credit", "net_amount"],
}


def execute():
	for doctype, columns in TABLES.items():
		if not frappe.db.table_exists(doctype):
			continue
		table = f"tab{doctype}"
		specs = []
		for col in columns:
			prec = "30,9" if "unit_cost" in col else "30,3"
			specs.append(f"`{col}` DECIMAL({prec}) NOT NULL DEFAULT 0")
		frappe.db.sql(f"ALTER TABLE `{table}` MODIFY " + ", MODIFY ".join(specs))
