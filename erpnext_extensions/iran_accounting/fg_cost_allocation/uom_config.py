# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Item Primary UOM configuration — user-controlled costing scope (no Item master writes)."""

from __future__ import annotations

import json
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.fg_cost_allocation import FG_ITEM_GROUP, FG_PARENT_ITEM_GROUP
from erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility import verify_fg_item_group_hierarchy


def get_item_stock_uom(item_code: str) -> str:
	return frappe.db.get_value("Item", item_code, "stock_uom") or ""


def resolve_conversion(item_code: str, primary_uom: str) -> tuple[Decimal | None, str, str]:
	"""Return (source_factor, status, remarks) from Item UOM Conversion Detail only."""
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
	raw = rows[0].get("conversion_factor") if isinstance(rows[0], dict) else rows[0].conversion_factor
	factor = Decimal(str(flt(raw)))
	if factor <= 0:
		return None, "Invalid", f"Non-positive conversion factor {factor}"
	return factor, "Valid", ""


def apply_row_factors(row) -> None:
	"""Set source/applied/status on a FG Item Primary UOM row without writing Item masters."""
	source, status, remarks = resolve_conversion(row.item, row.primary_uom)
	row.source_conversion_factor = float(source) if source is not None else 0.0
	# Treat blank/0 as "no override" so users can clear the field.
	raw_manual = row.manual_override_factor
	manual = flt(raw_manual) if raw_manual not in (None, "") else None
	if manual is not None and manual == 0:
		manual = None
		row.manual_override_factor = None
	if manual is not None and manual > 0:
		row.conversion_factor = manual
		row.conversion_status = "Manual Override"
		row.conversion_source = "Manual Override"
		row.remarks = remarks or row.override_reason or ""
	elif source is not None and status == "Valid":
		row.conversion_factor = float(source)
		row.conversion_status = "Valid"
		row.conversion_source = "ERPNext"
		row.remarks = remarks
	else:
		row.conversion_factor = 0.0
		row.conversion_status = status
		row.conversion_source = "Missing"
		row.remarks = remarks


def list_default_seed_items(company: str | None = None) -> list[dict]:
	"""Initial default population: enabled Items in محصول نهایی → تزریقی only."""
	verify_fg_item_group_hierarchy()
	return frappe.db.sql(
		"""
		SELECT i.name AS item, i.item_name, i.stock_uom, i.item_group
		FROM tabItem i
		INNER JOIN `tabItem Group` ig ON ig.name = i.item_group
		WHERE i.item_group = %(fg_item_group)s
		  AND ig.parent_item_group = %(fg_parent)s
		  AND IFNULL(i.disabled, 0) = 0
		ORDER BY i.name
		""",
		{"fg_item_group": FG_ITEM_GROUP, "fg_parent": FG_PARENT_ITEM_GROUP},
		as_dict=True,
	)


# Back-compat alias used by older callers
list_fg_items = list_default_seed_items


def build_primary_uom_map(settings) -> dict[str, dict]:
	"""item_code → {primary_uom, factor, status, stock_uom} using Applied Conversion Factor."""
	out: dict[str, dict] = {}
	for row in settings.get("item_primary_uoms") or []:
		if not row.item:
			continue
		apply_row_factors(row)
		factor = Decimal(str(flt(row.conversion_factor))) if flt(row.conversion_factor) > 0 else None
		status = row.conversion_status or "Missing"
		ok = status in ("Valid", "Manual Override") and factor is not None and factor > 0
		out[row.item] = {
			"primary_uom": row.primary_uom,
			"stock_uom": row.stock_uom or get_item_stock_uom(row.item),
			"factor": factor if ok else None,
			"status": "Valid" if ok else status,
			"remarks": row.remarks or "",
		}
	return out


