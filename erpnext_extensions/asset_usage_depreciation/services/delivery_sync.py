# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Synchronize Asset Request fulfillment from submitted Asset Movements.

Approval workflow (``status`` / ``workflow_state``) is never mutated here.
Delivery source of truth: submitted Asset Movement with proven Asset Request
linkage and delivery semantics (Issue / Transfer and Issue / linked Transfer).
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.asset_usage_depreciation.constants import (
	ALLOC_CANCELLED,
	ALLOC_ISSUED,
	ALLOC_MOVEMENT_DRAFT,
	ASSET_REQUEST_DOCTYPE,
	DELIVERY_MOVEMENT_PURPOSES,
	LINKED_TRANSFER_PURPOSE,
	METHOD_ISSUE,
)
from erpnext_extensions.asset_usage_depreciation.services.request_service import (
	refresh_header_fulfillment,
)

_BACKFILL_REASON = "Reconciled from submitted Asset Movement linked to this Asset Request"


def is_delivery_movement(movement_doc) -> bool:
	"""True when a submitted movement represents delivery to an Asset Request."""
	if cint(getattr(movement_doc, "docstatus", 0)) != 1:
		return False
	purpose = (getattr(movement_doc, "purpose", None) or "").strip()
	if purpose in DELIVERY_MOVEMENT_PURPOSES:
		return True
	# Historical / manual Desk Transfer with explicit AR reference.
	if (
		purpose == LINKED_TRANSFER_PURPOSE
		and getattr(movement_doc, "reference_doctype", None) == ASSET_REQUEST_DOCTYPE
		and getattr(movement_doc, "reference_name", None)
	):
		return True
	return False


def resolve_asset_request_for_movement(movement_doc) -> str | None:
	"""Resolve AR name from reference or from allocations pointing at this movement."""
	if (
		getattr(movement_doc, "reference_doctype", None) == ASSET_REQUEST_DOCTYPE
		and getattr(movement_doc, "reference_name", None)
	):
		if frappe.db.exists(ASSET_REQUEST_DOCTYPE, movement_doc.reference_name):
			return movement_doc.reference_name

	rows = frappe.get_all(
		"Asset Request Allocation",
		filters={"asset_movement": movement_doc.name, "fulfillment_status": ("!=", ALLOC_CANCELLED)},
		fields=["parent"],
		limit=5,
	)
	parents = {r.parent for r in rows if r.parent}
	if len(parents) == 1:
		return next(iter(parents))
	return None


def on_asset_movement_submit(movement_doc) -> None:
	"""Promote draft allocations / backfill proven links after AM submit."""
	if frappe.flags.get("asset_request_fulfillment_in_progress"):
		return
	if not is_delivery_movement(movement_doc):
		return
	ar_name = resolve_asset_request_for_movement(movement_doc)
	if not ar_name:
		return
	sync_request_from_movement(ar_name, movement_doc, create_missing_allocations=True)


def sync_request_from_movement(
	ar_name: str,
	movement_doc,
	*,
	create_missing_allocations: bool = True,
) -> dict:
	"""Idempotent sync of one AR from one submitted delivery movement.

	Returns a small report dict for reconciliation / tests.
	"""
	report = {
		"asset_request": ar_name,
		"asset_movement": movement_doc.name,
		"promoted": 0,
		"created_allocations": 0,
		"skipped_assets": [],
		"ambiguous": [],
		"changed": False,
	}
	if not is_delivery_movement(movement_doc):
		report["ambiguous"].append("not_a_delivery_movement")
		return report
	if cint(frappe.db.get_value(ASSET_REQUEST_DOCTYPE, ar_name, "docstatus")) != 1:
		report["ambiguous"].append("request_not_submitted")
		return report

	frappe.flags.asset_request_fulfillment_in_progress = True
	try:
		ar = frappe.get_doc(ASSET_REQUEST_DOCTYPE, ar_name)
		changed = False

		# 1) Promote Movement Draft → Issued for rows already linked to this AM.
		for alloc in ar.get("allocations") or []:
			if alloc.fulfillment_status == ALLOC_CANCELLED:
				continue
			if alloc.asset_movement == movement_doc.name and alloc.fulfillment_status == ALLOC_MOVEMENT_DRAFT:
				alloc.fulfillment_status = ALLOC_ISSUED
				report["promoted"] += 1
				changed = True

		# 2) Backfill missing allocations for assets on the movement (manual / historical).
		if create_missing_allocations:
			created, skipped, ambiguous = _backfill_allocations_from_movement(ar, movement_doc)
			report["created_allocations"] = created
			report["skipped_assets"] = skipped
			report["ambiguous"].extend(ambiguous)
			if created:
				changed = True

		if changed or _qty_fields_need_refresh(ar):
			refresh_header_fulfillment(ar)
			ar.flags.ignore_validate_update_after_submit = True
			ar.save(ignore_permissions=True)
			report["changed"] = True
		return report
	finally:
		frappe.flags.asset_request_fulfillment_in_progress = False


