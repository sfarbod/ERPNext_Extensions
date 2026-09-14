# Copyright (c) 2026, ERPNext Extensions contributors
"""Preview and execute Job Card Work Order reassignment."""

from __future__ import annotations

import hashlib
import json
import secrets

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.stock_extensions.job_card_reassign.audit import add_comments, write_audit
from erpnext_extensions.stock_extensions.job_card_reassign.classify import (
	CLASS_MOVE,
	classify_scope,
)
from erpnext_extensions.stock_extensions.job_card_reassign.mapping import (
	ACTION_CREATE,
	DEFINITION_FIELDS,
	group_create_definitions,
	map_job_card_operation,
)
from erpnext_extensions.stock_extensions.job_card_reassign.qty import analyze_historical_qty
from erpnext_extensions.stock_extensions.job_card_reassign.recalc import recalculate_work_order, refresh_job_card
from erpnext_extensions.stock_extensions.job_card_reassign.validate import evaluate_compatibility

CACHE_PREFIX = "jc_wo_reassign:"
CACHE_TTL = 30 * 60
PREVIEW_VERSION = 3

WO_COUNTER_FIELDS = (
	"name",
	"status",
	"company",
	"production_item",
	"bom_no",
	"qty",
	"produced_qty",
	"process_loss_qty",
	"material_transferred_for_manufacturing",
	"additional_transferred_qty",
	"track_semi_finished_goods",
	"transfer_material_against",
	"skip_transfer",
	"project",
	"docstatus",
	"modified",
	"planned_operating_cost",
	"actual_operating_cost",
	"total_operating_cost",
	"corrective_operation_cost",
)

WO_OPERATION_LOAD_FIELDS = (
	"name",
	"idx",
	"operation",
	"finished_good",
	"sequence_id",
	"completed_qty",
	"pending_qty",
	"process_loss_qty",
	"workstation",
	"workstation_type",
	"bom_no",
	"bom",
	"status",
	"actual_operation_time",
	"actual_operating_cost",
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
	"actual_start_time",
	"actual_end_time",
	"planned_start_time",
	"planned_end_time",
)

JC_FIELDS = (
	"name",
	"work_order",
	"operation",
	"operation_id",
	"operation_row_id",
	"operation_row_number",
	"sequence_id",
	"status",
	"docstatus",
	"for_quantity",
	"total_completed_qty",
	"manufactured_qty",
	"transferred_qty",
	"pending_qty",
	"process_loss_qty",
	"finished_good",
	"semi_fg_bom",
	"bom_no",
	"workstation",
	"workstation_type",
	"track_semi_finished_goods",
	"is_paused",
	"is_corrective_job_card",
	"is_subcontracted",
	"modified",
)


def _require_system_manager() -> None:
	frappe.only_for("System Manager")


def _cache_key(token: str) -> str:
	return f"{CACHE_PREFIX}{token}"


def load_work_order(name: str) -> dict:
	if not name or not frappe.db.exists("Work Order", name):
		frappe.throw(frappe._("Work Order {0} does not exist").format(name))
	row = frappe.db.get_value("Work Order", name, list(WO_COUNTER_FIELDS), as_dict=True)
	fields = list(WO_OPERATION_LOAD_FIELDS)
	meta = frappe.get_meta("Work Order Operation")
	for df in meta.fields:
		if df.fieldname and df.fieldname.startswith("custom_") and df.fieldname not in fields:
			fields.append(df.fieldname)
	ops = frappe.get_all(
		"Work Order Operation",
		filters={"parent": name},
		fields=fields,
		order_by="idx",
	)
	items = frappe.get_all(
		"Work Order Item",
		filters={"parent": name},
		fields=["item_code", "required_qty", "transferred_qty", "consumed_qty", "returned_qty"],
		order_by="idx",
	)
	row["operations"] = ops
	row["required_items"] = items
	return row


def source_operations_index(source: dict) -> dict[str, dict]:
	return {op["name"]: op for op in source.get("operations") or [] if op.get("name")}


def load_job_cards(names: list[str]) -> list[dict]:
	if not names:
		return []
	rows = frappe.get_all(
		"Job Card",
		filters={"name": ["in", names]},
		fields=list(JC_FIELDS),
	)
	by_name = {r.name: r for r in rows}
	missing = [n for n in names if n not in by_name]
	if missing:
		frappe.throw(frappe._("Job Card(s) not found: {0}").format(", ".join(missing)))
	return [by_name[n] for n in names]


