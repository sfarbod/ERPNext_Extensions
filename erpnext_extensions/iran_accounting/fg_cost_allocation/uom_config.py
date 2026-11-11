# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Per-item primary UOM configuration and validation (no Item master writes)."""

from __future__ import annotations

import json
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.fg_cost_allocation import FG_PARENT_ITEM_GROUP


def get_item_stock_uom(item_code: str) -> str:
	return frappe.db.get_value("Item", item_code, "stock_uom") or ""


def resolve_conversion(item_code: str, primary_uom: str) -> tuple[Decimal | None, str, str]:
	"""Return (factor, status, remarks). Factor = primary units per 1 stock UOM."""
	if not item_code or not primary_uom:
		return None, "Missing", "Item or Primary UOM not set"
	stock_uom = get_item_stock_uom(item_code)
	if not stock_uom:
		return None, "Invalid", "Item has no Stock UOM"
	if stock_uom == primary_uom:
		return Decimal("1"), "Valid", "Stock UOM equals Primary UOM"
	rows = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": item_code, "uom": primary_uom},
		fields=["name", "conversion_factor"],
	)
	if not rows:
		return None, "Missing", f"No UOM Conversion Detail for {primary_uom}"
	if len(rows) > 1:
		return None, "Ambiguous", f"{len(rows)} conversion rows for {primary_uom}"
	raw_factor = rows[0].get("conversion_factor") if isinstance(rows[0], dict) else rows[0].conversion_factor
	factor = Decimal(str(flt(raw_factor)))
	if factor <= 0:
		return None, "Invalid", f"Non-positive conversion factor {factor}"
	return factor, "Valid", ""


def list_fg_items(company: str | None = None) -> list[dict]:
	"""Eligible Finished Goods items (parent item group محصول نهایی)."""
	return frappe.db.sql(
		"""
		SELECT i.name AS item, i.item_name, i.stock_uom
		FROM tabItem i
		INNER JOIN `tabItem Group` ig ON ig.name = i.item_group
		WHERE ig.parent_item_group = %(fg)s
		  AND IFNULL(i.disabled, 0) = 0
		ORDER BY i.name
		""",
		{"fg": FG_PARENT_ITEM_GROUP},
		as_dict=True,
	)


def build_primary_uom_map(settings) -> dict[str, dict]:
	"""item_code → {primary_uom, factor, status, stock_uom}."""
	out: dict[str, dict] = {}
	for row in settings.get("item_primary_uoms") or []:
		if not row.item:
			continue
		factor, status, remarks = resolve_conversion(row.item, row.primary_uom)
		out[row.item] = {
			"primary_uom": row.primary_uom,
			"stock_uom": row.stock_uom or get_item_stock_uom(row.item),
			"factor": factor,
			"status": status,
			"remarks": remarks,
		}
	return out


@frappe.whitelist()
def validate_and_refresh_item_uoms(settings_name: str, replace_existing: int = 0):
	"""Refresh Item Primary UOM rows from FG items + default_primary_uom. Never writes Item masters."""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	default_uom = doc.default_primary_uom
	if not default_uom:
		frappe.throw(_("Set Default Primary UOM before validating Item UOMs."))

	existing = {r.item: r for r in (doc.item_primary_uoms or []) if r.item}
	items = list_fg_items(doc.company)
	created = updated = skipped = valid = missing = ambiguous = invalid = 0

	if cint(replace_existing):
		doc.set("item_primary_uoms", [])
		existing = {}

	seen = set()
	for it in items:
		item = it.item
		seen.add(item)
		primary = existing[item].primary_uom if item in existing and not cint(replace_existing) else default_uom
		if item in existing and not cint(replace_existing):
			# refresh status/factor only
			row = existing[item]
			factor, status, remarks = resolve_conversion(item, row.primary_uom or default_uom)
			row.stock_uom = it.stock_uom
			row.item_name = it.item_name
			if not row.primary_uom:
				row.primary_uom = default_uom
			row.conversion_factor = float(factor) if factor is not None else None
			row.conversion_status = status
			row.remarks = remarks
			updated += 1
		else:
			factor, status, remarks = resolve_conversion(item, primary)
			doc.append(
				"item_primary_uoms",
				{
					"item": item,
					"item_name": it.item_name,
					"stock_uom": it.stock_uom,
					"primary_uom": primary,
					"conversion_factor": float(factor) if factor is not None else None,
					"conversion_status": status,
					"remarks": remarks,
				},
			)
			created += 1
		st = status if item not in existing or cint(replace_existing) else resolve_conversion(item, primary)[1]
		# recount from final resolve
		factor, status, remarks = resolve_conversion(item, primary)
		if status == "Valid":
			valid += 1
		elif status == "Missing":
			missing += 1
		elif status == "Ambiguous":
			ambiguous += 1
		else:
			invalid += 1

	# Drop rows for items no longer FG? Keep manually — only skip count
	orphan = [r.item for r in (doc.item_primary_uoms or []) if r.item and r.item not in seen]
	skipped = len(orphan)

	summary = {
		"items_scanned": len(items),
		"created": created,
		"updated": updated,
		"orphan_rows_kept": skipped,
		"valid": valid,
		"missing": missing,
		"ambiguous": ambiguous,
		"invalid": invalid,
		"default_primary_uom": default_uom,
	}
	doc.last_uom_validation_summary = json.dumps(summary, ensure_ascii=False, indent=2)
	# Refresh all row statuses once more for consistency
	for row in doc.item_primary_uoms or []:
		factor, status, remarks = resolve_conversion(row.item, row.primary_uom)
		row.stock_uom = get_item_stock_uom(row.item)
		row.conversion_factor = float(factor) if factor is not None else None
		row.conversion_status = status
		row.remarks = remarks
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return summary


@frappe.whitelist()
def preview_uom_exceptions(settings_name: str):
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("read")
	rows = []
	for row in doc.item_primary_uoms or []:
		if row.conversion_status != "Valid":
			rows.append(
				{
					"item": row.item,
					"item_name": row.item_name,
					"stock_uom": row.stock_uom,
					"primary_uom": row.primary_uom,
					"conversion_status": row.conversion_status,
					"remarks": row.remarks,
				}
			)
	return {"count": len(rows), "rows": rows}
