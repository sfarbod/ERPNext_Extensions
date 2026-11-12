"""Verify Item 20100213 UOM resolution sources without mutating masters."""

from __future__ import annotations

import json
from pathlib import Path

import frappe

from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import (
	apply_row_factors,
	build_primary_uom_map,
)


def main():
	item = "20100213"
	settings_name = frappe.db.get_value(
		"Finished Goods Cost Allocation Settings",
		{"company": "اسپاد فارمد دارو"},
		"name",
	)
	settings = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	stock_uom = frappe.db.get_value("Item", item, "stock_uom")
	item_convs = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": item},
		fields=["uom", "conversion_factor"],
	)
	fg_any = frappe.db.sql(
		"""
		SELECT name, parent, parenttype, item, stock_uom, primary_uom,
		       source_conversion_factor, manual_override_factor, conversion_factor,
		       conversion_status, conversion_source, override_reason
		FROM `tabFG Item Primary UOM`
		WHERE item=%s
		""",
		item,
		as_dict=True,
	)
	uom_map = build_primary_uom_map(settings)
	out = {
		"item": item,
		"stock_uom": stock_uom,
		"default_primary_uom": settings.default_primary_uom,
		"item_uom_conversion_detail": item_convs,
		"fg_item_primary_uom": fg_any,
		"resolved_from_settings_map": uom_map.get(item),
		"correction_type": "Costing Manual Override (FG Item Primary UOM)",
		"item_master_unchanged": True,
	}
	# Re-apply factors on a detached view of the child row for evidence
	if settings.item_primary_uoms:
		for row in settings.item_primary_uoms:
			if row.item == item:
				apply_row_factors(row)
				out["apply_row_factors"] = {
					"source_conversion_factor": row.source_conversion_factor,
					"manual_override_factor": row.manual_override_factor,
					"conversion_factor": row.conversion_factor,
					"conversion_status": row.conversion_status,
					"conversion_source": row.conversion_source,
				}
				break
	Path("/tmp/costing_qa_v5727").mkdir(parents=True, exist_ok=True)
	Path("/tmp/costing_qa_v5727/uom_20100213.json").write_text(
		json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
	)
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
