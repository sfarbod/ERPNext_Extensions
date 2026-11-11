# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Finished Goods Item Group eligibility for costing (تزریقی under محصول نهایی)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.fg_cost_allocation import (
	FG_ITEM_GROUP,
	FG_PARENT_ITEM_GROUP,
)


@dataclass
class ItemGroupHierarchy:
	item_group: str
	parent_item_group: str
	is_group: int


def _as_map(value) -> dict:
	if value is None:
		return {}
	if isinstance(value, dict):
		return value
	return dict(value)


def verify_fg_item_group_hierarchy() -> ItemGroupHierarchy:
	"""Confirm تزریقی exists, is an Item Group leaf (or usable group), and parent is محصول نهایی."""
	row = _as_map(
		frappe.db.get_value(
			"Item Group",
			FG_ITEM_GROUP,
			["name", "parent_item_group", "is_group"],
			as_dict=True,
		)
	)
	if not row:
		frappe.throw(_("Item Group {0} not found.").format(FG_ITEM_GROUP))
	parent_name = row.get("parent_item_group") or ""
	if parent_name != FG_PARENT_ITEM_GROUP:
		frappe.throw(
			_(
				"Item Group {0} parent is {1}; expected parent Item Group {2}."
			).format(FG_ITEM_GROUP, parent_name or "(blank)", FG_PARENT_ITEM_GROUP)
		)
	parent = _as_map(
		frappe.db.get_value(
			"Item Group",
			FG_PARENT_ITEM_GROUP,
			["name", "is_group"],
			as_dict=True,
		)
	)
	if not parent:
		frappe.throw(_("Parent Item Group {0} not found.").format(FG_PARENT_ITEM_GROUP))
	return ItemGroupHierarchy(
		item_group=row.get("name") or FG_ITEM_GROUP,
		parent_item_group=parent_name,
		is_group=int(row.get("is_group") or 0),
	)


def is_eligible_fg_item(item_code: str) -> bool:
	"""True when Item.item_group is تزریقی and hierarchy is verified."""
	if not item_code:
		return False
	# Item Group nodes are not Items; Item doctype only.
	item_group = frappe.db.get_value("Item", item_code, "item_group")
	if item_group != FG_ITEM_GROUP:
		return False
	parent = frappe.db.get_value("Item Group", FG_ITEM_GROUP, "parent_item_group")
	return parent == FG_PARENT_ITEM_GROUP


def classify_item_group(item_group: str | None) -> str:
	"""Return 'eligible', 'sibling_under_parent', or 'outside'."""
	if not item_group:
		return "outside"
	if item_group == FG_ITEM_GROUP:
		parent = frappe.db.get_value("Item Group", item_group, "parent_item_group")
		return "eligible" if parent == FG_PARENT_ITEM_GROUP else "outside"
	parent = frappe.db.get_value("Item Group", item_group, "parent_item_group")
	if parent == FG_PARENT_ITEM_GROUP:
		return "sibling_under_parent"
	return "outside"


@dataclass
class ExclusionRow:
	stock_entry: str
	stock_entry_detail: str
	work_order: str
	item_code: str
	item_name: str
	item_group: str
	batch_no: str
	qty: Decimal
	reason: str


def summarize_exclusions(rows: list[ExclusionRow]) -> dict:
	by_group: dict[str, dict] = {}
	total_qty = Decimal("0")
	for r in rows:
		total_qty += r.qty
		g = by_group.setdefault(r.item_group or "(blank)", {"row_count": 0, "qty": Decimal("0"), "items": set()})
		g["row_count"] += 1
		g["qty"] += r.qty
		g["items"].add(r.item_code)
	return {
		"excluded_source_row_count": len(rows),
		"excluded_qty": float(total_qty),
		"by_item_group": {
			k: {"row_count": v["row_count"], "qty": float(v["qty"]), "item_count": len(v["items"])}
			for k, v in sorted(by_group.items())
		},
		"sample_rows": [
			{
				"stock_entry": r.stock_entry,
				"stock_entry_detail": r.stock_entry_detail,
				"work_order": r.work_order,
				"item_code": r.item_code,
				"item_group": r.item_group,
				"batch_no": r.batch_no,
				"qty": float(r.qty),
				"reason": r.reason,
			}
			for r in rows[:100]
		],
	}
