# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset Scrap/Restore valuation integrity (v5.5.53).

ERPNext core ``restore_asset`` reverses the disposal-day depreciation Journal Entry
and restores Finance Book ``value_after_depreciation``, but does **not** restore
Asset-level ``value_after_depreciation``.

Scrap-day depreciation is posted as a Depreciation Entry whose submit path
(``Journal Entry.update_asset_on_depreciation``) reduces **both** Asset and
Finance Book VAD. The restoring reverse entry has expense **credit** (not debit),
so that same path never adds Asset VAD back. Core only patches the Finance Book
row in ``update_value_after_depreciation_on_asset_restore``.

Repeated Scrap → Restore therefore leaves Asset VAD permanently reduced by each
disposal-day depreciation while Finance Book VAD returns to baseline — progressive
NBV leakage and divergent scrap loss amounts.

This module re-synchronises Asset VAD to the Finance Book after restore (and
asserts parity after scrap) without modifying ERPNext core.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt


def sync_asset_vad_from_finance_book(asset_name: str) -> dict:
	"""Set Asset.value_after_depreciation = primary Finance Book VAD.

	Idempotent. Returns before/after for audit.
	"""
	if not asset_name or not frappe.db.exists("Asset", asset_name):
		return {"asset": asset_name, "skipped": "missing_asset"}
	asset = frappe.get_doc("Asset", asset_name)
	if not asset.get("finance_books"):
		return {"asset": asset_name, "skipped": "no_finance_books"}

	fb = asset.finance_books[0]
	before = flt(asset.value_after_depreciation)
	fb_vad = flt(fb.value_after_depreciation)
	changed = abs(before - fb_vad) > 1e-9
	if changed:
		asset.db_set("value_after_depreciation", fb_vad, update_modified=False)
	# Booked count can drift after scrap/restore schedule rebuilds.
	try:
		asset.set_total_booked_depreciations()
	except Exception:
		pass
	return {
		"asset": asset_name,
		"changed": changed,
		"asset_vad_before": before,
		"asset_vad_after": fb_vad,
		"fb_vad": fb_vad,
	}


def assert_asset_fb_vad_parity(asset_name: str, *, context: str = "") -> dict:
	"""Return parity report; does not throw (callers decide)."""
	asset = frappe.get_doc("Asset", asset_name)
	fb_vad = flt(asset.finance_books[0].value_after_depreciation) if asset.finance_books else None
	asset_vad = flt(asset.value_after_depreciation)
	return {
		"asset": asset_name,
		"context": context,
		"asset_vad": asset_vad,
		"fb_vad": fb_vad,
		"in_sync": fb_vad is not None and abs(asset_vad - fb_vad) < 1e-9,
	}