def list_source_job_cards(source_work_order: str) -> list[dict]:
	return frappe.get_all(
		"Job Card",
		filters={"work_order": source_work_order, "docstatus": ["!=", 2]},
		fields=list(JC_FIELDS),
		order_by="sequence_id, creation",
	)


def snapshot_counters(wo: dict) -> dict:
	return {
		"name": wo.get("name"),
		"status": wo.get("status"),
		"qty": flt(wo.get("qty")),
		"produced_qty": flt(wo.get("produced_qty")),
		"process_loss_qty": flt(wo.get("process_loss_qty")),
		"material_transferred_for_manufacturing": flt(wo.get("material_transferred_for_manufacturing")),
		"actual_operating_cost": flt(wo.get("actual_operating_cost")),
		"total_operating_cost": flt(wo.get("total_operating_cost")),
		"operations": [
			{
				"name": op.get("name"),
				"idx": op.get("idx"),
				"operation": op.get("operation"),
				"finished_good": op.get("finished_good"),
				"completed_qty": flt(op.get("completed_qty")),
				"pending_qty": flt(op.get("pending_qty")),
				"process_loss_qty": flt(op.get("process_loss_qty")),
				"status": op.get("status"),
			}
			for op in wo.get("operations") or []
		],
		"required_items": [
			{
				"item_code": it.get("item_code"),
				"required_qty": flt(it.get("required_qty")),
				"transferred_qty": flt(it.get("transferred_qty")),
				"consumed_qty": flt(it.get("consumed_qty")),
				"returned_qty": flt(it.get("returned_qty")),
			}
			for it in wo.get("required_items") or []
		],
	}


def estimate_after(source: dict, target: dict, job_cards: list[dict], mappings: dict, classified: dict) -> dict:
	"""Estimate source/target counters after the move. Recalc on execute is authoritative."""
	source_after = snapshot_counters(source)
	target_after = snapshot_counters(target)

	def _op_map(snap):
		return {row["name"]: row for row in snap["operations"]}

	src_ops = _op_map(source_after)
	tgt_ops = _op_map(target_after)

	# Provisional CREATE ops appear in the estimate under their create_key.
	for mapped in mappings.values():
		if mapped.get("action") != ACTION_CREATE or not mapped.get("ok"):
			continue
		key = mapped.get("target_operation_id")
		if key and key not in tgt_ops:
			row = {
				"name": key,
				"idx": None,
				"operation": mapped.get("target_operation"),
				"finished_good": mapped.get("target_finished_good"),
				"completed_qty": 0,
				"pending_qty": 0,
				"process_loss_qty": 0,
				"status": "Pending",
				"action": ACTION_CREATE,
			}
			target_after["operations"].append(row)
			tgt_ops[key] = row

	for jc in job_cards:
		mapped = mappings.get(jc["name"]) or {}
		if not mapped.get("ok") or cint(jc.get("docstatus")) != 1:
			continue
		src_id = mapped.get("source_operation_id")
		tgt_id = mapped.get("target_operation_id")
		completed = flt(jc.get("total_completed_qty"))
		loss = flt(jc.get("process_loss_qty"))
		if src_id in src_ops:
			src_ops[src_id]["completed_qty"] = max(0, flt(src_ops[src_id]["completed_qty"]) - completed)
			src_ops[src_id]["process_loss_qty"] = max(0, flt(src_ops[src_id]["process_loss_qty"]) - loss)
		if tgt_id in tgt_ops:
			tgt_ops[tgt_id]["completed_qty"] = flt(tgt_ops[tgt_id]["completed_qty"]) + completed
			tgt_ops[tgt_id]["process_loss_qty"] = flt(tgt_ops[tgt_id]["process_loss_qty"]) + loss

	delta_fg = sum(
		flt(j.get("manufactured_qty"))
		for j in job_cards
		if mappings.get(j["name"], {}).get("ok")
		and cint(j.get("docstatus")) == 1
		and j.get("finished_good") == source.get("production_item")
	)
	if cint(source.get("track_semi_finished_goods")):
		source_after["produced_qty"] = max(0, flt(source_after["produced_qty"]) - delta_fg)
		if source.get("production_item") == target.get("production_item"):
			target_after["produced_qty"] = flt(target_after["produced_qty"]) + delta_fg

	moved_ses = [r for r in classified.get("rows") or [] if r.get("classification") == CLASS_MOVE]
	src_loss = sum(flt(r.get("process_loss_qty")) for r in moved_ses if cint(r.get("docstatus")) == 1)
	source_after["process_loss_qty"] = max(0, flt(source_after["process_loss_qty"]) - src_loss)
	target_after["process_loss_qty"] = flt(target_after["process_loss_qty"]) + src_loss

	return {"source": source_after, "target": target_after}


