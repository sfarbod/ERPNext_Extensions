# Copyright (c) 2026, ERPNext Extensions contributors
"""Map a Job Card onto a target Work Order Operation (REUSE or CREATE)."""

from __future__ import annotations

from frappe.utils import cint, cstr, flt

ACTION_REUSE = "REUSE"
ACTION_CREATE = "CREATE"

# Definition fields copied when creating a Target Work Order Operation.
# Derived execution counters are intentionally excluded.
DEFINITION_FIELDS = (
	"operation",
	"workstation",
	"workstation_type",
	"sequence_id",
	"finished_good",
	"bom_no",
	"bom",
	"time_in_mins",
	"batch_size",
	"hour_rate",
	"planned_operating_cost",
	"description",
	"source_warehouse",
	"wip_warehouse",
	"fg_warehouse",
	"is_subcontracted",
	"skip_material_transfer",
	"backflush_from_wip_warehouse",
	"quality_inspection_required",
)

ZERO_ON_CREATE = (
	"completed_qty",
	"pending_qty",
	"process_loss_qty",
	"actual_operation_time",
	"actual_operating_cost",
	"actual_start_time",
	"actual_end_time",
	"planned_start_time",
	"planned_end_time",
)


def provisional_create_id(source_operation_id: str | None, job_card_name: str) -> str:
	base = source_operation_id or f"jc:{job_card_name}"
	return f"CREATE:{base}"


def build_operation_definition(source_op: dict | None, job_card: dict) -> dict:
	"""Build a creatable Work Order Operation definition from the source row (preferred) or JC."""
	defn: dict = {}
	source_op = source_op or {}
	for field in DEFINITION_FIELDS:
		if field in source_op and source_op.get(field) not in (None, ""):
			defn[field] = source_op.get(field)
	# Job Card fills gaps; source operation remains authoritative when present.
	if not defn.get("operation"):
		defn["operation"] = job_card.get("operation")
	if not defn.get("finished_good") and job_card.get("finished_good"):
		defn["finished_good"] = job_card.get("finished_good")
	if defn.get("sequence_id") in (None, ""):
		defn["sequence_id"] = job_card.get("sequence_id") or 0
	if not defn.get("workstation") and job_card.get("workstation"):
		defn["workstation"] = job_card.get("workstation")
	if not defn.get("workstation_type") and job_card.get("workstation_type"):
		defn["workstation_type"] = job_card.get("workstation_type")
	if not defn.get("bom_no") and job_card.get("semi_fg_bom"):
		defn["bom_no"] = job_card.get("semi_fg_bom")
	elif not defn.get("bom_no") and job_card.get("bom_no"):
		defn["bom_no"] = job_card.get("bom_no")
	for field in ZERO_ON_CREATE:
		defn[field] = None if "time" in field else 0
	defn["status"] = "Pending"
	return defn


def _exact_reuse_candidates(job_card: dict, target_operations: list[dict]) -> list[dict]:
	"""Exact compatible reuse: unique (operation + finished_good) match.

	Same Operation master name alone is not enough. Prefer CREATE when ambiguous.
	"""
	operation = cstr(job_card.get("operation") or "")
	finished_good = cstr(job_card.get("finished_good") or "")
	if not operation:
		return []
	matches = [
		op
		for op in target_operations
		if cstr(op.get("operation") or "") == operation
		and cstr(op.get("finished_good") or "") == finished_good
	]
	return matches


def map_job_card_operation(
	job_card: dict,
	target_operations: list[dict],
	source_operations_by_id: dict[str, dict] | None = None,
) -> dict:
	"""Return REUSE or CREATE mapping. Missing target ops are CREATE, not blockers."""
	jc_name = job_card.get("name")
	operation = cstr(job_card.get("operation") or "")
	finished_good = cstr(job_card.get("finished_good") or "")
	source_operation_id = job_card.get("operation_id")
	source_ops = source_operations_by_id or {}
	source_op = source_ops.get(source_operation_id) if source_operation_id else None

	if not operation:
		return {
			"ok": False,
			"action": None,
			"error": f"Job Card {jc_name} has no operation.",
			"source_operation_id": source_operation_id,
		}

	candidates = _exact_reuse_candidates(job_card, target_operations)
	if len(candidates) == 1:
		op = candidates[0]
		return {
			"ok": True,
			"action": ACTION_REUSE,
			"source_operation_id": source_operation_id,
			"source_operation": operation,
			"source_finished_good": finished_good,
			"source_sequence_id": job_card.get("sequence_id"),
			"source_operation_row_id": job_card.get("operation_row_id"),
			"target_operation_id": op.get("name"),
			"target_operation": op.get("operation"),
			"target_finished_good": op.get("finished_good"),
			"target_sequence_id": op.get("sequence_id"),
			"target_operation_row_id": op.get("idx"),
			"target_workstation": op.get("workstation"),
			"target_workstation_type": op.get("workstation_type"),
			"target_bom_no": op.get("bom_no") or op.get("bom"),
			"target_semi_fg_bom": op.get("bom_no") or op.get("bom"),
			"create_definition": None,
			"create_key": None,
		}

	# No unique exact match → CREATE from source operation definition.
	defn = build_operation_definition(source_op, job_card)
	create_key = provisional_create_id(source_operation_id, jc_name)
	return {
		"ok": True,
		"action": ACTION_CREATE,
		"source_operation_id": source_operation_id,
		"source_operation": operation,
		"source_finished_good": finished_good,
		"source_sequence_id": job_card.get("sequence_id"),
		"source_operation_row_id": job_card.get("operation_row_id"),
		"target_operation_id": create_key,
		"target_operation": defn.get("operation"),
		"target_finished_good": defn.get("finished_good"),
		"target_sequence_id": defn.get("sequence_id"),
		"target_operation_row_id": None,
		"target_workstation": defn.get("workstation"),
		"target_workstation_type": defn.get("workstation_type"),
		"target_bom_no": defn.get("bom_no") or defn.get("bom"),
		"target_semi_fg_bom": defn.get("bom_no") or defn.get("bom"),
		"create_definition": defn,
		"create_key": create_key,
		"note": (
			"CREATE TARGET OPERATION"
			if not candidates
			else f"CREATE TARGET OPERATION (ambiguous: {len(candidates)} exact matches)"
		),
	}


def group_create_definitions(mappings: dict[str, dict]) -> dict[str, dict]:
	"""Deduplicate CREATE rows by create_key (usually source Work Order Operation name)."""
	out: dict[str, dict] = {}
	for mapped in mappings.values():
		if mapped.get("action") != ACTION_CREATE or not mapped.get("ok"):
			continue
		key = mapped.get("create_key")
		if not key:
			continue
		if key not in out:
			out[key] = dict(mapped.get("create_definition") or {})
	return out


def completed_qty_estimate(job_card: dict) -> float:
	return max(flt(job_card.get("manufactured_qty")), flt(job_card.get("total_completed_qty")))
