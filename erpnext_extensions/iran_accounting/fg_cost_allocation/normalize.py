# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Per-item primary UOM normalization from Settings (Item UOM Conversion Detail only)."""

from __future__ import annotations

from decimal import Decimal

from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket
from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import build_primary_uom_map, resolve_conversion


def apply_normalization(
	buckets: dict[tuple[str, str], ProductBucket],
	settings,
) -> list[str]:
	"""Mutate buckets using per-item Primary UOM from Settings. Never invents factors."""
	uom_map = build_primary_uom_map(settings)
	exceptions: list[str] = []
	for bucket in buckets.values():
		cfg = uom_map.get(bucket.item_code)
		if not cfg:
			bucket.missing_uom_factor = True
			bucket.normalization_factor = None
			bucket.normalization_uom = ""
			bucket.normalized_qty = Decimal("0")
			msg = (
				f"No Primary UOM configured for item {bucket.item_code} "
				f"(batch {bucket.batch_no})"
			)
			bucket.exceptions.append(msg)
			exceptions.append(msg)
			continue

		factor = cfg.get("factor")
		status = cfg.get("status") or "Missing"
		primary = cfg.get("primary_uom") or ""
		# Re-resolve live in case Settings row stale
		live_factor, live_status, remarks = resolve_conversion(bucket.item_code, primary)
		factor = live_factor
		status = live_status

		bucket.normalization_uom = primary
		if status != "Valid" or factor is None:
			bucket.missing_uom_factor = True
			bucket.normalization_factor = None
			bucket.normalized_qty = Decimal("0")
			msg = (
				f"UOM {status} for item {bucket.item_code} "
				f"stock→{primary} (batch {bucket.batch_no}): {remarks}"
			)
			bucket.exceptions.append(msg)
			exceptions.append(msg)
			continue

		bucket.missing_uom_factor = False
		bucket.normalization_factor = factor
		bucket.normalized_qty = bucket.actual_qty * factor
	return exceptions
