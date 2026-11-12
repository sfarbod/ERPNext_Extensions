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

		# Use Applied Conversion Factor from Settings (includes Manual Override).
		# Do NOT re-resolve from Item master only — that would discard Costing overrides.
		factor = cfg.get("factor")
		status = cfg.get("status") or "Missing"
		primary = cfg.get("primary_uom") or ""
		remarks = cfg.get("remarks") or ""

		bucket.normalization_uom = primary
		ok = status in ("Valid", "Manual Override") and factor is not None and factor > 0
		if not ok:
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
		# Convention: Normalized Qty = Actual Qty (Stock UOM) × Applied Conversion Factor
		bucket.normalized_qty = bucket.actual_qty * factor
	return exceptions
