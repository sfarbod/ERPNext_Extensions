# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""UOM normalization using Item UOM Conversion Detail."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket


def get_item_stock_uom(item_code: str) -> str:
	return frappe.db.get_value("Item", item_code, "stock_uom") or ""


def get_conversion_factor(item_code: str, target_uom: str) -> Decimal | None:
	"""Return common units per 1 stock UOM (site convention), or None if missing."""
	if not item_code or not target_uom:
		return None
	stock_uom = get_item_stock_uom(item_code)
	if stock_uom == target_uom:
		return Decimal("1")
	factor = frappe.db.get_value(
		"UOM Conversion Detail",
		{"parent": item_code, "uom": target_uom},
		"conversion_factor",
	)
	if factor in (None, ""):
		return None
	value = Decimal(str(flt(factor)))
	return value if value > 0 else None


def apply_normalization(
	buckets: dict[tuple[str, str], ProductBucket], normalization_uom: str
) -> list[str]:
	"""Mutate buckets with factor/normalized_qty. Return exception messages."""
	exceptions: list[str] = []
	for bucket in buckets.values():
		bucket.normalization_uom = normalization_uom or ""
		factor = get_conversion_factor(bucket.item_code, normalization_uom) if normalization_uom else None
		if factor is None:
			bucket.missing_uom_factor = True
			bucket.normalization_factor = None
			bucket.normalized_qty = Decimal("0")
			msg = (
				f"Missing UOM conversion for item {bucket.item_code} "
				f"to {normalization_uom or '(unset)'} (batch {bucket.batch_no})"
			)
			bucket.exceptions.append(msg)
			exceptions.append(msg)
			continue
		bucket.missing_uom_factor = False
		bucket.normalization_factor = factor
		bucket.normalized_qty = bucket.actual_qty * factor
	return exceptions