@frappe.whitelist()
def validate_and_refresh_item_uoms(settings_name: str, replace_existing: int = 0):
	"""Refresh/seed Item Primary UOM rows. Never writes Item masters.

	replace_existing=1 reseeds from تزریقی default hierarchy (initial population).
	replace_existing=0 only refreshes factors/status for existing rows (keeps manual adds).
	"""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	default_uom = doc.default_primary_uom
	if not default_uom:
		frappe.throw(_("Set Default Primary UOM before validating Item UOMs."))

	existing = {r.item: r for r in (doc.item_primary_uoms or []) if r.item}
	created = updated = 0
	valid = missing = ambiguous = invalid = manual = 0

	if cint(replace_existing):
		doc.set("item_primary_uoms", [])
		existing = {}
		for it in list_default_seed_items(doc.company):
			row = doc.append(
				"item_primary_uoms",
				{
					"item": it.item,
					"item_name": it.item_name,
					"item_group": it.item_group,
					"stock_uom": it.stock_uom,
					"primary_uom": default_uom,
				},
			)
			apply_row_factors(row)
			created += 1
	else:
		for row in doc.item_primary_uoms or []:
			if not row.item:
				continue
			meta = frappe.db.get_value("Item", row.item, ["item_name", "item_group", "stock_uom"], as_dict=True)
			if meta:
				row.item_name = meta.item_name
				row.item_group = meta.item_group
				row.stock_uom = meta.stock_uom
			if not row.primary_uom:
				row.primary_uom = default_uom
			apply_row_factors(row)
			updated += 1

	for row in doc.item_primary_uoms or []:
		st = row.conversion_status or "Missing"
		if st == "Valid":
			valid += 1
		elif st == "Manual Override":
			manual += 1
		elif st == "Missing":
			missing += 1
		elif st == "Ambiguous":
			ambiguous += 1
		else:
			invalid += 1

	summary = {
		"created": created,
		"updated": updated,
		"valid": valid,
		"manual_override": manual,
		"missing": missing,
		"ambiguous": ambiguous,
		"invalid": invalid,
		"default_primary_uom": default_uom,
		"total_configured": len(doc.item_primary_uoms or []),
	}
	doc.last_uom_validation_summary = json.dumps(summary, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return summary


@frappe.whitelist()
def preview_uom_exceptions(settings_name: str):
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("read")
	rows = []
	for row in doc.item_primary_uoms or []:
		apply_row_factors(row)
		if row.conversion_status not in ("Valid", "Manual Override"):
			rows.append(
				{
					"item": row.item,
					"item_name": row.item_name,
					"item_group": row.item_group,
					"stock_uom": row.stock_uom,
					"primary_uom": row.primary_uom,
					"conversion_status": row.conversion_status,
					"remarks": row.remarks,
				}
			)
	return {"count": len(rows), "rows": rows}


@frappe.whitelist()
def add_items_to_primary_uoms(settings_name: str, items: str | list, primary_uom: str | None = None):
	"""Append Items (any Item Group) to Item Primary UOMs. No Item master writes."""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	if isinstance(items, str):
		items = json.loads(items) if items.strip().startswith("[") else [x.strip() for x in items.split(",") if x.strip()]
	default_uom = primary_uom or doc.default_primary_uom
	if not default_uom:
		frappe.throw(_("Primary UOM is required."))
	existing = {r.item for r in (doc.item_primary_uoms or []) if r.item}
	added = []
	skipped = []
	for item in items:
		if item in existing:
			skipped.append(item)
			continue
		if not frappe.db.exists("Item", item):
			frappe.throw(_("Item {0} does not exist.").format(item))
		meta = frappe.db.get_value(
			"Item", item, ["item_name", "item_group", "stock_uom", "disabled"], as_dict=True
		)
		if cint(meta.disabled):
			frappe.throw(_("Item {0} is disabled; enable it or handle historical cases explicitly.").format(item))
		row = doc.append(
			"item_primary_uoms",
			{
				"item": item,
				"item_name": meta.item_name,
				"item_group": meta.item_group,
				"stock_uom": meta.stock_uom,
				"primary_uom": default_uom,
			},
		)
		apply_row_factors(row)
		added.append(item)
		existing.add(item)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"added": added, "skipped_duplicates": skipped, "total": len(doc.item_primary_uoms or [])}