def _qty_fields_need_refresh(ar) -> bool:
	"""Force refresh when delivered_qty fields are missing/stale on load."""
	for row in ar.get("items") or []:
		if not hasattr(row, "delivered_qty"):
			return False
		# Always recompute via refresh when any delivered_qty is unset after schema add.
		if row.delivered_qty is None:
			return True
	return False


def _allocated_assets(ar) -> set[str]:
	out = set()
	for alloc in ar.get("allocations") or []:
		if alloc.fulfillment_status == ALLOC_CANCELLED:
			continue
		if alloc.allocated_asset:
			out.add(alloc.allocated_asset)
	return out


def _item_remaining_capacity(ar) -> dict[str, int]:
	"""Remaining units per Asset Request Item row name."""
	delivered = _delivered_by_item(ar)
	capacity: dict[str, int] = {}
	for row in ar.get("items") or []:
		capacity[row.name] = max(0, cint(row.qty) - cint(delivered.get(row.name, 0)))
	return capacity


def _delivered_by_item(ar) -> dict[str, int]:
	"""Count unique Issued allocations per request item (no double-count).

	Only ``ALLOC_ISSUED`` counts as delivered. Draft movements, cancelled rows,
	and purchase Received-without-Issue do not.
	"""
	seen_assets: set[str] = set()
	seen_keys: set[tuple[str, str]] = set()
	by_item: dict[str, int] = {}
	for alloc in ar.get("allocations") or []:
		if alloc.fulfillment_status != ALLOC_ISSUED:
			continue
		asset = (alloc.allocated_asset or "").strip()
		key = (alloc.asset_movement or "", asset or alloc.name)
		if asset and asset in seen_assets:
			continue
		if key in seen_keys:
			continue
		if asset:
			seen_assets.add(asset)
		seen_keys.add(key)
		item_key = alloc.asset_request_item or ""
		by_item[item_key] = by_item.get(item_key, 0) + 1
	return by_item


def _match_item_for_asset(ar, item_code: str | None) -> object | None:
	"""Match an AM asset's item_code to an AR item with remaining capacity."""
	if not item_code:
		return None
	capacity = _item_remaining_capacity(ar)
	candidates = []
	for row in ar.get("items") or []:
		if capacity.get(row.name, 0) <= 0:
			continue
		fulfilled = (row.fulfilled_item_code or "").strip()
		requested = (row.requested_item_code or "").strip()
		if item_code in (fulfilled, requested):
			candidates.append(row)
	if len(candidates) == 1:
		return candidates[0]
	if not candidates:
		return None
	# Prefer exact fulfilled_item_code match, then first with capacity.
	for row in candidates:
		if (row.fulfilled_item_code or "").strip() == item_code:
			return row
	return candidates[0]


def _backfill_allocations_from_movement(ar, movement_doc) -> tuple[int, list, list]:
	created = 0
	skipped: list[str] = []
	ambiguous: list[str] = []
	already = _allocated_assets(ar)

	for row in movement_doc.get("assets") or []:
		asset = (getattr(row, "asset", None) or "").strip()
		if not asset:
			continue
		if asset in already:
			skipped.append(asset)
			continue
		item_code = frappe.db.get_value("Asset", asset, "item_code")
		ar_item = _match_item_for_asset(ar, item_code)
		if not ar_item:
			ambiguous.append(f"no_item_match:{asset}:{item_code}")
			continue
		# Capacity check after tentative match
		if _item_remaining_capacity(ar).get(ar_item.name, 0) <= 0:
			ambiguous.append(f"over_capacity:{asset}:{ar_item.name}")
			continue

		ar.append(
			"allocations",
			{
				"asset_request_item": ar_item.name,
				"requested_item_code": ar_item.requested_item_code,
				"fulfilled_item_code": ar_item.fulfilled_item_code or ar_item.requested_item_code,
				"fulfilled_purchase_item": ar_item.fulfilled_purchase_item
				or ar_item.fulfilled_item_code
				or ar_item.requested_item_code,
				"method": METHOD_ISSUE,
				"fulfillment_status": ALLOC_ISSUED,
				"allocated_asset": asset,
				"asset_movement": movement_doc.name,
				"substitution_reason": _BACKFILL_REASON,
			},
		)
		already.add(asset)
		created += 1
	return created, skipped, ambiguous


