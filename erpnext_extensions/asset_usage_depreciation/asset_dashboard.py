# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Additive Asset dashboard extension for Usage Period and Asset Request.

Also sanitizes a known site customization that duplicated Asset Movement
under a second group and overwrote the core ``asset`` filter with
``asset_name`` (display title). Canonical ERPNext relationship is preserved:

Movement → Asset Movement, non_standard_fieldnames["Asset Movement"] = "asset"
"""

from __future__ import annotations

import frappe
from frappe import _


ASSET_MOVEMENT = "Asset Movement"
MOVEMENT_GROUP = "Movement"


def get_data(data=None):
	"""Extend the ERPNext Asset dashboard additively — does not replace core groups."""
	data = frappe._dict(data or {})

	if not data.get("non_standard_fieldnames"):
		data.non_standard_fieldnames = {}
	data.non_standard_fieldnames["Asset Usage Period"] = "asset"

	if not data.get("internal_links"):
		data.internal_links = {}
	data.internal_links["Asset Request"] = ["allocations", "allocated_asset"]

	if not data.get("transactions"):
		data.transactions = []

	_append_group(data, _("Usage"), "Asset Usage Period")
	_append_group(data, _("Request"), "Asset Request")
	_sanitize_asset_movement(data)
	return data


def _append_group(data, label: str, item: str) -> None:
	for group in data.transactions:
		if _(group.get("label") or "") == label:
			items = group.setdefault("items", [])
			if item not in items:
				items.append(item)
			return
	data.transactions.append({"label": label, "items": [item]})


def _sanitize_asset_movement(data) -> None:
	"""Keep Asset Movement once under Movement with the core ``asset`` filter."""
	if not data.get("non_standard_fieldnames"):
		data.non_standard_fieldnames = {}
	data.non_standard_fieldnames[ASSET_MOVEMENT] = "asset"

	transactions = list(data.get("transactions") or [])
	if not transactions:
		return

	canonical_idx = None
	for idx, group in enumerate(transactions):
		label = _(group.get("label") or "")
		items = group.get("items") or []
		if label == _(MOVEMENT_GROUP) and ASSET_MOVEMENT in items:
			canonical_idx = idx
			break

	if canonical_idx is None:
		for idx, group in enumerate(transactions):
			if ASSET_MOVEMENT in (group.get("items") or []):
				canonical_idx = idx
				break

	if canonical_idx is None:
		return

	sanitized = []
	for idx, group in enumerate(transactions):
		items = list(group.get("items") or [])
		if idx == canonical_idx:
			# Ensure AM once; preserve other items in the Movement group.
			deduped = []
			for item in items:
				if item not in deduped:
					deduped.append(item)
			if ASSET_MOVEMENT not in deduped:
				deduped.insert(0, ASSET_MOVEMENT)
			row = dict(group)
			row["items"] = deduped
			# Prefer the native Movement label when this was already Movement.
			if _(row.get("label") or "") == _(MOVEMENT_GROUP):
				row["label"] = _(MOVEMENT_GROUP)
			sanitized.append(row)
			continue

		if ASSET_MOVEMENT not in items:
			sanitized.append(group)
			continue

		remaining = [i for i in items if i != ASSET_MOVEMENT]
		if not remaining:
			# Drop empty duplicate group (e.g. "Asset Movement" → only AM).
			continue
		row = dict(group)
		row["items"] = remaining
		sanitized.append(row)

	data.transactions = sanitized