def fingerprint(payload: dict) -> str:
	material = {
		"version": PREVIEW_VERSION,
		"source": payload["source_work_order"],
		"target": payload["target_work_order"],
		"job_cards": [
			{
				"name": j["name"],
				"modified": str(j.get("modified")),
				"work_order": j.get("work_order"),
				"operation_id": j.get("operation_id"),
				"docstatus": j.get("docstatus"),
				"status": j.get("status"),
			}
			for j in payload["job_cards"]
		],
		"stock_entries": [
			{
				"name": r["name"],
				"modified": str(r.get("modified")),
				"work_order": r.get("work_order"),
				"job_card": r.get("job_card"),
				"docstatus": r.get("docstatus"),
				"classification": r.get("classification"),
			}
			for r in payload["stock_entries"]
		],
		"operation_actions": [
			{
				"job_card": jc,
				"action": (payload.get("operation_mapping") or {}).get(jc, {}).get("action"),
				"target_operation_id": (payload.get("operation_mapping") or {}).get(jc, {}).get(
					"target_operation_id"
				),
				"create_key": (payload.get("operation_mapping") or {}).get(jc, {}).get("create_key"),
			}
			for jc in sorted((payload.get("operation_mapping") or {}).keys())
		],
		"planned_target_qty": (payload.get("historical_qty") or {}).get("planned_qty"),
		"source_modified": str(payload["source"].get("modified")),
		"target_modified": str(payload["target"].get("modified")),
		"source_status": payload["source"].get("status"),
		"target_status": payload["target"].get("status"),
	}
	blob = json.dumps(material, sort_keys=True, default=str)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def remaining_job_card_count(work_order: str, excluding: list[str] | None = None) -> int:
	filters: dict = {"work_order": work_order, "docstatus": ["!=", 2]}
	if excluding:
		filters["name"] = ["not in", excluding]
	return frappe.db.count("Job Card", filters)


def build_preview(source_work_order: str, target_work_order: str, job_cards: list[str], reason: str = "") -> dict:
	selected = list(dict.fromkeys([n for n in job_cards if n]))
	source = load_work_order(source_work_order)
	target = load_work_order(target_work_order)
	jc_docs = load_job_cards(selected)
	classified = classify_scope(source_work_order, selected)
	source_ops = source_operations_index(source)
	mappings = {
		jc["name"]: map_job_card_operation(jc, target["operations"], source_ops) for jc in jc_docs
	}
	historical_qty = analyze_historical_qty(target=target, moved_job_cards=jc_docs, mappings=mappings)
	blockers, warnings = evaluate_compatibility(
		source=source,
		target=target,
		job_cards=jc_docs,
		selected=selected,
		classified=classified,
		mappings=mappings,
		reason=reason,
	)
	if historical_qty.get("historical_exceeds_planned_qty"):
		warnings.append(
			{
				"code": "historical_exceeds_planned_qty",
				"message": historical_qty.get("message")
				or (
					f"Historical execution exceeds current planned Work Order quantity. "
					f"Planned Qty remains unchanged: {historical_qty.get('planned_qty')}."
				),
			}
		)

	creates = group_create_definitions(mappings)
	preview = {
		"source_work_order": source_work_order,
		"target_work_order": target_work_order,
		"reason": reason,
		"source": source,
		"target": target,
		"job_cards": jc_docs,
		"operation_mapping": mappings,
		"operations_to_create": creates,
		"historical_qty": historical_qty,
		"stock_entries": classified["rows"],
		"move_stock_entries": classified["move"],
		"keep_stock_entries": classified["keep"],
		"blockers": blockers,
		"warnings": warnings,
		"counters_before": {"source": snapshot_counters(source), "target": snapshot_counters(target)},
		"counters_after_estimate": estimate_after(source, target, jc_docs, mappings, classified),
		"source_remaining_job_cards": remaining_job_card_count(source_work_order, selected),
		"can_execute": not blockers,
		"created_on": str(now_datetime()),
	}
	# Planned qty never changes in estimates.
	preview["counters_after_estimate"]["source"]["qty"] = flt(source.get("qty"))
	preview["counters_after_estimate"]["target"]["qty"] = flt(target.get("qty"))
	preview["fingerprint"] = fingerprint(preview)
	return preview


