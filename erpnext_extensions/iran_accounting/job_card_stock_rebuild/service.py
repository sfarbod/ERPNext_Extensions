# Copyright (c) 2026, ERPNext Extensions contributors
"""Job Card Stock Rebuild service — SCAN / PREVIEW / DRY RUN / APPLY / VERIFY.

v5.4.1: tracking rebuild + optional per-row Secondary type reclassification
with explicit user approval. Runtime scope = selected Job Card dependency closure.
Never mutates Manufacture / SLE / GL / valuation.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import build_evidence
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.guards import (
	expect_rm_candidate,
	inventory_custom14_server_script,
	simulate_manufacture_candidates,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.readiness import (
	simulate_normal_make_stock_entry,
	stage_contains_item,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary import inventory_secondary
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary_type import (
	apply_type_approvals,
	suggest_secondary_type_changes,
	validate_type_approvals,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics import (
	TRACKING_FIELDS,
	derive_tracking_for_item,
	load_job_card_items,
	propose_link_backfills,
	qty_equal,
	reconcile_batches,
)


def _parse_approvals(raw) -> list[dict]:
	if not raw:
		return []
	if isinstance(raw, str):
		raw = json.loads(raw) if raw.strip().startswith("[") else []
	return list(raw or [])


def _snapshot_jc(job_card: str) -> dict:
	items = load_job_card_items(job_card)
	meta = frappe.db.get_value(
		"Job Card",
		job_card,
		["name", "docstatus", "status", "work_order", "modified", "for_quantity", "production_item", "finished_good"],
		as_dict=1,
	)
	type_sug = suggest_secondary_type_changes(job_card) if meta else {"rows": []}
	return {
		"job_card": meta,
		"items": items,
		"secondary_types": type_sug.get("rows") or [],
	}


SCOPE_SECONDARY_TYPE_ONLY = "SECONDARY_TYPE_ONLY"
_AUDIT_KEYS = {"modified", "modified_by", "creation", "owner"}


def _normalize_apply_scope(apply_scope) -> str | None:
	"""Opt-in scope. Omitted or blank keeps the historical full Apply."""
	if apply_scope is None:
		return None
	scope = str(apply_scope).strip()
	if not scope or scope.lower() in {"none", "null"}:
		return None
	if scope != SCOPE_SECONDARY_TYPE_ONLY:
		frappe.throw(frappe._("Unknown apply_scope {0}.").format(scope))
	return scope


def _scrub_audit(value):
	if isinstance(value, dict):
		return {
			key: _scrub_audit(item)
			for key, item in value.items()
			if key not in _AUDIT_KEYS and not str(key).startswith("_")
		}
	if isinstance(value, list):
		return [_scrub_audit(item) for item in value]
	return value


def _job_card_business_snapshot(job_card: str) -> dict:
	frappe.clear_document_cache("Job Card", job_card)
	return _scrub_audit(frappe.get_doc("Job Card", job_card).as_dict())


def _ledger_snapshot(voucher_nos: list[str]) -> dict:
	if not voucher_nos:
		return {"sle": [], "gl": []}
	sle = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": "Stock Entry", "voucher_no": ["in", voucher_nos]},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"actual_qty",
			"qty_after_transaction",
			"incoming_rate",
			"valuation_rate",
			"stock_value",
			"stock_value_difference",
			"batch_no",
			"is_cancelled",
		],
		order_by="name",
		limit_page_length=0,
	)
	gl = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Stock Entry", "voucher_no": ["in", voucher_nos]},
		fields=["name", "account", "debit", "credit", "against", "is_cancelled"],
		order_by="name",
		limit_page_length=0,
	)
	return {
		"sle": [_scrub_audit(dict(row)) for row in sle],
		"gl": [_scrub_audit(dict(row)) for row in gl],
	}


def _stock_business_snapshot(job_card: str) -> dict:
	names = frappe.get_all(
		"Stock Entry", filters={"job_card": job_card}, pluck="name", order_by="name"
	)
	documents = []
	for name in names:
		frappe.clear_document_cache("Stock Entry", name)
		documents.append(_scrub_audit(frappe.get_doc("Stock Entry", name).as_dict()))
	return {
		"stock_entries": documents,
		"riv_count": frappe.db.count("Repost Item Valuation"),
		**_ledger_snapshot(names),
	}


def _is_number(value) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool)


def _values_equal(left, right) -> bool:
	if _is_number(left) and _is_number(right):
		return abs(float(left) - float(right)) <= 1e-6
	return left == right


def _flatten_business(value, prefix=""):
	if isinstance(value, dict):
		rows = []
		for key in sorted(value):
			path = f"{prefix}.{key}" if prefix else str(key)
			rows.extend(_flatten_business(value[key], path))
		return rows
	if isinstance(value, list):
		if value and all(isinstance(item, dict) and item.get("name") for item in value):
			rows = []
			for item in sorted(value, key=lambda row: str(row.get("name"))):
				rows.extend(_flatten_business(item, f"{prefix}.{item['name']}"))
			return rows
		rows = []
		for index, item in enumerate(value):
			rows.extend(_flatten_business(item, f"{prefix}[{index}]"))
		return rows
	return [(prefix, value)]


def _business_diffs(before, after) -> list[dict]:
	before_map = dict(_flatten_business(before))
	after_map = dict(_flatten_business(after))
	diffs = []
	for path in sorted(set(before_map) | set(after_map)):
		old = before_map.get(path)
		new = after_map.get(path)
		if path not in before_map or path not in after_map or not _values_equal(old, new):
			diffs.append({"path": path, "before": old, "after": new})
	return diffs


def _unexpected_job_card_diffs(diffs: list[dict], approvals: list[dict]) -> list[dict]:
	allowed = {
		f"secondary_items.{row['secondary_row']}.secondary_item_type": row["approved_type"]
		for row in approvals
	}
	unexpected = []
	for diff in diffs:
		approved = allowed.get(diff["path"])
		if approved is not None and diff["after"] == approved and diff["before"] != diff["after"]:
			continue
		unexpected.append(diff)
	return unexpected


def _format_diffs(diffs: list[dict], limit: int = 12) -> str:
	parts = []
	for diff in diffs[:limit]:
		parts.append(f"{diff['path']}: {diff['before']} -> {diff['after']}")
	if len(diffs) > limit:
		parts.append(f"... {len(diffs) - limit} more")
	return "; ".join(parts)


def _apply_secondary_types_via_document(job_card: str, approvals: list[dict]) -> list[dict]:
	"""Persist approved secondary types through Job Card.save().

	Tracking writes and Stock Entry sync are not part of this path. Any business
	field other than the approved secondary_item_type fails the transaction.
	"""
	before_job_card = _job_card_business_snapshot(job_card)
	before_stock = _stock_business_snapshot(job_card)
	doc = frappe.get_doc("Job Card", job_card)
	rows = {row.name: row for row in doc.secondary_items}
	for approval in approvals:
		row = rows.get(approval["secondary_row"])
		if row is None:
			raise RuntimeError(f"Secondary row {approval['secondary_row']} is not on {job_card}")
		current = (row.secondary_item_type or "").strip()
		if current != approval["current_type"]:
			raise RuntimeError(
				f"STALE_PREVIEW — {approval['secondary_row']} is {current}, "
				f"approval expected {approval['current_type']}"
			)
		row.secondary_item_type = approval["approved_type"]
	doc.save()
	job_card_diffs = _unexpected_job_card_diffs(
		_business_diffs(before_job_card, _job_card_business_snapshot(job_card)),
		approvals,
	)
	stock_diffs = _business_diffs(before_stock, _stock_business_snapshot(job_card))
	if job_card_diffs or stock_diffs:
		raise RuntimeError(
			"Unexpected mutation blocked: "
			+ _format_diffs(job_card_diffs + stock_diffs)
		)
	return [
		{**approval, "type": "secondary_item_type", "persisted_via": "document_save"}
		for approval in approvals
	]


def _jc_hard_blockers(statuses: list[str], material_rows: list[dict], type_suggestions: dict) -> list[str]:
	"""Escalate only JC-unsafe conditions (dependency-aware)."""
	hard = []
	for s in statuses:
		if s in (
			S.AMBIGUOUS_OWNERSHIP,
			S.BATCH_MISMATCH,
			S.UOM_MAPPING_REQUIRED,
			S.SECONDARY_ITEM_MISMATCH,
			S.COMPONENT_SCRAP_MISMATCH,
			S.RETURN_MISMATCH,
		):
			hard.append(s)
	# OVER_CONSUMED only blocks if any material row with writes/actions needs rebuild
	if S.OVER_CONSUMED in statuses:
		if any(
			r.get("action") not in ("NO CHANGE", None)
			and S.OVER_CONSUMED in (r.get("statuses") or [])
			for r in material_rows
		):
			hard.append(S.OVER_CONSUMED)
	# Duplicate secondary / MANUAL REVIEW that disables approval and is unresolved
	for row in type_suggestions.get("rows") or []:
		if row.get("action") == "BLOCKED" or (
			row.get("suggested_type") == "MANUAL REVIEW"
			and row.get("block_reason")
			and "duplicate" in (row.get("block_reason") or "").lower()
		):
			hard.append(S.SECONDARY_ITEM_MISMATCH)
	return list(dict.fromkeys(hard))


def _build_plan(job_card: str, item_filter: str | None = None, batch_filter: str | None = None) -> dict:
	if not job_card:
		frappe.throw(frappe._("Job Card is required."))
	if not frappe.db.exists("Job Card", job_card):
		frappe.throw(frappe._("Job Card {0} not found.").format(job_card))

	evidence = build_evidence(job_card, item_filter=item_filter, batch_filter=batch_filter)
	collapsed, batch_statuses = reconcile_batches(evidence["items"])
	jc_item_rows = load_job_card_items(job_card)
	jc_items = {r.item_code: r for r in jc_item_rows}

	material_rows = []
	link_proposals = []
	statuses: list[str] = list(batch_statuses)
	writes = []

	if evidence.get("posting_order_warning"):
		statuses.append(S.POSTING_ORDER_WARNING)

	for item in collapsed:
		jc_item = jc_items.get(item["item_code"])
		has_rm_activity = any(
			flt(item.get(k)) > 1e-9 for k in ("issued", "returned", "consumed", "component_scrap")
		)
		if not jc_item and not has_rm_activity:
			continue
		if not jc_item and has_rm_activity:
			# Informational orphan — does not auto-block whole JC unless ownership ambiguous on writes
			statuses.append(S.ORPHAN_WIP)
			material_rows.append(
				{
					"item_code": item["item_code"],
					"jc_item": None,
					"required_qty": None,
					"issued": item["issued"],
					"returned": item["returned"],
					"consumed": item["consumed"],
					"component_scrap": item["component_scrap"],
					"wip_remainder": item["wip_remainder"],
					"net_available_for_manufacture": item["net_available_for_manufacture"],
					"field_plan": [],
					"statuses": [S.ORPHAN_WIP],
					"action": "MANUAL REVIEW",
					"batch_no": item.get("batch_no"),
					"batches": item.get("batches"),
					"evidence": item.get("evidence"),
					"link_proposals": [],
					"current_transferred_qty": 0,
					"derived_transferred_qty": 0,
					"difference": item["wip_remainder"],
				}
			)
			continue
		derived = derive_tracking_for_item(jc_item, item)
		links = propose_link_backfills(job_card, item)
		for lp in links:
			if lp.get("status") == S.AMBIGUOUS_OWNERSHIP:
				statuses.append(S.AMBIGUOUS_OWNERSHIP)
			if lp.get("action") == "BACKFILL LINK":
				writes.append({"type": "sed_job_card_item", **lp})
		link_proposals.extend(links)
		statuses.extend(derived["statuses"])

		action = derived["action"]
		if any(lp.get("action") == "BACKFILL LINK" for lp in links):
			action = (
				"UPDATE TRACKING + BACKFILL LINK"
				if "UPDATE" in action
				else "BACKFILL LINK"
				if action == "NO CHANGE"
				else action
			)
		if any(lp.get("action") == "BLOCKED" for lp in links):
			action = "BLOCKED"

		for fp in derived["field_plan"]:
			if fp["write_required"]:
				writes.append(
					{
						"type": "jc_item_field",
						"jc_item": derived["jc_item"],
						"item_code": derived["item_code"],
						**fp,
					}
				)

		material_rows.append(
			{
				**derived,
				"batch_no": item.get("batch_no"),
				"batches": item.get("batches"),
				"evidence": item.get("evidence"),
				"link_proposals": links,
				"action": action,
				"current_transferred_qty": next(
					(fp["current"] for fp in derived["field_plan"] if fp["fieldname"] == "transferred_qty"),
					0,
				),
				"derived_transferred_qty": next(
					(fp["derived"] for fp in derived["field_plan"] if fp["fieldname"] == "transferred_qty"),
					0,
				),
				"difference": flt(derived["wip_remainder"])
				- (
					flt(jc_item.custom_still_in_wip)
					if jc_item and jc_item.get("custom_still_in_wip") is not None
					else flt(jc_item.transferred_qty if jc_item else 0)
					- flt(jc_item.consumed_qty if jc_item else 0)
				),
			}
		)

	secondary = inventory_secondary(job_card, evidence["movements"])
	type_suggestions = suggest_secondary_type_changes(job_card)
	statuses.extend(secondary.get("statuses") or [])
	statuses.extend(type_suggestions.get("statuses") or [])
	if any(r.get("action") == "TYPE_CHANGE_SUGGESTED" for r in type_suggestions.get("rows") or []):
		statuses.append(S.TYPE_CHANGE_SUGGESTED)

	custom14 = inventory_custom14_server_script()

	link_backfills = [w for w in writes if w.get("type") == "sed_job_card_item"]
	non_consumed_field_writes = [
		w
		for w in writes
		if w.get("type") == "jc_item_field"
		and w.get("fieldname") != "consumed_qty"
		and w.get("write_required")
	]
	closed_wip = all(flt(r.get("wip_remainder")) <= 1e-9 for r in material_rows) if material_rows else False
	if closed_wip and not link_backfills and not non_consumed_field_writes:
		writes = [
			w
			for w in writes
			if not (w.get("type") == "jc_item_field" and w.get("fieldname") == "consumed_qty")
		]
		statuses = [s for s in statuses if s != S.JOB_CARD_TRACKING_INCOMPLETE]
		for r in material_rows:
			r["action"] = "NO CHANGE"
			r["statuses"] = [s for s in r.get("statuses") or [] if s != S.JOB_CARD_TRACKING_INCOMPLETE]
			for fp in r.get("field_plan") or []:
				if fp.get("fieldname") == "consumed_qty" and fp.get("write_required"):
					fp["write_required"] = False

	tracking_writes = [
		w
		for w in writes
		if (w.get("type") == "jc_item_field" and w.get("write_required"))
		or (w.get("type") == "sed_job_card_item" and w.get("action") == "BACKFILL LINK")
	]

	hard = _jc_hard_blockers(statuses, material_rows, type_suggestions)
	approvable_types = [r for r in (type_suggestions.get("rows") or []) if r.get("approval_enabled")]
	has_work = bool(tracking_writes) or bool(approvable_types)

	# Full selected-JC manufacture readiness (current DB state — no type approval yet)
	readiness = simulate_normal_make_stock_entry(job_card)
	mfg_ready = bool(readiness.get("ok"))
	if not mfg_ready:
		statuses.append(S.MANUFACTURE_READINESS_BLOCKED)
		statuses.append(S.DOWNSTREAM_MANUFACTURE_BLOCKED)

	if hard:
		overall = hard[0]
		apply_allowed = False
	elif has_work:
		overall = S.REBUILD_READY if tracking_writes else S.TYPE_CHANGE_SUGGESTED
		apply_allowed = True
	else:
		overall = S.BALANCED
		apply_allowed = False

	wo = frappe.db.get_value("Job Card", job_card, "work_order")

	return {
		"run_id": str(uuid.uuid4()),
		"job_card": job_card,
		"work_order": wo,
		"fingerprint": evidence["fingerprint"],
		"fingerprint_scope": "selected_job_card_dependency_closure",
		"posting_order_warning": evidence.get("posting_order_warning"),
		"material_rows": material_rows,
		"link_proposals": link_proposals,
		"secondary": secondary,
		"secondary_type_suggestions": type_suggestions,
		"custom14": custom14,
		"writes": tracking_writes,
		"statuses": list(dict.fromkeys(statuses)),
		"warnings": [s for s in statuses if s in S.WARNINGS_ALLOW_REBUILD],
		"blockers": hard,
		"apply_allowed": apply_allowed,
		"overall_status": overall,
		"manufacture_repair_required": S.MISSING_MANUFACTURE_CONSUMPTION in statuses
		or S.PARTIAL_MANUFACTURE_CONSUMPTION in statuses,
		"manufacture_readiness": readiness,
		"manufacture_readiness_status": (
			S.MANUFACTURE_READINESS_PASS if mfg_ready else S.MANUFACTURE_READINESS_BLOCKED
		),
		"job_card_rebuild_valid": not hard,
		"evidence_summary": {
			"stock_entry_count": len(evidence["stock_entries"]),
			"movement_count": len(evidence["movements"]),
		},
	}


def scan_job_card(
	job_card: str, item_filter: str | None = None, batch_filter: str | None = None
) -> dict:
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "SCAN"
	plan["mutated"] = False
	return plan


def preview_rebuild(
	job_card: str, item_filter: str | None = None, batch_filter: str | None = None
) -> dict:
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "PREVIEW"
	plan["mutated"] = False
	plan["proposed_snapshot"] = {
		"writes": plan["writes"],
		"secondary_type_suggestions": plan["secondary_type_suggestions"],
	}
	return plan


def _apply_writes(writes: list[dict]) -> list[dict]:
	applied = []
	for w in writes:
		if w.get("type") == "jc_item_field":
			if not w.get("jc_item") or not w.get("write_required"):
				continue
			frappe.db.set_value(
				"Job Card Item",
				w["jc_item"],
				w["fieldname"],
				w["derived"],
				update_modified=False,
			)
			applied.append(w)
		elif w.get("type") == "sed_job_card_item":
			if w.get("action") != "BACKFILL LINK" or not w.get("write_required", True):
				continue
			frappe.db.set_value(
				"Stock Entry Detail",
				w["detail_name"],
				"job_card_item",
				w["derived"],
				update_modified=False,
			)
			applied.append(w)
	return applied


def _verify_tracking(job_card: str, plan: dict) -> dict:
	mismatches = []
	for row in plan["material_rows"]:
		if not row.get("jc_item"):
			continue
		cur = frappe.db.get_value("Job Card Item", row["jc_item"], list(TRACKING_FIELDS), as_dict=1)
		for fp in row["field_plan"]:
			if not fp["write_required"]:
				continue
			if not qty_equal(cur.get(fp["fieldname"]), fp["derived"]):
				mismatches.append(
					{
						"jc_item": row["jc_item"],
						"field": fp["fieldname"],
						"expected": fp["derived"],
						"actual": cur.get(fp["fieldname"]),
					}
				)
	for w in plan["writes"]:
		if w.get("type") == "sed_job_card_item" and w.get("action") == "BACKFILL LINK":
			actual = frappe.db.get_value("Stock Entry Detail", w["detail_name"], "job_card_item")
			if actual != w["derived"]:
				mismatches.append(
					{
						"detail_name": w["detail_name"],
						"field": "job_card_item",
						"expected": w["derived"],
						"actual": actual,
					}
				)
	return {"ok": not mismatches, "mismatches": mismatches}


def _write_audit(payload: dict) -> str | None:
	if not frappe.db.exists("DocType", "Job Card Stock Rebuild Log"):
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Job Card Stock Rebuild Log",
			"run_id": payload.get("run_id"),
			"job_card": payload.get("job_card"),
			"work_order": payload.get("work_order"),
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"mode": payload.get("mode"),
			"status": payload.get("overall_status") or payload.get("status"),
			"before_snapshot": json.dumps(payload.get("before_snapshot") or {}, default=str),
			"evidence_snapshot": json.dumps(
				{
					"fingerprint": payload.get("fingerprint"),
					"evidence_summary": payload.get("evidence_summary"),
					"statuses": payload.get("statuses"),
					"secondary_type_suggestions": payload.get("secondary_type_suggestions"),
					"secondary_type_approvals": payload.get("secondary_type_approvals"),
					"manufacture_readiness_status": payload.get("manufacture_readiness_status"),
				},
				default=str,
			),
			"proposed_snapshot": json.dumps(payload.get("proposed_snapshot") or {}, default=str),
			"after_snapshot": json.dumps(payload.get("after_snapshot") or {}, default=str),
			"verification_snapshot": json.dumps(
				{
					"verification": payload.get("verification"),
					"manufacture_readiness": {
						"status": (payload.get("manufacture_readiness") or {}).get("status"),
						"error": (payload.get("manufacture_readiness") or {}).get("error"),
						"stage": (payload.get("manufacture_readiness") or {}).get("stage"),
						"classified_keys": list(
							((payload.get("manufacture_readiness") or {}).get("classified") or {}).keys()
						),
					},
				},
				default=str,
			),
			"warnings": json.dumps(payload.get("warnings") or [], default=str),
			"affected_links": json.dumps(
				[w for w in (payload.get("applied") or []) if w.get("type") in ("sed_job_card_item", "secondary_item_type")],
				default=str,
			),
			"error": payload.get("error") or "",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def dry_run_rebuild(
	job_card: str,
	fingerprint: str | None = None,
	item_filter: str | None = None,
	batch_filter: str | None = None,
	secondary_type_approvals=None,
	apply_scope: str | None = None,
) -> dict:
	"""DRY RUN — apply tracking + optional approved type changes inside savepoint, then rollback."""
	scope = _normalize_apply_scope(apply_scope)
	approvals_in = _parse_approvals(secondary_type_approvals)
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "DRY_RUN"
	plan["apply_scope"] = scope
	plan["secondary_type_approvals"] = approvals_in

	if fingerprint and fingerprint != plan["fingerprint"]:
		plan["overall_status"] = S.STALE_PREVIEW
		plan["apply_allowed"] = False
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = "STALE_PREVIEW — re-scan required"
		plan["mutated"] = False
		return plan

	v = validate_type_approvals(
		job_card, approvals_in, (plan.get("secondary_type_suggestions") or {}).get("rows") or []
	)
	if not v["ok"]:
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = json.dumps(v["errors"], default=str)
		plan["mutated"] = False
		return plan
	normalized = v["normalized"]

	if scope == SCOPE_SECONDARY_TYPE_ONLY and not normalized:
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = "SECONDARY_TYPE_ONLY requires an explicit secondary type approval"
		plan["mutated"] = False
		return plan

	if plan.get("blockers") and not normalized and not plan.get("writes"):
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = "Blocked: " + ", ".join(plan["blockers"])
		plan["mutated"] = False
		return plan

	if scope == SCOPE_SECONDARY_TYPE_ONLY and plan.get("blockers"):
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = "Blocked: " + ", ".join(plan["blockers"])
		plan["mutated"] = False
		return plan

	before = _snapshot_jc(job_card)
	sp = f"jc_rebuild_dry_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	try:
		frappe.db.sql("select name from `tabJob Card` where name=%s for update", job_card)
		if scope == SCOPE_SECONDARY_TYPE_ONLY:
			applied = _apply_secondary_types_via_document(job_card, normalized)
			verification = {
				"ok": True,
				"mismatches": [],
				"scope": scope,
				"tracking_writes_skipped": len(plan.get("writes") or []),
			}
		else:
			applied = _apply_writes(plan["writes"])
			applied_types = apply_type_approvals(normalized) if normalized else []
			applied.extend(applied_types)
			verification = _verify_tracking(job_card, plan) if plan["writes"] else {"ok": True, "mismatches": []}
		if not verification["ok"]:
			raise RuntimeError(f"Post-write verification failed: {verification['mismatches']}")

		# Full selected-JC real Make path AFTER temporary mutations
		readiness = simulate_normal_make_stock_entry(job_card)
		# Also keep RM candidate checks for missing manufacture consumption
		rm_sim = simulate_manufacture_candidates(job_card)
		rm_checks = []
		for row in plan["material_rows"]:
			expected = flt(row.get("net_available_for_manufacture"))
			if expected <= 0:
				continue
			if S.MISSING_MANUFACTURE_CONSUMPTION in (row.get("statuses") or []):
				rm_checks.append(expect_rm_candidate(rm_sim, row["item_code"], expected))

		result = deepcopy(plan)
		result["before_snapshot"] = before
		result["applied"] = applied
		result["verification"] = verification
		result["manufacture_readiness"] = readiness
		result["manufacture_readiness_status"] = (
			S.MANUFACTURE_READINESS_PASS if readiness.get("ok") else S.MANUFACTURE_READINESS_BLOCKED
		)
		result["rm_simulation"] = {"simulation": rm_sim, "rm_checks": rm_checks}
		result["mutated"] = False
		result["type_approvals_applied_in_txn"] = normalized
		result["dry_run_status"] = "DRY_RUN_PASS"
		result["dry_run"] = "PASS"
		if normalized:
			result["statuses"] = list(
				dict.fromkeys((result.get("statuses") or []) + [S.TYPE_CHANGE_APPROVED])
			)
		if not readiness.get("ok"):
			result["note"] = (
				"DRY_RUN_PASS with MANUFACTURE_READINESS_BLOCKED"
				if not normalized
				else f"Approved type change still blocked: {readiness.get('error')}"
			)
			# Approved types that were meant to fix readiness but didn't → dry-run FAIL for apply path
			if normalized:
				result["dry_run_status"] = "DRY_RUN_FAIL"
				result["dry_run"] = "FAIL"
				result["error"] = readiness.get("error") or S.MANUFACTURE_READINESS_BLOCKED
		return result
	except Exception as exc:
		plan["dry_run"] = "FAIL"
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = str(exc)
		plan["mutated"] = False
		return plan
	finally:
		frappe.db.rollback(save_point=sp)


def apply_rebuild(
	job_card: str,
	fingerprint: str,
	confirm: int | bool = 0,
	item_filter: str | None = None,
	batch_filter: str | None = None,
	secondary_type_approvals=None,
	apply_scope: str | None = None,
) -> dict:
	"""APPLY — atomic tracking + explicitly approved Secondary type changes.

	``apply_scope="SECONDARY_TYPE_ONLY"`` saves the approved Job Card secondary
	types and does not write tracking quantities or Stock Entry rows.
	"""
	if not cint_truthy(confirm):
		frappe.throw(frappe._("Confirmation required before Apply."))

	scope = _normalize_apply_scope(apply_scope)
	approvals_in = _parse_approvals(secondary_type_approvals)
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "APPLY"
	plan["apply_scope"] = scope
	plan["secondary_type_approvals"] = approvals_in

	if fingerprint != plan["fingerprint"]:
		plan["overall_status"] = S.STALE_PREVIEW
		plan["error"] = "STALE_PREVIEW — re-scan required"
		plan["mutated"] = False
		_write_audit(plan)
		return plan

	v = validate_type_approvals(
		job_card, approvals_in, (plan.get("secondary_type_suggestions") or {}).get("rows") or []
	)
	if not v["ok"]:
		plan["error"] = json.dumps(v["errors"], default=str)
		plan["mutated"] = False
		_write_audit(plan)
		return plan
	normalized = v["normalized"]

	if scope == SCOPE_SECONDARY_TYPE_ONLY and not normalized:
		plan["error"] = "SECONDARY_TYPE_ONLY requires an explicit secondary type approval"
		plan["mutated"] = False
		_write_audit(plan)
		return plan

	has_mutations = bool(plan.get("writes")) or bool(normalized)
	if scope != SCOPE_SECONDARY_TYPE_ONLY and not has_mutations:
		plan["status"] = S.NO_CHANGE
		plan["overall_status"] = S.BALANCED
		plan["note"] = "BALANCED / NO REBUILD REQUIRED"
		plan["mutated"] = False
		_write_audit(plan)
		return plan

	if plan.get("blockers"):
		plan["error"] = "Blocked: " + ", ".join(plan["blockers"])
		plan["mutated"] = False
		_write_audit(plan)
		return plan

	before = _snapshot_jc(job_card)
	sp = f"jc_rebuild_apply_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	try:
		frappe.db.sql("select name from `tabJob Card` where name=%s for update", job_card)
		fresh_fp = build_evidence(job_card, item_filter=item_filter, batch_filter=batch_filter)[
			"fingerprint"
		]
		if fresh_fp != fingerprint:
			raise RuntimeError(S.STALE_PREVIEW)

		if scope == SCOPE_SECONDARY_TYPE_ONLY:
			applied = _apply_secondary_types_via_document(job_card, normalized)
			verification = {"ok": True, "scope": scope, "tracking_writes_skipped": len(plan.get("writes") or [])}
		else:
			applied = _apply_writes(plan["writes"])
			applied_types = apply_type_approvals(normalized) if normalized else []
			applied.extend(applied_types)
			verification = _verify_tracking(job_card, plan) if plan["writes"] else {"ok": True}
		if not verification.get("ok"):
			raise RuntimeError(f"Verification failed: {verification.get('mismatches')}")

		# Verify approved types persisted
		for a in normalized:
			cur = frappe.db.get_value("Job Card Secondary Item", a["secondary_row"], "secondary_item_type")
			if cur != a["approved_type"]:
				raise RuntimeError(f"Type apply failed for {a['secondary_row']}")

		readiness = simulate_normal_make_stock_entry(job_card)
		if normalized and not readiness.get("ok"):
			raise RuntimeError(
				readiness.get("error")
				or "MANUFACTURE_READINESS_BLOCKED after approved type changes"
			)
		# If only tracking writes and readiness blocked by pre-existing stage issue, allow
		# persistence of tracking (Phase 1 contract) — type approvals require readiness PASS.

		after = _snapshot_jc(job_card)
		plan["before_snapshot"] = before
		plan["after_snapshot"] = after
		plan["applied"] = applied
		plan["verification"] = verification
		plan["manufacture_readiness"] = readiness
		plan["manufacture_readiness_status"] = (
			S.MANUFACTURE_READINESS_PASS if readiness.get("ok") else S.MANUFACTURE_READINESS_BLOCKED
		)
		plan["overall_status"] = S.REBUILT
		plan["status"] = S.REBUILT
		plan["mutated"] = True
		plan["proposed_snapshot"] = {
			"writes": plan["writes"],
			"secondary_type_approvals": normalized,
		}
		log_name = _write_audit(plan)
		plan["audit_log"] = log_name
		return plan
	except Exception as exc:
		frappe.db.rollback(save_point=sp)
		plan["error"] = str(exc)
		plan["overall_status"] = S.STALE_PREVIEW if str(exc) == S.STALE_PREVIEW else S.MANUAL_REVIEW
		plan["mutated"] = False
		_write_audit(plan)
		return plan


def cint_truthy(v) -> bool:
	if isinstance(v, bool):
		return v
	try:
		return int(v) == 1
	except Exception:
		return str(v).lower() in ("1", "true", "yes")


def scan_work_order_readonly(work_order: str) -> dict:
	"""Read-only candidate scan across Job Cards of a Work Order (no Apply)."""
	jcs = frappe.get_all(
		"Job Card", filters={"work_order": work_order}, pluck="name", order_by="name"
	)
	results = []
	for jc in jcs:
		plan = scan_job_card(jc)
		results.append(
			{
				"job_card": jc,
				"overall_status": plan["overall_status"],
				"apply_allowed": plan["apply_allowed"],
				"manufacture_repair_required": plan["manufacture_repair_required"],
				"manufacture_readiness_status": plan.get("manufacture_readiness_status"),
				"statuses": plan["statuses"],
			}
		)
	return {"work_order": work_order, "job_cards": results, "mutated": False}


__all__ = [
	"scan_job_card",
	"preview_rebuild",
	"dry_run_rebuild",
	"apply_rebuild",
	"scan_work_order_readonly",
]
