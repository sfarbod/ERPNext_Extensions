# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Synchronize Asset Request fulfillment from submitted Asset Movements.

Approval workflow (``status`` / ``workflow_state``) is never mutated here.

Delivery eligibility (v5.5.52 hardened):
- Submitted (docstatus=1) Asset Movement linked to the Asset Request
- Purpose Issue / Transfer and Issue, OR Transfer with proven employee handover
- Plain Transfer with only reference_doctype/reference_name is NOT enough
- Recipient ``to_employee`` must match the request employee when assignment applies
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

_BACKFILL_REASON = "Reconciled from submitted Asset Movement with proven delivery handover"


def evaluate_delivery_eligibility(movement_doc, ar_employee: str | None = None) -> dict:
	"""Deterministic delivery eligibility for one Asset Movement.

	Returns::
	    {
	      "eligible": bool,
	      "reason": str,
	      "eligible_assets": list[str],  # asset names that may count
	      "ambiguous_assets": list[str],
	    }
	"""
	result = {
		"eligible": False,
		"reason": "",
		"eligible_assets": [],
		"ambiguous_assets": [],
	}
	if cint(getattr(movement_doc, "docstatus", 0)) != 1:
		result["reason"] = "not_submitted"
		return result

	ar_name = None
	if (
		getattr(movement_doc, "reference_doctype", None) == ASSET_REQUEST_DOCTYPE
		and getattr(movement_doc, "reference_name", None)
	):
		ar_name = movement_doc.reference_name
	if not ar_name:
		# Fallback: unique allocation parent
		rows = frappe.get_all(
			"Asset Request Allocation",
			filters={
				"asset_movement": movement_doc.name,
				"fulfillment_status": ("!=", ALLOC_CANCELLED),
			},
			fields=["parent"],
			limit=5,
		)
		parents = {r.parent for r in rows if r.parent}
		if len(parents) == 1:
			ar_name = next(iter(parents))
		else:
			result["reason"] = "not_linked_to_asset_request"
			return result

	if not frappe.db.exists(ASSET_REQUEST_DOCTYPE, ar_name):
		result["reason"] = "missing_request"
		return result

	if not ar_employee:
		ar_employee = frappe.db.get_value(ASSET_REQUEST_DOCTYPE, ar_name, "employee") or ""
	ar_employee = (ar_employee or "").strip()

	purpose = (getattr(movement_doc, "purpose", None) or "").strip()
	eligible_assets: list[str] = []
	ambiguous_assets: list[str] = []

	for row in movement_doc.get("assets") or []:
		asset = (getattr(row, "asset", None) or "").strip()
		if not asset:
			continue
		to_emp = (getattr(row, "to_employee", None) or "").strip()

		if purpose in DELIVERY_MOVEMENT_PURPOSES:
			# Issue / Transfer and Issue: count unless recipient explicitly mismatches.
			if to_emp and ar_employee and to_emp != ar_employee:
				ambiguous_assets.append(asset)
				continue
			eligible_assets.append(asset)
			continue

		if purpose == LINKED_TRANSFER_PURPOSE:
			# Transfer requires positive handover proof to the requesting employee.
			if not ar_employee:
				ambiguous_assets.append(asset)
				continue
			if to_emp == ar_employee:
				eligible_assets.append(asset)
			else:
				# Location-only or wrong employee — not delivery.
				ambiguous_assets.append(asset)
			continue

		# Receipt / other purposes never count.
		ambiguous_assets.append(asset)

	if purpose == LINKED_TRANSFER_PURPOSE and not eligible_assets:
		result["reason"] = "transfer_without_employee_handover"
		result["ambiguous_assets"] = ambiguous_assets
		return result

	if purpose not in DELIVERY_MOVEMENT_PURPOSES and purpose != LINKED_TRANSFER_PURPOSE:
		result["reason"] = f"unsupported_purpose:{purpose}"
		result["ambiguous_assets"] = ambiguous_assets
		return result

	if not eligible_assets:
		result["reason"] = "no_eligible_assets"
		result["ambiguous_assets"] = ambiguous_assets
		return result

	result["eligible"] = True
	result["reason"] = "ok"
	result["eligible_assets"] = eligible_assets
	result["ambiguous_assets"] = ambiguous_assets
	result["asset_request"] = ar_name
	return result


def is_delivery_movement(movement_doc, ar_employee: str | None = None) -> bool:
	"""True when a submitted movement has at least one eligible delivery asset."""
	return bool(evaluate_delivery_eligibility(movement_doc, ar_employee=ar_employee)["eligible"])


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
	ar_name = resolve_asset_request_for_movement(movement_doc)
	if not ar_name:
		return
	ar_employee = frappe.db.get_value(ASSET_REQUEST_DOCTYPE, ar_name, "employee")
	if not is_delivery_movement(movement_doc, ar_employee=ar_employee):
		return
	sync_request_from_movement(ar_name, movement_doc, create_missing_allocations=True)


