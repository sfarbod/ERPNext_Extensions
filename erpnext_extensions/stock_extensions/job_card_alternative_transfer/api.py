# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Whitelist API for Job Card alternative-transfer fulfillment status.

All endpoints are read-only. They never create or submit Stock Entries.
"""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.stock_extensions.job_card_alternative_transfer.attribution import (
	get_material_transfer_status,
)


def _load_job_card_for_read(job_card: str):
	"""Validate identifier and enforce Job Card read permission."""
	if not job_card or not str(job_card).strip():
		frappe.throw(_("Job Card is required"), frappe.ValidationError)

	job_card = str(job_card).strip()
	if not frappe.db.exists("Job Card", job_card):
		frappe.throw(_("Job Card {0} not found").format(frappe.bold(job_card)), frappe.DoesNotExistError)

	doc = frappe.get_doc("Job Card", job_card)
	doc.check_permission("read")
	return doc


@frappe.whitelist()
def get_material_transfer_status_for_job_card(job_card: str) -> dict:
	"""Return authoritative material fulfillment for Desk Move Materials / Submit."""
	doc = _load_job_card_for_read(job_card)
	return get_material_transfer_status(doc)


@frappe.whitelist()
def get_material_shortages_for_job_card(job_card: str) -> list:
	"""Read-only shortage payload for one Job Card (original + substitute aware).

	Uses the patched ``JobCard.get_material_shortages`` so aggregation matches
	Move Materials. Does not create or submit Stock Entries.
	"""
	doc = _load_job_card_for_read(job_card)
	return doc.get_material_shortages()


def onload(doc, method=None):
	"""Expose fulfillment on ``doc.__onload`` so Desk JS can stay synchronous."""
	if doc.is_new() or not doc.name:
		return
	try:
		# onload runs after the document was loaded under the current user;
		# skip extra permission noise but still fail closed on calculation errors.
		if hasattr(doc, "check_permission"):
			doc.check_permission("read")
		status = get_material_transfer_status(doc)
	except frappe.PermissionError:
		return
	except Exception:
		frappe.log_error(title="Job Card alternative transfer onload failed")
		return

	doc.set_onload("ee_materials_fully_transferred", bool(status.get("fully_transferred")))
	doc.set_onload(
		"ee_material_shortfalls",
		[
			{
				"name": row.name,
				"item_code": row.item_code,
				"required_qty": row.required_qty,
				"effective_transferred_qty": row.effective_transferred_qty,
				"shortfall_qty": row.shortfall_qty,
			}
			for row in status.get("shortfalls") or []
		],
	)
	doc.set_onload("ee_attributed_by_item", status.get("attributed_by_item") or {})
