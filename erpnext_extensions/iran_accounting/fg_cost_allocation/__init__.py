# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Retrospective Finished Goods Cost Allocation (v5.7.27)."""

from __future__ import annotations

DETAIL_DOCTYPE_MAP = {
	"expense_pool": "MFG Cost Expense Pool",
	"gl_line": "MFG Cost GL Line",
	"batch": "MFG Cost Batch Allocation",
	"batch_expense": "MFG Cost Batch Expense Allocation",
	"product": "MFG Cost Product Allocation",
	"product_expense": "MFG Cost Product Expense Allocation",  # historical only; not primary costing
	"account_dimension_summary": "MFG Cost Account Dimension Summary",
	"reconciliation_check": "MFG Cost Reconciliation Check",
	"exception": "MFG Cost Exception",
	"revision": "MFG Cost Calculation Revision",
}

CONSUMABLE_STATUSES = frozenset({"Calculated", "Recalculated", "Locked"})

FG_PARENT_ITEM_GROUP = "محصول نهایی"
FG_ITEM_GROUP = "تزریقی"