def sync_request_from_movement(
	ar_name: str,
	movement_doc,
	*,
	create_missing_allocations: bool = True,
) -> dict:
	"""Idempotent sync of one AR from one submitted delivery movement."""
	report = {
		"asset_request": ar_name,
		"asset_movement": movement_doc.name,
		"promoted": 0,
		"created_allocations": 0,
		"skipped_assets": [],
		"ambiguous": [],
		"changed": False,
		"eligibility": None,
	}
	if cint(frappe.db.get_value(ASSET_REQUEST_DOCTYPE, ar_name, "docstatus")) != 1:
		report["ambiguous"].append("request_not_submitted")
		return report

	ar_employee = frappe.db.get_value(ASSET_REQUEST_DOCTYPE, ar_name, "employee")
	eligibility = evaluate_delivery_eligibility(movement_doc, ar_employee=ar_employee)
	report["eligibility"] = eligibility
	if not eligibility.get("eligible"):
		report["ambiguous"].append(eligibility.get("reason") or "not_a_delivery_movement")
		return report

	eligible_set = set(eligibility.get("eligible_assets") or [])

	frappe.flags.asset_request_fulfillment_in_progress = True
	try:
		ar = frappe.get_doc(ASSET_REQUEST_DOCTYPE, ar_name)
		changed = False

		# 1) Promote Movement Draft → Issued only for eligible assets.
		for alloc in ar.get("allocations") or []:
			if alloc.fulfillment_status == ALLOC_CANCELLED:
				continue
			if alloc.asset_movement != movement_doc.name:
				continue
			if alloc.fulfillment_status != ALLOC_MOVEMENT_DRAFT:
				continue
			asset = (alloc.allocated_asset or "").strip()
			if asset and asset not in eligible_set:
				report["ambiguous"].append(f"draft_ineligible:{asset}")
				continue
			alloc.fulfillment_status = ALLOC_ISSUED
			report["promoted"] += 1
			changed = True

		# 2) Backfill missing allocations for eligible assets only.
		if create_missing_allocations:
			created, skipped, ambiguous = _backfill_allocations_from_movement(
				ar, movement_doc, eligible_assets=eligible_set
			)
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
	for row in ar.get("items") or []:
		if not hasattr(row, "delivered_qty"):
			return False
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
	delivered = _delivered_by_item(ar)
	capacity: dict[str, int] = {}
	for row in ar.get("items") or []:
		capacity[row.name] = max(0, cint(row.qty) - cint(delivered.get(row.name, 0)))
	return capacity


def _delivered_by_item(ar) -> dict[str, int]:
	"""Count unique Issued allocations per request item (no double-count)."""
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
	for row in candidates:
		if (row.fulfilled_item_code or "").strip() == item_code:
			return row
	return candidates[0]


def _backfill_allocations_from_movement(
	ar, movement_doc, *, eligible_assets: set[str] | None = None
) -> tuple[int, list, list]:
	created = 0
	skipped: list[str] = []
	ambiguous: list[str] = []
	already = _allocated_assets(ar)
	allow = eligible_assets  # None = allow all (caller should pass eligibility set)

	for row in movement_doc.get("assets") or []:
		asset = (getattr(row, "asset", None) or "").strip()
		if not asset:
			continue
		if allow is not None and asset not in allow:
			ambiguous.append(f"ineligible_asset:{asset}")
			continue
		if asset in already:
			skipped.append(asset)
			continue
		item_code = frappe.db.get_value("Asset", asset, "item_code")
		ar_item = _match_item_for_asset(ar, item_code)
		if not ar_item:
			ambiguous.append(f"no_item_match:{asset}:{item_code}")
			continue
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


