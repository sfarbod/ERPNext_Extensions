# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Explicit refresh of Document Account Control Breakdown for one Product Allocation.

Not an automatic historical backfill. Used for Development verification / targeted refresh.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.document_breakdown import (
	build_document_breakdown,
	sum_batch_expense_by_account_control,
)


def _fg_qty_by_stock_entry(*, batch_no: str, item_code: str, stock_entries: list[str]) -> dict[str, Decimal]:
	out: dict[str, Decimal] = {}
	if not stock_entries:
		return out
	rows = frappe.db.sql(
		"""
		SELECT sed.parent AS stock_entry, SUM(sed.transfer_qty) AS qty
		FROM `tabStock Entry Detail` sed
		INNER JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE sed.parent IN %(ses)s
		  AND sed.item_code = %(item)s
		  AND IFNULL(sed.batch_no, '') = %(batch)s
		  AND IFNULL(sed.is_finished_item, 0) = 1
		  AND se.docstatus = 1
		GROUP BY sed.parent
		""",
		{"ses": tuple(stock_entries), "item": item_code, "batch": batch_no},
		as_dict=True,
	)
	for r in rows:
		out[r.stock_entry] = Decimal(str(flt(r.qty)))
	return out


def refresh_product_document_breakdown(product_allocation: str) -> dict:
	"""Replace breakdown child rows for one Product Allocation from live Costing data."""
	pa = frappe.get_doc("MFG Cost Product Allocation", product_allocation)
	import json

	try:
		stock_entries = json.loads(pa.stock_entries or "[]")
	except Exception:
		stock_entries = []
	if not stock_entries and pa.stock_entry:
		stock_entries = [pa.stock_entry]

	bx_rows = frappe.get_all(
		"MFG Cost Batch Expense Allocation",
		filters={
			"monthly_calculation": pa.monthly_calculation,
			"revision": pa.revision,
			"batch_no": pa.batch_no,
		},
		fields=["account_control", "allocated_amount"],
		limit_page_length=0,
	)
	overhead, labor, _other = sum_batch_expense_by_account_control(bx_rows)
	se_qtys = _fg_qty_by_stock_entry(
		batch_no=pa.batch_no, item_code=pa.item_code, stock_entries=list(stock_entries)
	)
	breakdown = build_document_breakdown(
		stock_entry_qtys=se_qtys,
		batch_overhead=overhead,
		batch_labor=labor,
		product_share=Decimal(str(pa.product_share or 0)),
		currency=pa.currency or "IRR",
	)

	pa.set("document_account_control_breakdown", [])
	for row in breakdown.rows:
		pa.append(
			"document_account_control_breakdown",
			{
				"source_document_type": row.source_document_type,
				"source_document": row.source_document,
				"quantity": float(row.quantity),
				"overhead_amount": float(row.overhead_amount),
				"labor_amount": float(row.labor_amount),
				"total_allocated_amount": float(row.total_allocated_amount),
				"currency": pa.currency,
			},
		)
	pa.breakdown_total_qty = float(breakdown.total_quantity)
	pa.breakdown_total_overhead = float(breakdown.total_overhead)
	pa.breakdown_total_labor = float(breakdown.total_labor)
	pa.breakdown_total_amount = float(breakdown.total_allocated_amount)
	pa.flags.ignore_validate_update_after_submit = True
	pa.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"product_allocation": pa.name,
		"rows": len(breakdown.rows),
		"total_quantity": float(breakdown.total_quantity),
		"total_overhead": float(breakdown.total_overhead),
		"total_labor": float(breakdown.total_labor),
		"total_allocated_amount": float(breakdown.total_allocated_amount),
		"batch_overhead": float(overhead),
		"batch_labor": float(labor),
		"other_or_blank": float(_other),
	}
