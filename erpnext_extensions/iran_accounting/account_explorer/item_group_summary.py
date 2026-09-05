# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

from erpnext_extensions.iran_accounting.account_explorer.constants import ITEM_GROUP_SORTABLE_FIELDS
from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import get_item_group_node
from erpnext_extensions.iran_accounting.account_explorer.pagination import paginate_stock_summary_rows, sort_rows
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.stock_measures import stock_row_from_buckets
from erpnext_extensions.iran_accounting.account_explorer.stock_opening import (
	get_item_group_stock_buckets,
)

ITEM_GROUP_COLUMNS = [
	{"id": "display_code", "label": "Item Group", "fieldtype": "Data", "width": 160},
	{"id": "display_title", "label": "Title", "fieldtype": "Data", "width": 220},
	{"id": "inward_value", "label": "Inward Value", "fieldtype": "Currency", "width": 140},
	{"id": "outward_value", "label": "Outward Value", "fieldtype": "Currency", "width": 140},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 140},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 140},
]


def build_item_group_summary(spec: AccountExplorerQuerySpec) -> dict:
	"""Aggregate SLE stock value by leaf Item Group only (v5.1.1)."""
	opening, period = get_item_group_stock_buckets(spec)
	leaf_keys = {k for k in (set(opening.keys()) | set(period.keys())) if k}

	selected = None
	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	if item_group_scope:
		selected = item_group_scope.selected_item_group

	rows: list[dict] = []
	for group_name in sorted(leaf_keys):
		meta = get_item_group_node(group_name) or {}
		if int(meta.get("is_group") or 0):
			continue

		op = opening.get(group_name, {})
		pe = period.get(group_name, {})
		measures = stock_row_from_buckets(
			opening_value=op.get("opening_value", 0.0),
			inward_value=pe.get("inward_value", 0.0),
			outward_value=pe.get("outward_value", 0.0),
			include_qty=False,
		)
		rows.append(
			{
				"row_key": f"item_group:{group_name}",
				"item_group": group_name,
				"display_code": group_name,
				"display_title": group_name,
				"is_virtual_group": 0,
				"drill_down_enabled": 1,
				"is_group": 0,
				**measures,
			}
		)

	rows = sort_rows(rows, spec, ITEM_GROUP_SORTABLE_FIELDS)
	result = paginate_stock_summary_rows(rows, spec, include_qty=False)
	result["warnings"] = []
	result["selected_item_group"] = selected
	result["presentation_groups"] = []
	return result
