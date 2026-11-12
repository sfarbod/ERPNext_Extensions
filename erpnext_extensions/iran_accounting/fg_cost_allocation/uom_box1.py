# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""BOX(1pfs) Primary UOM standardization helpers.

Costing convention (documented):
  Applied Conversion Factor = number of Primary UOM units contained in 1 Stock UOM unit
  Normalized Quantity = Actual Quantity (Stock UOM) × Applied Conversion Factor

ERPNext Item UOM Conversion Detail stores: 1 <UOM> = conversion_factor × Stock UOM.
When that detail exists for Primary UOM, Costing Applied Factor = 1 / ERPNext_cf
(when ERPNext_cf > 0), except when Stock UOM already equals Primary UOM (factor = 1).

Packaging Stock UOMs BOX(1pfs)/BOX(2pfs)/BOX(4pfs) encode verified pack sizes.
When Primary is BOX(1pfs) and Stock is BOX(Npfs) with N in {1,2,4} and no usable
ERPNext conversion row exists, Applied Factor = N with source
"Stock UOM Packaging Convention" (not an invented arbitrary value).

Other Stock UOMs without a verifiable BOX(1pfs) relationship remain exceptions.
"""

from __future__ import annotations

import re
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import (
	apply_row_factors,
	get_item_stock_uom,
	resolve_conversion,
)

PRIMARY_BOX1 = "BOX(1pfs)"
_PACK_RE = re.compile(r"^BOX\((\d+)pfs\)$", re.IGNORECASE)


def packaging_factor_for_stock_uom(stock_uom: str | None) -> Decimal | None:
	"""Return N for Stock UOM BOX(Npfs) when N in {1,2,4}; else None."""
	if not stock_uom:
		return None
	if stock_uom == PRIMARY_BOX1:
		return Decimal("1")
	m = _PACK_RE.match(stock_uom.strip())
	if not m:
		return None
	n = int(m.group(1))
	if n in (1, 2, 4):
		return Decimal(n)
	return None


def resolve_box1_applied_factor(item_code: str) -> dict:
	"""Resolve Applied factor for Primary BOX(1pfs) without writing Item masters."""
	stock = get_item_stock_uom(item_code)
	out = {
		"item": item_code,
		"stock_uom": stock,
		"primary_uom": PRIMARY_BOX1,
		"source_conversion_factor": 0.0,
		"applied_factor": None,
		"conversion_status": "Missing",
		"conversion_source": "Missing",
		"remarks": "",
		"packaging_factor": float(packaging_factor_for_stock_uom(stock) or 0) or None,
	}
	if not item_code:
		out["remarks"] = "Item not set"
		return out
	if stock == PRIMARY_BOX1:
		out.update(
			{
				"source_conversion_factor": 1.0,
				"applied_factor": 1.0,
				"conversion_status": "Valid",
				"conversion_source": "ERPNext",
				"remarks": "Stock UOM equals Primary UOM BOX(1pfs)",
			}
		)
		return out

	erp_cf, status, remarks = resolve_conversion(item_code, PRIMARY_BOX1)
	if erp_cf is not None and status == "Valid" and erp_cf > 0:
		# ERPNext: 1 BOX(1pfs) = erp_cf × Stock → primary_per_stock = 1/erp_cf
		applied = (Decimal("1") / Decimal(str(erp_cf))).normalize()
		out.update(
			{
				"source_conversion_factor": float(erp_cf),
				"applied_factor": float(applied),
				"conversion_status": "Valid",
				"conversion_source": "ERPNext",
				"remarks": f"Derived as 1/{erp_cf} from Item UOM Conversion Detail ({remarks})",
			}
		)
		return out

	pack = packaging_factor_for_stock_uom(stock)
	if pack is not None:
		out.update(
			{
				"source_conversion_factor": float(erp_cf) if erp_cf is not None else 0.0,
				"applied_factor": float(pack),
				"conversion_status": "Manual Override",
				"conversion_source": "Manual Override",
				"remarks": (
					f"Stock UOM packaging convention: 1 {stock} = {pack} × {PRIMARY_BOX1}. "
					f"ERPNext BOX(1pfs) conversion: {status} ({remarks})"
				),
			}
		)
		return out

	out.update(
		{
			"source_conversion_factor": float(erp_cf) if erp_cf is not None else 0.0,
			"conversion_status": status if status else "Missing",
			"conversion_source": "Missing",
			"remarks": remarks
			or f"No verified conversion from Stock UOM {stock} to {PRIMARY_BOX1}",
		}
	)
	return out


@frappe.whitelist()
def apply_box1_primary_uoms(settings_name: str, only_missing: int = 0):
	"""Set Primary UOM to BOX(1pfs) for configured Items; assign verified factors only."""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	if not frappe.db.exists("UOM", PRIMARY_BOX1):
		frappe.throw(_("UOM {0} does not exist.").format(PRIMARY_BOX1))

	doc.default_primary_uom = PRIMARY_BOX1
	updated = exceptions = unchanged = 0
	details = []
	for row in doc.item_primary_uoms or []:
		if not row.item:
			continue
		if cint(only_missing) and row.conversion_status in ("Valid", "Manual Override") and row.primary_uom == PRIMARY_BOX1:
			unchanged += 1
			continue
		resolved = resolve_box1_applied_factor(row.item)
		row.primary_uom = PRIMARY_BOX1
		row.stock_uom = resolved["stock_uom"]
		meta = frappe.db.get_value("Item", row.item, ["item_name", "item_group"], as_dict=True) or {}
		row.item_name = meta.get("item_name") or row.item_name
		row.item_group = meta.get("item_group") or row.item_group
		row.source_conversion_factor = resolved["source_conversion_factor"] or 0
		if resolved["applied_factor"] is not None and resolved["conversion_status"] == "Manual Override":
			row.manual_override_factor = resolved["applied_factor"]
			row.override_reason = resolved["remarks"][:140]
		elif resolved["applied_factor"] is not None and resolved["conversion_status"] == "Valid":
			row.manual_override_factor = None
			row.override_reason = None
		else:
			row.manual_override_factor = None
		apply_row_factors(row)
		# Ensure packaging override lands as applied even if apply_row_factors path differs
		if resolved["applied_factor"] is not None:
			if resolved["conversion_source"] == "Manual Override":
				row.manual_override_factor = resolved["applied_factor"]
				apply_row_factors(row)
			row.conversion_status = resolved["conversion_status"]
			row.conversion_source = resolved["conversion_source"]
			row.remarks = resolved["remarks"]
			row.conversion_factor = float(resolved["applied_factor"])
			updated += 1
		else:
			row.conversion_status = resolved["conversion_status"]
			row.conversion_source = "Missing"
			row.remarks = resolved["remarks"]
			row.conversion_factor = 0
			exceptions += 1
		details.append(
			{
				"item": row.item,
				"stock_uom": row.stock_uom,
				"applied": row.conversion_factor,
				"status": row.conversion_status,
				"remarks": row.remarks,
			}
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"default_primary_uom": PRIMARY_BOX1,
		"updated": updated,
		"exceptions": exceptions,
		"unchanged": unchanged,
		"total": len(doc.item_primary_uoms or []),
		"details_sample": details[:40],
		"exception_items": [d for d in details if d["status"] not in ("Valid", "Manual Override")],
	}


def cint(v):
	from frappe.utils import cint as _c

	return _c(v)