def store_preview(preview: dict) -> dict:
	token = secrets.token_hex(16)
	preview["preview_token"] = token
	frappe.cache().set_value(_cache_key(token), preview, expires_in_sec=CACHE_TTL)
	return preview


def load_preview(token: str) -> dict | None:
	if not token:
		return None
	return frappe.cache().get_value(_cache_key(token))


def lock_scope(source: str, target: str, job_cards: list[str], stock_entries: list[str]) -> None:
	for name in (source, target):
		frappe.db.sql("select name from `tabWork Order` where name=%s for update", name)
	for name in job_cards:
		frappe.db.sql("select name from `tabJob Card` where name=%s for update", name)
	for name in stock_entries:
		frappe.db.sql("select name from `tabStock Entry` where name=%s for update", name)


def _copy_custom_definition_fields(defn: dict) -> dict:
	"""Keep only creatable fields, including custom_* manufacturing fields."""
	out = {}
	meta = frappe.get_meta("Work Order Operation")
	allowed = set(DEFINITION_FIELDS) | {
		df.fieldname for df in meta.fields if df.fieldname and df.fieldname.startswith("custom_")
	}
	for key, value in (defn or {}).items():
		if key in allowed:
			out[key] = value
	out.setdefault("status", "Pending")
	out["completed_qty"] = 0
	out["pending_qty"] = 0
	out["process_loss_qty"] = 0
	out["actual_operation_time"] = 0
	out["actual_operating_cost"] = 0
	out["actual_start_time"] = None
	out["actual_end_time"] = None
	out["planned_start_time"] = None
	out["planned_end_time"] = None
	return out


def create_target_operations(target_name: str, mappings: dict[str, dict]) -> dict[str, dict]:
	"""Create missing Target Work Order Operations. Returns create_key → resolved mapping fields."""
	creates = group_create_definitions(mappings)
	if not creates:
		return {}

	planned_qty = flt(frappe.db.get_value("Work Order", target_name, "qty"))
	wo = frappe.get_doc("Work Order", target_name)
	wo.flags.ignore_validate_update_after_submit = True
	before = {d.name for d in wo.operations}
	next_idx = max((cint(d.idx) for d in wo.operations), default=0) + 1
	ordered_keys = list(creates.keys())
	for key in ordered_keys:
		row = _copy_custom_definition_fields(creates[key])
		row["idx"] = next_idx
		next_idx += 1
		wo.append("operations", row)
	wo.qty = planned_qty
	wo.save()
	frappe.db.set_value("Work Order", target_name, "qty", planned_qty, update_modified=False)
	wo.reload()

	new_rows = [d for d in wo.operations if d.name not in before]
	if len(new_rows) != len(ordered_keys):
		frappe.throw(
			frappe._(
				"Failed to create Target Work Order Operations ({0} expected, {1} created)."
			).format(len(ordered_keys), len(new_rows))
		)

	resolved: dict[str, dict] = {}
	for key, row in zip(ordered_keys, new_rows, strict=True):
		resolved[key] = {
			"target_operation_id": row.name,
			"target_operation": row.operation,
			"target_finished_good": row.finished_good,
			"target_sequence_id": row.sequence_id,
			"target_operation_row_id": row.idx,
			"target_workstation": row.workstation,
			"target_workstation_type": row.workstation_type,
			"target_bom_no": row.bom_no or row.bom,
			"target_semi_fg_bom": row.bom_no or row.bom,
		}
	return resolved


def resolve_create_mappings(mappings: dict[str, dict], created: dict[str, dict]) -> dict[str, dict]:
	"""Replace provisional CREATE:* ids with real Work Order Operation names."""
	out = {}
	for jc_name, mapped in mappings.items():
		row = dict(mapped)
		if row.get("action") == ACTION_CREATE and row.get("create_key") in created:
			row.update(created[row["create_key"]])
		out[jc_name] = row
	return out


