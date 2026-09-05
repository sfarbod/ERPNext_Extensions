# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.account_explorer.constants import ITEM_SORTABLE_FIELDS
from erpnext_extensions.iran_accounting.account_explorer.pagination import paginate_stock_summary_rows, sort_rows
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.stock_measures import stock_row_from_buckets
from erpnext_extensions.iran_accounting.account_explorer.stock_opening import (
	get_item_stock_buckets,
)

ITEM_COLUMNS = [
	{"id": "display_code", "label": "Item", "fieldtype": "Data", "width": 140},
	{"id": "display_title", "label": "Item Name", "fieldtype": "Data", "width": 220},
	{"id": "item_group", "label": "Item Group", "fieldtype": "Data", "width": 140},
	{"id": "in_qty", "label": "In Qty", "fieldtype": "Float", "width": 100},
	{"id": "out_qty", "label": "Out Qty", "fieldtype": "Float", "width": 100},
	{"id": "balance_qty", "label": "Balance Qty", "fieldtype": "Float", "width": 110},
	{"id": "inward_value", "label": "Inward Value", "fieldtype": "Currency", "width": 130},
	{"id": "outward_value", "label": "Outward Value", "fieldtype": "Currency", "width": 130},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 130},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 130},
]


def build_item_summary(spec: AccountExplorerQuerySpec) -> dict:
	opening, period = get_item_stock_buckets(spec)
	keys = set(opening.keys()) | set(period.keys())

	item_meta: dict[str, dict] = {}
	if keys:
		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", list(keys)]},
			fields=["name", "item_name", "item_group"],
		):
			item_meta[row.name] = row

	rows: list[dict] = []
	for item_code in keys:
		if not item_code:
			# Missing item relationship — exclude; diagnostics cover broken links.
			continue
		op = opening.get(item_code, {})
		pe = period.get(item_code, {})
		meta = item_meta.get(item_code) or {}
		measures = stock_row_from_buckets(
			opening_qty=op.get("opening_qty", 0.0),
			in_qty=pe.get("in_qty", 0.0),
			out_qty=pe.get("out_qty", 0.0),
			opening_value=op.get("opening_value", 0.0),
			inward_value=pe.get("inward_value", 0.0),
			outward_value=pe.get("outward_value", 0.0),
			include_qty=True,
		)
		rows.append(
			{
				"row_key": f"item:{item_code}",
				"item_code": item_code,
				"item_group": meta.get("item_group") or "",
				"display_code": item_code,
				"display_title": meta.get("item_name") or item_code,
				"is_virtual_group": 0,
				"drill_down_enabled": 0,
				**measures,
			}
		)

	rows = sort_rows(rows, spec, ITEM_SORTABLE_FIELDS)
	result = paginate_stock_summary_rows(rows, spec, include_qty=True)
	# Footer: value family only. Summing qty across mixed UOMs is not business-meaningful.
	totals = result.get("totals") or {}
	for qty_key in ("in_qty", "out_qty", "balance_qty", "opening_qty", "closing_qty"):
		totals.pop(qty_key, None)
	totals["qty_footer_policy"] = "suppressed_mixed_uom"
	result["totals"] = totals
	result["warnings"] = []
	return result
