# Copyright (c) 2026, ERPNext Extensions contributors
"""Secondary Item reconstruction / validation for Job Card Stock Rebuild.

Phase 1 does not invent secondary rows from stock balances alone.
It inventories existing JC secondary rows + Manufacture evidence and
preserves classification / stage metadata. Conflicts → MANUAL_REVIEW.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S

CLASS_COMPONENT_SCRAP = "COMPONENT_SCRAP"
CLASS_MAIN_PRODUCT_REJECT = "MAIN_PRODUCT_REJECT"
CLASS_CO_PRODUCT = "CO_PRODUCT"
CLASS_BY_PRODUCT = "BY_PRODUCT"
CLASS_ADDITIONAL_FINISHED_GOOD = "ADDITIONAL_FINISHED_GOOD"
CLASS_ORDINARY_SCRAP = "ORDINARY_SCRAP"
CLASS_UNKNOWN = "UNKNOWN"


def secondary_identity(row: dict) -> tuple:
	return (
		(row.get("classification") or "").strip(),
		(row.get("item_code") or "").strip(),
		(row.get("batch_no") or "").strip(),
		(row.get("bom_secondary_item") or "").strip(),
		(row.get("secondary_item_type") or "").strip(),
		(row.get("uom") or "").strip(),
		(row.get("custom_parent_co_product") or "").strip(),
	)


def classify_secondary_row(row: dict) -> str:
	"""Map JC secondary / SE evidence to rebuild classification."""
	sec = (row.get("secondary_item_type") or "").strip()
	out_class = (row.get("custom_output_class") or "").strip()
	if out_class == CLASS_MAIN_PRODUCT_REJECT or out_class == "MAIN_PRODUCT_REJECT":
		return CLASS_MAIN_PRODUCT_REJECT
	if out_class == CLASS_COMPONENT_SCRAP or out_class == "COMPONENT_SCRAP":
		return CLASS_COMPONENT_SCRAP
	if out_class in ("MAIN_FG", "MAIN_PRODUCT"):
		return CLASS_ADDITIONAL_FINISHED_GOOD
	if sec == "Co-Product" or out_class == "CO_PRODUCT":
		return CLASS_CO_PRODUCT
	if sec == "By-Product" or out_class == "BY_PRODUCT":
		return CLASS_BY_PRODUCT
	if sec == "Additional Finished Good":
		return CLASS_ADDITIONAL_FINISHED_GOOD
	if sec == "Scrap":
		# Without output_class, ordinary scrap — do not invent Product Reject
		return CLASS_ORDINARY_SCRAP
	if sec:
		return CLASS_UNKNOWN
	return CLASS_UNKNOWN


def load_jc_secondary_items(job_card: str) -> list[dict]:
	meta = frappe.get_meta("Job Card")
	if not meta.has_field("secondary_items"):
		return []
	# Discover columns that exist
	cols = frappe.db.get_table_columns("Job Card Secondary Item")
	wanted = [
		"name",
		"idx",
		"item_code",
		"secondary_item_type",
		"bom_secondary_item",
		"uom",
		"stock_uom",
		"qty",
		"stock_qty",
		"warehouse",
		"custom_output_class",
		"custom_output_equivalent_factor",
		"custom_physical_conversion",
		"custom_equivalent_qty",
		"custom_common_uom",
		"custom_parent_co_product",
	]
	fields = [c for c in wanted if c in cols]
	if "item_code" not in fields:
		return []
	return frappe.db.sql(
		f"select {', '.join(fields)} from `tabJob Card Secondary Item` where parent=%s order by idx",
		job_card,
		as_dict=1,
	)


def inventory_secondary(job_card: str, movements: list[dict]) -> dict:
	"""Build secondary preview: preserve JC rows; flag conflicts; no invention."""
	jc_rows = load_jc_secondary_items(job_card)
	preview = []
	statuses = []
	seen_ids: set[tuple] = set()

	for row in jc_rows:
		classification = classify_secondary_row(row)
		payload = {
			"source": "job_card_secondary",
			"name": row.get("name"),
			"idx": row.get("idx"),
			"item_code": row.get("item_code"),
			"batch_no": "",
			"qty": flt(row.get("stock_qty") or row.get("qty")),
			"uom": row.get("uom"),
			"stock_uom": row.get("stock_uom"),
			"secondary_item_type": row.get("secondary_item_type"),
			"bom_secondary_item": row.get("bom_secondary_item"),
			"classification": classification,
			"custom_output_class": row.get("custom_output_class"),
			"custom_output_equivalent_factor": row.get("custom_output_equivalent_factor"),
			"custom_physical_conversion": row.get("custom_physical_conversion"),
			"custom_equivalent_qty": row.get("custom_equivalent_qty"),
			"custom_common_uom": row.get("custom_common_uom"),
			"custom_parent_co_product": row.get("custom_parent_co_product"),
			"action": "NO CHANGE",
			"guard_status": "OK",
		}
		ident = secondary_identity(payload)
		if ident in seen_ids:
			payload["action"] = "BLOCKED"
			payload["guard_status"] = S.SECONDARY_ITEM_MISMATCH
			statuses.append(S.SECONDARY_ITEM_MISMATCH)
		seen_ids.add(ident)
		if classification == CLASS_UNKNOWN and payload["secondary_item_type"]:
			payload["guard_status"] = S.MANUAL_REVIEW
			statuses.append(S.MANUAL_REVIEW)
		preview.append(payload)

	# Manufacture secondary evidence — informational only (no auto-add in Phase 1)
	mfg_secondary = [
		m
		for m in movements
		if m.get("bucket")
		in ("COMPONENT_SCRAP", "PRODUCT_REJECT", "ORDINARY_SCRAP", "SECONDARY_OUTPUT", "FINISHED")
	]
	for m in mfg_secondary:
		classification = {
			"COMPONENT_SCRAP": CLASS_COMPONENT_SCRAP,
			"PRODUCT_REJECT": CLASS_MAIN_PRODUCT_REJECT,
			"ORDINARY_SCRAP": CLASS_ORDINARY_SCRAP,
			"SECONDARY_OUTPUT": classify_secondary_row(m),
			"FINISHED": CLASS_ADDITIONAL_FINISHED_GOOD,
		}.get(m["bucket"], CLASS_UNKNOWN)
		ident = secondary_identity(
			{
				"classification": classification,
				"item_code": m["item_code"],
				"batch_no": m.get("batch_no") or "",
				"bom_secondary_item": "",
				"secondary_item_type": m.get("secondary_item_type") or "",
				"uom": m.get("uom") or "",
				"custom_parent_co_product": m.get("custom_parent_co_product") or "",
			}
		)
		preview.append(
			{
				"source": "manufacture_evidence",
				"voucher": m["voucher"],
				"item_code": m["item_code"],
				"batch_no": m.get("batch_no") or "",
				"qty": m["qty"],
				"uom": m.get("uom"),
				"stock_uom": m.get("stock_uom"),
				"secondary_item_type": m.get("secondary_item_type"),
				"classification": classification,
				"custom_output_class": m.get("custom_output_class"),
				"custom_output_equivalent_factor": m.get("custom_output_equivalent_factor"),
				"custom_physical_conversion": m.get("custom_physical_conversion"),
				"custom_equivalent_qty": m.get("custom_equivalent_qty"),
				"custom_common_uom": m.get("custom_common_uom"),
				"custom_parent_co_product": m.get("custom_parent_co_product"),
				"already_on_job_card": ident in seen_ids,
				"action": "NO CHANGE",
				"guard_status": "EVIDENCE_ONLY",
				"note": "Phase 1 does not invent JC secondary rows from Manufacture alone.",
			}
		)

	return {
		"rows": preview,
		"statuses": list(dict.fromkeys(statuses)),
		"jc_secondary_count": len(jc_rows),
	}


__all__ = [
	"CLASS_COMPONENT_SCRAP",
	"CLASS_MAIN_PRODUCT_REJECT",
	"CLASS_CO_PRODUCT",
	"CLASS_BY_PRODUCT",
	"CLASS_ADDITIONAL_FINISHED_GOOD",
	"CLASS_ORDINARY_SCRAP",
	"CLASS_UNKNOWN",
	"secondary_identity",
	"classify_secondary_row",
	"load_jc_secondary_items",
	"inventory_secondary",
]