def _apply_job_card(jc: dict, mapping: dict, target: dict) -> None:
	values = {
		"work_order": target["name"],
		"operation_id": mapping["target_operation_id"],
		"operation_row_id": mapping["target_operation_row_id"],
		"operation_row_number": mapping["target_operation_id"],
		"sequence_id": (
			mapping.get("target_sequence_id")
			if mapping.get("target_sequence_id") not in (None, "")
			else mapping.get("target_operation_row_id")
		),
		"bom_no": target.get("bom_no"),
	}
	if cint(target.get("track_semi_finished_goods")):
		values["semi_fg_bom"] = mapping.get("target_semi_fg_bom") or target.get("bom_no")
		values["track_semi_finished_goods"] = 1
	if target.get("project") is not None:
		values["project"] = target.get("project")
	frappe.db.set_value("Job Card", jc["name"], values, update_modified=True)


def _apply_stock_entry(se_name: str, mapping: dict, target: dict, source_bom: str | None) -> None:
	se = frappe.db.get_value(
		"Stock Entry",
		se_name,
		["bom_no", "custom_operation", "custom_operation_row_id"],
		as_dict=True,
	)
	values = {"work_order": target["name"]}
	meta = frappe.get_meta("Stock Entry")
	if meta.has_field("custom_operation"):
		values["custom_operation"] = mapping.get("target_operation")
	if meta.has_field("custom_operation_row_id"):
		values["custom_operation_row_id"] = mapping.get("target_operation_row_id")
	new_bom = mapping.get("target_semi_fg_bom") or target.get("bom_no")
	if se and se.bom_no and source_bom and se.bom_no == source_bom and new_bom:
		values["bom_no"] = new_bom
	elif se and not se.bom_no and new_bom:
		values["bom_no"] = new_bom
	frappe.db.set_value("Stock Entry", se_name, values, update_modified=True)

	old_op = mapping.get("source_operation_id")
	new_op = mapping.get("target_operation_id")
	if old_op and new_op and old_op != new_op:
		frappe.db.sql(
			"""
			UPDATE `tabLanded Cost Taxes and Charges`
			SET operation_id = %s
			WHERE parent = %s AND parenttype = 'Stock Entry' AND operation_id = %s
			""",
			(new_op, se_name, old_op),
		)


def _transfer_additional_items(source_name: str, target_name: str, se_names: list[str]) -> None:
	source = frappe.get_doc("Work Order", source_name)
	target = frappe.get_doc("Work Order", target_name)
	source.flags.ignore_validate_update_after_submit = True
	target.flags.ignore_validate_update_after_submit = True
	for name in se_names:
		se = frappe.get_doc("Stock Entry", name)
		if se.purpose != "Material Transfer for Manufacture" or se.docstatus != 1:
			continue
		source.remove_additional_items(se)
		target.add_additional_items(se)


def _move_material_requests(job_cards: list[str], target_work_order: str) -> list[str]:
	rows = frappe.get_all(
		"Material Request",
		filters={"job_card": ["in", job_cards], "docstatus": ["!=", 2]},
		pluck="name",
	)
	for name in rows:
		frappe.db.set_value("Material Request", name, "work_order", target_work_order)
	return rows


