# Copyright (c) 2026, ERPNext Extensions contributors
"""Fail-closed guards for Job Card Stock Rebuild (reuse Iran / stage helpers)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary import (
	CLASS_BY_PRODUCT,
	CLASS_CO_PRODUCT,
	CLASS_UNKNOWN,
	CLASS_ADDITIONAL_FINISHED_GOOD,
)


def simulate_stage_output_guard_on_secondary(secondary_rows: list[dict]) -> dict:
	"""Simulate the v5.4.0 common-UOM / equivalent-factor gate on JC secondary rows.

	Does not invent factors. Reuses resolve_common_uom / _explicit_factor semantics
	via a thin adapter of row dicts.
	"""
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
		EQUIV_FACTOR_FIELD,
		COMMON_UOM_FIELD,
		PHYSICAL_CONV_FIELD,
		_explicit_factor,
		_has_complete_equivalence_snapshot,
		resolve_common_uom,
	)

	stage = []
	for row in secondary_rows:
		classification = row.get("classification")
		if classification not in (
			CLASS_CO_PRODUCT,
			CLASS_BY_PRODUCT,
			CLASS_ADDITIONAL_FINISHED_GOOD,
			"MAIN_PRODUCT_REJECT",
			"MAIN_FG",
		) and (row.get("secondary_item_type") or "") not in (
			"Co-Product",
			"By-Product",
			"Additional Finished Good",
		):
			# Ordinary scrap / component scrap are not stage-equivalent pool members
			continue
		# Skip evidence-only Manufacture rows that are not on JC
		if row.get("source") == "manufacture_evidence" and not row.get("already_on_job_card"):
			continue
		adapter = frappe._dict(
			{
				"item_code": row.get("item_code"),
				"idx": row.get("idx") or 0,
				"qty": flt(row.get("qty")),
				"uom": row.get("uom"),
				"stock_uom": row.get("stock_uom"),
				EQUIV_FACTOR_FIELD: row.get("custom_output_equivalent_factor"),
				COMMON_UOM_FIELD: row.get("custom_common_uom"),
				PHYSICAL_CONV_FIELD: row.get("custom_physical_conversion"),
			}
		)
		stage.append(adapter)

	if len(stage) < 2:
		return {"ok": True, "offending": [], "message": None}

	try:
		if _has_complete_equivalence_snapshot(stage):
			return {"ok": True, "offending": [], "message": None}
		common_uom = resolve_common_uom(stage)
		if common_uom:
			return {"ok": True, "offending": [], "message": None, "common_uom": common_uom}
		missing_factor = [row for row in stage if _explicit_factor(row) is None]
		if missing_factor:
			labels = [
				f"Row #{getattr(r, 'idx', '?')} {r.get('item_code')}" for r in missing_factor
			]
			return {
				"ok": False,
				"status": S.DOWNSTREAM_MANUFACTURE_BLOCKED,
				"offending": labels,
				"message": (
					"Stage outputs {0} do not share a common UOM conversion and have no "
					"output equivalent factor. 1:1 is not assumed."
				).format(", ".join(labels)),
				"manufacture_blocked_by_stage_output_configuration": True,
			}
		return {"ok": True, "offending": [], "message": None}
	except Exception as exc:
		return {
			"ok": False,
			"status": S.DOWNSTREAM_MANUFACTURE_BLOCKED,
			"offending": [],
			"message": str(exc),
			"manufacture_blocked_by_stage_output_configuration": True,
		}


def classify_secondary_unknown_block(secondary_rows: list[dict]) -> list[str]:
	statuses = []
	for row in secondary_rows:
		if row.get("source") != "job_card_secondary":
			continue
		if row.get("classification") == CLASS_UNKNOWN and row.get("secondary_item_type"):
			statuses.append(S.MANUAL_REVIEW)
	return list(dict.fromkeys(statuses))


def simulate_manufacture_candidates(job_card: str, for_quantity: float | None = None) -> dict:
	"""Run Core ManufactureEntry.add_raw_materials without persisting a Stock Entry.

	Uses in-memory JC Item values (already written in the dry-run txn, or current DB).
	"""
	from erpnext.stock.doctype.stock_entry_type.stock_entry_type import ManufactureEntry

	jc_cols = set(frappe.db.get_table_columns("Job Card") or [])
	wanted = [
		"name",
		"company",
		"work_order",
		"bom_no",
		"wip_warehouse",
		"production_item",
		"for_quantity",
		"process_loss_qty",
		"project",
		"skip_material_transfer",
		"backflush_from_wip_warehouse",
		"target_warehouse",
	]
	fields = [c for c in wanted if c in jc_cols]
	jc = frappe.db.get_value("Job Card", job_card, fields, as_dict=1)
	if not jc:
		return {"ok": False, "error": "Job Card not found", "items": []}

	wo = {}
	if jc.work_order and frappe.db.exists("Work Order", jc.work_order):
		wo = frappe.db.get_value(
			"Work Order",
			jc.work_order,
			["production_item", "fg_warehouse", "wip_warehouse", "bom_no", "company"],
			as_dict=1,
		) or {}

	production_item = jc.get("production_item") or wo.get("production_item")
	fg_warehouse = (
		jc.get("target_warehouse")
		or wo.get("fg_warehouse")
		or jc.get("wip_warehouse")
		or wo.get("wip_warehouse")
	)
	wip_warehouse = jc.get("wip_warehouse") or wo.get("wip_warehouse")
	bom_no = jc.get("bom_no") or wo.get("bom_no")
	company = jc.get("company") or wo.get("company")

	qty = flt(for_quantity if for_quantity is not None else jc.for_quantity) or 1.0
	kwargs = frappe._dict(
		{
			"purpose": "Manufacture",
			"company": company,
			"bom_no": bom_no,
			"for_quantity": qty,
			"process_loss_qty": flt(jc.get("process_loss_qty")),
			"project": jc.get("project"),
			"job_card": jc.name,
			"work_order": jc.work_order,
			"wip_warehouse": wip_warehouse,
			"fg_warehouse": fg_warehouse,
			"production_item": production_item,
			"skip_material_transfer": flt(jc.get("skip_material_transfer")),
			"backflush_from_wip_warehouse": flt(jc.get("backflush_from_wip_warehouse")),
		}
	)
	try:
		entry = ManufactureEntry(kwargs)
		entry.stock_entry = frappe.new_doc("Stock Entry")
		entry.stock_entry.purpose = "Manufacture"
		entry.stock_entry.company = company
		entry.stock_entry.job_card = jc.name
		entry.stock_entry.work_order = jc.work_order
		entry.stock_entry.bom_no = bom_no
		entry.stock_entry.fg_completed_qty = qty
		entry.prepare_source_warehouse()
		entry.add_raw_materials()
		# Do not call add_finished_good / save — inspection only
		rm = []
		for row in entry.stock_entry.items or []:
			rm.append(
				{
					"item_code": row.item_code,
					"qty": flt(row.qty),
					"s_warehouse": row.s_warehouse,
					"t_warehouse": row.t_warehouse,
					"job_card_item": row.get("job_card_item"),
					"is_finished_item": row.get("is_finished_item"),
					"is_scrap_item": row.get("is_scrap_item"),
					"secondary_item_type": row.get("secondary_item_type"),
				}
			)
		return {
			"ok": True,
			"items": rm,
			"persisted": False,
			"stock_entry_name": getattr(entry.stock_entry, "name", None),
		}
	except Exception as exc:
		return {"ok": False, "error": str(exc), "items": [], "persisted": False}


def expect_rm_candidate(simulation: dict, item_code: str, qty: float, precision: int = 6) -> dict:
	"""Assert a raw-material candidate exists with expected qty."""
	matches = [r for r in simulation.get("items") or [] if r["item_code"] == item_code]
	if not matches:
		return {
			"ok": False,
			"status": S.DOWNSTREAM_MANUFACTURE_BLOCKED,
			"detail": f"Simulated Manufacture missing raw material {item_code}",
		}
	found = matches[0]
	if abs(flt(found["qty"]) - flt(qty)) > (0.5 * 10 ** (-precision)):
		return {
			"ok": False,
			"status": S.DOWNSTREAM_MANUFACTURE_BLOCKED,
			"detail": f"Expected {item_code}×{qty}, got {found['qty']}",
			"found": found,
		}
	return {"ok": True, "found": found}


def inventory_custom14_server_script() -> dict:
	"""Detect Custom 14 - Restore Missing Job Card Scrap without executing it."""
	name = frappe.db.get_value(
		"Server Script",
		{"name": ["like", "%Custom 14%Restore Missing Job Card Scrap%"]},
		"name",
	)
	if not name:
		name = frappe.db.get_value(
			"Server Script",
			{"script_type": "DocType Event", "reference_doctype": "Stock Entry"},
			"name",
			order_by="modified desc",
		)
	row = None
	if name and frappe.db.exists("Server Script", name):
		row = frappe.db.get_value(
			"Server Script",
			name,
			["name", "disabled", "script_type", "reference_doctype", "doctype_event"],
			as_dict=1,
		)
	return {
		"name": row.name if row else None,
		"disabled": int(row.disabled) if row else None,
		"note": (
			"Custom 14 appends missing JC scrap on Stock Entry events. "
			"Phase 1 must not duplicate scrap rows that Custom 14 will create."
		),
	}


def apply_gate_statuses(statuses: list[str], stage_guard: dict, secondary_statuses: list[str]) -> dict:
	"""Compute whether APPLY is allowed and overall status list."""
	all_statuses = list(dict.fromkeys(list(statuses) + list(secondary_statuses)))
	warnings = [s for s in all_statuses if s in S.WARNINGS_ALLOW_REBUILD]
	blockers = [s for s in all_statuses if s in S.APPLY_BLOCKERS]
	manufacture_blocked = False
	job_card_rebuild_valid = True

	if stage_guard and not stage_guard.get("ok", True):
		manufacture_blocked = True
		all_statuses.append(S.DOWNSTREAM_MANUFACTURE_BLOCKED)
		# Spec §27: JC rebuild may still be valid; Manufacture blocked separately
		if stage_guard.get("manufacture_blocked_by_stage_output_configuration"):
			# Do not put stage-output into APPLY_BLOCKERS for tracking-only if tracking is deterministic
			# But still surface DOWNSTREAM_MANUFACTURE_BLOCKED — Apply for tracking remains allowed
			# unless other hard blockers exist.
			pass

	hard = [s for s in all_statuses if s in S.APPLY_BLOCKERS and s != S.DOWNSTREAM_MANUFACTURE_BLOCKED]
	# UNKNOWN secondary / MANUAL_REVIEW etc. already in hard via APPLY_BLOCKERS

	apply_allowed = not hard
	if apply_allowed and S.JOB_CARD_TRACKING_INCOMPLETE in all_statuses:
		overall = S.REBUILD_READY
	elif apply_allowed and not any(
		s
		for s in all_statuses
		if s
		not in (
			S.BALANCED,
			S.POSTING_ORDER_WARNING,
			S.MISSING_MANUFACTURE_CONSUMPTION,
			S.PARTIAL_MANUFACTURE_CONSUMPTION,
			S.DOWNSTREAM_MANUFACTURE_BLOCKED,
			S.REBUILD_READY,
		)
	):
		if S.JOB_CARD_TRACKING_INCOMPLETE not in all_statuses and not any(
			s == S.MISSING_MANUFACTURE_CONSUMPTION for s in all_statuses
		):
			overall = S.BALANCED
			apply_allowed = False
		else:
			overall = S.REBUILD_READY if S.JOB_CARD_TRACKING_INCOMPLETE in all_statuses else S.BALANCED
			if overall == S.BALANCED:
				apply_allowed = False
	else:
		overall = hard[0] if hard else (S.REBUILD_READY if apply_allowed else S.MANUAL_REVIEW)

	result: dict[str, Any] = {
		"statuses": list(dict.fromkeys(all_statuses)),
		"warnings": warnings,
		"blockers": hard,
		"apply_allowed": apply_allowed,
		"overall_status": overall,
		"manufacture_repair_required": S.MISSING_MANUFACTURE_CONSUMPTION in all_statuses
		or S.PARTIAL_MANUFACTURE_CONSUMPTION in all_statuses,
		"manufacture_blocked_by_stage_output_configuration": bool(
			stage_guard and stage_guard.get("manufacture_blocked_by_stage_output_configuration")
		),
		"job_card_rebuild_valid": job_card_rebuild_valid and not hard,
	}
	return result


__all__ = [
	"simulate_stage_output_guard_on_secondary",
	"classify_secondary_unknown_block",
	"simulate_manufacture_candidates",
	"expect_rm_candidate",
	"inventory_custom14_server_script",
	"apply_gate_statuses",
]
