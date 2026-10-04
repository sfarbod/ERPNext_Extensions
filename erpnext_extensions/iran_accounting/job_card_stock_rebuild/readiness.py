# Copyright (c) 2026, ERPNext Extensions contributors
"""Full selected-Job-Card Manufacture readiness via normal Make Stock Entry path.

Does not persist Stock Entries. Uses savepoint + patched save when needed.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S


def simulate_normal_make_stock_entry(job_card: str) -> dict:
	"""Mirror JobCard.make_stock_entry_for_semi_fg_item without persisting.

	Returns generated rows, Iran classification, stage set membership, and
	whether allocate_stage_output_cost would pass.
	"""
	from erpnext.manufacturing.doctype.bom.bom import add_additional_cost
	from erpnext.stock.doctype.stock_entry_type.stock_entry_type import ManufactureEntry
	from erpnext_extensions.iran_accounting.scrap_costing import (
		classify_manufacture_outputs,
		secondary_item_type_of,
		_is_incoming,
	)
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
		CLASS_CO_PRODUCT,
		CLASS_CO_PRODUCT_REJECT,
		CLASS_MAIN_FG,
		CLASS_MAIN_PRODUCT_REJECT,
		_explicit_factor,
		_is_finance_excluded,
		allocate_stage_output_cost,
		snapshot_output_classification,
	)

	if not frappe.db.exists("Job Card", job_card):
		return {"ok": False, "error": "Job Card not found", "persisted": False}

	jc = frappe.get_doc("Job Card", job_card)
	sp = f"jc_ready_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	out: dict[str, Any] = {
		"ok": False,
		"persisted": False,
		"path": "make_stock_entry_for_semi_fg_item",
		"rows": [],
		"classified": {},
		"stage": [],
		"error": None,
		"custom14_duplicate_scrap": False,
	}
	try:
		if not jc.track_semi_finished_goods:
			out["path"] = "make_stock_entry_mapped"
			from erpnext.manufacturing.doctype.job_card.job_card import make_stock_entry

			doc = make_stock_entry(job_card)
			# mapped MTfM — not Manufacture readiness
			out["ok"] = True
			out["note"] = "Job Card is not track_semi_finished_goods; mapped path is Material Transfer."
			out["rows"] = [
				{
					"idx": r.idx,
					"item_code": r.item_code,
					"qty": flt(r.qty),
					"secondary_item_type": r.secondary_item_type,
					"is_finished_item": r.is_finished_item,
				}
				for r in (doc.get("items") or [])
			]
			return out

		consumed_process_loss = jc.get_consumed_process_loss()
		for_qty = flt(jc.get_qty_to_produce()) - flt(jc.manufactured_qty) - flt(consumed_process_loss)
		if for_qty <= 0:
			# Still simulate using for_quantity so readiness can inspect secondary structure
			for_qty = flt(jc.for_quantity) or 1.0
			out["note"] = "manufactured_qty already covers for_quantity; simulating with for_quantity"

		ste = ManufactureEntry(
			{
				"for_quantity": for_qty,
				"process_loss_qty": max(flt(jc.process_loss_qty) - flt(consumed_process_loss), 0),
				"job_card": jc.name,
				"skip_material_transfer": jc.skip_material_transfer,
				"backflush_from_wip_warehouse": jc.backflush_from_wip_warehouse,
				"work_order": jc.work_order,
				"purpose": "Manufacture",
				"production_item": jc.finished_good,
				"company": jc.company,
				"wip_warehouse": jc.wip_warehouse,
				"fg_warehouse": jc.target_warehouse,
				"bom_no": jc.semi_fg_bom,
				"project": frappe.db.get_value("Work Order", jc.work_order, "project"),
			}
		)
		ste.make_stock_entry()
		ste.stock_entry.flags.ignore_mandatory = True
		wo_doc = frappe.get_doc("Work Order", jc.work_order)
		add_additional_cost(ste.stock_entry, wo_doc, jc)
		ste.stock_entry.pro_doc = wo_doc
		ste.stock_entry.set_secondary_items_from_job_card()
		for row in ste.stock_entry.items:
			if (row.secondary_item_type or row.valuation_type) and not row.t_warehouse:
				row.t_warehouse = jc.target_warehouse

		doc = ste.stock_entry

		# Duplicate scrap identity check (Custom 14 risk) — selected JC only
		scrap_keys = []
		for r in doc.get("items") or []:
			if (r.secondary_item_type or "") == "Scrap":
				key = (r.item_code, flt(r.qty), r.stock_uom or r.uom)
				if key in scrap_keys:
					out["custom14_duplicate_scrap"] = True
				scrap_keys.append(key)

		rows = []
		for i, r in enumerate(doc.get("items") or [], 1):
			rows.append(
				{
					"idx": r.idx or i,
					"item_code": r.item_code,
					"qty": flt(r.qty),
					"uom": r.uom,
					"stock_uom": r.stock_uom,
					"s_warehouse": r.s_warehouse,
					"t_warehouse": r.t_warehouse,
					"is_finished_item": r.is_finished_item,
					"secondary_item_type": r.secondary_item_type,
					"bom_secondary_item": r.get("bom_secondary_item"),
					"custom_output_class": r.get("custom_output_class"),
					"incoming": _is_incoming(r),
				}
			)
		out["rows"] = rows

		classified = classify_manufacture_outputs(doc)
		out["classified"] = {
			k: [
				{
					"idx": r.idx,
					"item_code": r.item_code,
					"qty": flt(r.qty),
					"sec": secondary_item_type_of(r),
					"cls": r.get("custom_output_class"),
				}
				for r in v
			]
			for k, v in classified.items()
			if v
		}

		snap = snapshot_output_classification(doc)
		finance_excluded = [
			row
			for row in snap[CLASS_CO_PRODUCT] + snap[CLASS_CO_PRODUCT_REJECT]
			if _is_finance_excluded(row)
		]
		participating_co = [row for row in snap[CLASS_CO_PRODUCT] if row not in finance_excluded]
		participating_reject = [
			row for row in snap[CLASS_CO_PRODUCT_REJECT] if row not in finance_excluded
		]
		stage = (
			list(snap[CLASS_MAIN_FG])
			+ list(snap[CLASS_MAIN_PRODUCT_REJECT])
			+ participating_co
			+ participating_reject
		)
		out["stage"] = [
			{
				"idx": r.idx,
				"item_code": r.item_code,
				"qty": flt(r.qty),
				"uom": r.uom,
				"sec": secondary_item_type_of(r),
				"factor": _explicit_factor(r),
				"bucket": (
					"MAIN_FG"
					if r in snap[CLASS_MAIN_FG]
					else "MAIN_PRODUCT_REJECT"
					if r in snap[CLASS_MAIN_PRODUCT_REJECT]
					else "CO_PRODUCT"
					if r in participating_co
					else "CO_PRODUCT_REJECT"
					if r in participating_reject
					else "?"
				),
			}
			for r in stage
		]

		try:
			allocate_stage_output_cost(doc)
			out["allocate_ok"] = True
			out["ok"] = not out["custom14_duplicate_scrap"]
			if out["custom14_duplicate_scrap"]:
				out["error"] = "Duplicate Scrap identity on selected Job Card Manufacture simulation"
				out["status"] = S.DOWNSTREAM_MANUFACTURE_BLOCKED
			else:
				out["status"] = "MANUFACTURE_READINESS_PASS"
		except Exception as exc:
			out["allocate_ok"] = False
			out["ok"] = False
			out["error"] = str(exc)
			out["status"] = S.DOWNSTREAM_MANUFACTURE_BLOCKED
		return out
	except Exception as exc:
		out["error"] = str(exc)
		out["status"] = S.DOWNSTREAM_MANUFACTURE_BLOCKED
		return out
	finally:
		frappe.db.rollback(save_point=sp)


def stage_contains_item(readiness: dict, item_code: str) -> bool:
	return any(r.get("item_code") == item_code for r in (readiness.get("stage") or []))


__all__ = [
	"simulate_normal_make_stock_entry",
	"stage_contains_item",
]