def reconcile_asset_request_fulfillment(
	*,
	asset_request: str | None = None,
	dry_run: bool = False,
) -> dict:
	"""Idempotent historical reconciliation of proven deliveries.

	Does not create Asset Movements. Creates missing allocations only when a
	submitted delivery movement explicitly references the Asset Request.
	"""
	filters = {
		"reference_doctype": ASSET_REQUEST_DOCTYPE,
		"docstatus": 1,
	}
	if asset_request:
		filters["reference_name"] = asset_request

	movements = frappe.get_all(
		"Asset Movement",
		filters=filters,
		fields=["name", "purpose", "reference_name", "docstatus"],
		order_by="modified asc",
	)

	summary = {
		"scanned": 0,
		"updated": [],
		"unchanged": [],
		"skipped": [],
		"ambiguous": [],
		"dry_run": dry_run,
	}

	for row in movements:
		summary["scanned"] += 1
		am = frappe.get_doc("Asset Movement", row.name)
		if not is_delivery_movement(am):
			summary["skipped"].append({"movement": am.name, "reason": "not_delivery_purpose"})
			continue
		ar_name = am.reference_name
		if not ar_name or not frappe.db.exists(ASSET_REQUEST_DOCTYPE, ar_name):
			summary["skipped"].append({"movement": am.name, "reason": "missing_request"})
			continue
		if dry_run:
			# Simulate capacity / match without save
			ar = frappe.get_doc(ASSET_REQUEST_DOCTYPE, ar_name)
			_promotable = sum(
				1
				for a in (ar.get("allocations") or [])
				if a.asset_movement == am.name and a.fulfillment_status == ALLOC_MOVEMENT_DRAFT
			)
			_created, _skipped, amb = _backfill_allocations_from_movement(ar, am)
			# rollback in-memory appends by not saving; discard doc
			entry = {
				"movement": am.name,
				"asset_request": ar_name,
				"would_promote": _promotable,
				"would_create": _created,
				"ambiguous": amb,
			}
			if amb and not _created and not _promotable:
				summary["ambiguous"].append(entry)
			elif _created or _promotable:
				summary["updated"].append(entry)
			else:
				summary["unchanged"].append(entry)
			continue

		report = sync_request_from_movement(ar_name, am, create_missing_allocations=True)
		if report.get("ambiguous") and not report.get("changed"):
			summary["ambiguous"].append(report)
		elif report.get("changed"):
			summary["updated"].append(report)
		else:
			summary["unchanged"].append(report)

	# Also re-stamp qty fields for approved requests that already have Issued allocs
	# but stale fulfillment_status (no new movements).
	ar_filters = {"docstatus": 1}
	if asset_request:
		ar_filters["name"] = asset_request
	for name in frappe.get_all(ASSET_REQUEST_DOCTYPE, filters=ar_filters, pluck="name"):
		if dry_run:
			continue
		if frappe.flags.get("asset_request_fulfillment_in_progress"):
			continue
		frappe.flags.asset_request_fulfillment_in_progress = True
		try:
			ar = frappe.get_doc(ASSET_REQUEST_DOCTYPE, name)
			before = ar.fulfillment_status
			refresh_header_fulfillment(ar)
			if (
				ar.fulfillment_status != before
				or any(cint(getattr(i, "delivered_qty", 0)) for i in (ar.items or []))
			):
				ar.flags.ignore_validate_update_after_submit = True
				ar.save(ignore_permissions=True)
				if name not in {u.get("asset_request") for u in summary["updated"] if isinstance(u, dict)}:
					summary["updated"].append(
						{"asset_request": name, "asset_movement": None, "refreshed": True}
					)
		finally:
			frappe.flags.asset_request_fulfillment_in_progress = False

	return summary