def demote_ineligible_transfer_allocations(*, asset_request: str | None = None, dry_run: bool = False) -> dict:
	"""Cancel Issued allocations backed only by Transfer without employee handover.

	Does not delete rows or Asset Movements. Idempotent.
	"""
	summary = {
		"scanned_allocations": 0,
		"demoted": [],
		"kept": [],
		"dry_run": dry_run,
	}
	filters = {
		"fulfillment_status": ALLOC_ISSUED,
		"method": METHOD_ISSUE,
	}
	if asset_request:
		filters["parent"] = asset_request

	rows = frappe.get_all(
		"Asset Request Allocation",
		filters=filters,
		fields=["name", "parent", "allocated_asset", "asset_movement"],
	)
	by_movement: dict[str, list] = {}
	for row in rows:
		if not row.asset_movement:
			continue
		by_movement.setdefault(row.asset_movement, []).append(row)

	for am_name, allocs in by_movement.items():
		am = frappe.get_doc("Asset Movement", am_name)
		if (am.purpose or "").strip() != LINKED_TRANSFER_PURPOSE:
			for a in allocs:
				summary["kept"].append({"allocation": a.name, "reason": "non_transfer_purpose"})
			continue
		ar_employee = frappe.db.get_value(ASSET_REQUEST_DOCTYPE, allocs[0].parent, "employee")
		eligibility = evaluate_delivery_eligibility(am, ar_employee=ar_employee)
		eligible = set(eligibility.get("eligible_assets") or [])
		parents_to_refresh: set[str] = set()
		for a in allocs:
			summary["scanned_allocations"] += 1
			asset = (a.allocated_asset or "").strip()
			if asset in eligible:
				summary["kept"].append({"allocation": a.name, "asset": asset, "reason": "handover_ok"})
				continue
			summary["demoted"].append(
				{
					"allocation": a.name,
					"asset_request": a.parent,
					"asset": asset,
					"movement": am_name,
					"reason": eligibility.get("reason") or "transfer_without_employee_handover",
				}
			)
			if dry_run:
				continue
			frappe.db.set_value(
				"Asset Request Allocation",
				a.name,
				"fulfillment_status",
				ALLOC_CANCELLED,
				update_modified=False,
			)
			parents_to_refresh.add(a.parent)

		if dry_run:
			continue
		for parent in parents_to_refresh:
			frappe.flags.asset_request_fulfillment_in_progress = True
			try:
				ar = frappe.get_doc(ASSET_REQUEST_DOCTYPE, parent)
				refresh_header_fulfillment(ar)
				ar.flags.ignore_validate_update_after_submit = True
				ar.save(ignore_permissions=True)
			finally:
				frappe.flags.asset_request_fulfillment_in_progress = False

	return summary


def reconcile_asset_request_fulfillment(
	*,
	asset_request: str | None = None,
	dry_run: bool = False,
) -> dict:
	"""Idempotent historical reconciliation using the same eligibility contract."""
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
		"demote": None,
	}

	# First demote any previously over-eager Transfer allocations.
	summary["demote"] = demote_ineligible_transfer_allocations(
		asset_request=asset_request, dry_run=dry_run
	)

	for row in movements:
		summary["scanned"] += 1
		am = frappe.get_doc("Asset Movement", row.name)
		ar_name = am.reference_name
		if not ar_name or not frappe.db.exists(ASSET_REQUEST_DOCTYPE, ar_name):
			summary["skipped"].append({"movement": am.name, "reason": "missing_request"})
			continue
		ar_employee = frappe.db.get_value(ASSET_REQUEST_DOCTYPE, ar_name, "employee")
		eligibility = evaluate_delivery_eligibility(am, ar_employee=ar_employee)
		if not eligibility.get("eligible"):
			summary["skipped"].append(
				{"movement": am.name, "reason": eligibility.get("reason") or "not_eligible"}
			)
			continue
		if dry_run:
			ar = frappe.get_doc(ASSET_REQUEST_DOCTYPE, ar_name)
			_promotable = sum(
				1
				for a in (ar.get("allocations") or [])
				if a.asset_movement == am.name and a.fulfillment_status == ALLOC_MOVEMENT_DRAFT
			)
			_created, _skipped, amb = _backfill_allocations_from_movement(
				ar, am, eligible_assets=set(eligibility.get("eligible_assets") or [])
			)
			entry = {
				"movement": am.name,
				"asset_request": ar_name,
				"would_promote": _promotable,
				"would_create": _created,
				"ambiguous": amb,
				"eligibility": eligibility,
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
			before = (
				ar.fulfillment_status,
				cint(getattr(ar, "requested_qty", 0)),
				cint(ar.delivered_qty),
				cint(getattr(ar, "remaining_qty", 0)),
			)
			refresh_header_fulfillment(ar)
			after = (
				ar.fulfillment_status,
				cint(getattr(ar, "requested_qty", 0)),
				cint(ar.delivered_qty),
				cint(getattr(ar, "remaining_qty", 0)),
			)
			if before != after:
				ar.flags.ignore_validate_update_after_submit = True
				ar.save(ignore_permissions=True)
				summary["updated"].append(
					{
						"asset_request": name,
						"asset_movement": None,
						"refreshed": True,
						"before": before,
						"after": after,
					}
				)
		finally:
			frappe.flags.asset_request_fulfillment_in_progress = False

	return summary