def apply_reassignment(preview: dict) -> dict:
	source_name = preview["source_work_order"]
	target_name = preview["target_work_order"]
	target = dict(preview["target"])
	source = preview["source"]
	moved_ses = list(preview["move_stock_entries"])
	jc_by_name = {j["name"]: j for j in preview["job_cards"]}
	se_by_name = {r["name"]: r for r in preview["stock_entries"]}
	mappings = dict(preview["operation_mapping"])

	created = create_target_operations(target_name, mappings)
	mappings = resolve_create_mappings(mappings, created)
	preview["operation_mapping"] = mappings

	# Planned Work Order.qty is never modified by this historical repair.
	source_planned_qty = flt(source.get("qty"))
	target_planned_qty = flt(target.get("qty"))

	_transfer_additional_items(source_name, target_name, moved_ses)

	for jc in preview["job_cards"]:
		mapping = mappings[jc["name"]]
		if not mapping.get("ok") or not mapping.get("target_operation_id"):
			frappe.throw(frappe._("No resolved operation mapping for Job Card {0}").format(jc["name"]))
		if str(mapping["target_operation_id"]).startswith("CREATE:"):
			frappe.throw(
				frappe._("Target operation was not created for Job Card {0}").format(jc["name"])
			)
		_apply_job_card(jc, mapping, target)

	for se_name in moved_ses:
		se = se_by_name[se_name]
		mapping = mappings.get(se.get("job_card"))
		if not mapping:
			frappe.throw(frappe._("No operation mapping for Stock Entry {0}").format(se_name))
		_apply_stock_entry(se_name, mapping, target, source.get("bom_no"))

	moved_mrs = _move_material_requests(list(jc_by_name), target_name)

	for jc in preview["job_cards"]:
		refresh_job_card(jc["name"])

	recalculate_work_order(source_name)
	recalculate_work_order(target_name)

	# Belt-and-suspenders: restore planned qty if any native path touched it.
	if flt(frappe.db.get_value("Work Order", source_name, "qty")) != source_planned_qty:
		frappe.db.set_value("Work Order", source_name, "qty", source_planned_qty, update_modified=False)
	if flt(frappe.db.get_value("Work Order", target_name, "qty")) != target_planned_qty:
		frappe.db.set_value("Work Order", target_name, "qty", target_planned_qty, update_modified=False)

	source_after = load_work_order(source_name)
	target_after = load_work_order(target_name)
	remaining = remaining_job_card_count(source_name)
	log_name = write_audit(
		source_work_order=source_name,
		target_work_order=target_name,
		reason=preview.get("reason") or "",
		job_cards=preview["job_cards"],
		stock_entries=[r for r in preview["stock_entries"] if r["name"] in moved_ses],
		operation_mapping=mappings,
		counters_before=preview["counters_before"],
		counters_after={"source": snapshot_counters(source_after), "target": snapshot_counters(target_after)},
		preview_token=preview.get("preview_token") or "",
		source_empty_after=1 if remaining == 0 else 0,
	)
	add_comments(
		source_work_order=source_name,
		target_work_order=target_name,
		job_cards=list(jc_by_name),
		stock_entries=moved_ses,
		reason=preview.get("reason") or "",
		log_name=log_name,
	)
	return {
		"log": log_name,
		"source_remaining_job_cards": remaining,
		"source_empty": remaining == 0,
		"moved_job_cards": list(jc_by_name),
		"moved_stock_entries": moved_ses,
		"moved_material_requests": moved_mrs,
		"created_operations": created,
		"source_qty": source_planned_qty,
		"target_qty": target_planned_qty,
		"historical_qty": preview.get("historical_qty"),
		"counters_after": {"source": snapshot_counters(source_after), "target": snapshot_counters(target_after)},
	}


def preview_reassignment(source_work_order: str, target_work_order: str, job_cards: list[str], reason: str = "") -> dict:
	_require_system_manager()
	preview = build_preview(source_work_order, target_work_order, job_cards, reason)
	return store_preview(preview)


def execute_reassignment(preview_token: str, reason: str) -> dict:
	_require_system_manager()
	if not reason or not str(reason).strip():
		frappe.throw(frappe._("A reason is required."))
	cached = load_preview(preview_token)
	if not cached:
		frappe.throw(frappe._("Preview expired or missing. Run Preview again."))
	if cached.get("blockers"):
		frappe.throw(frappe._("Cannot execute while blockers exist. Run Preview again."))

	source = cached["source_work_order"]
	target = cached["target_work_order"]
	job_cards = [j["name"] for j in cached["job_cards"]]
	lock_names = list({r["name"] for r in cached["stock_entries"]})
	lock_scope(source, target, job_cards, lock_names)

	fresh = build_preview(source, target, job_cards, reason)
	if fresh["fingerprint"] != cached["fingerprint"]:
		frappe.throw(
			frappe._("Documents changed since preview. Run Preview again before executing.")
		)
	if fresh["blockers"]:
		messages = "; ".join(b.get("message") or b.get("code") for b in fresh["blockers"])
		frappe.throw(frappe._("Reassignment is blocked: {0}").format(messages))

	fresh["preview_token"] = preview_token
	fresh["reason"] = reason
	return apply_reassignment(fresh)
