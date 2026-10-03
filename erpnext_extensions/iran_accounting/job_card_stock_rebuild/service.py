# Copyright (c) 2026, ERPNext Extensions contributors
"""Job Card Stock Rebuild service — SCAN / PREVIEW / DRY RUN / APPLY / VERIFY.

Phase 1: reconstruct Job Card tracking state from submitted SE/SLE.
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
	apply_gate_statuses,
	classify_secondary_unknown_block,
	expect_rm_candidate,
	inventory_custom14_server_script,
	simulate_manufacture_candidates,
	simulate_stage_output_guard_on_secondary,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary import inventory_secondary
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics import (
	TRACKING_FIELDS,
	derive_tracking_for_item,
	load_job_card_items,
	propose_link_backfills,
	qty_equal,
	reconcile_batches,
)


def _snapshot_jc(job_card: str) -> dict:
	items = load_job_card_items(job_card)
	meta = frappe.db.get_value(
		"Job Card",
		job_card,
		["name", "docstatus", "status", "work_order", "modified", "for_quantity", "production_item"],
		as_dict=1,
	)
	return {
		"job_card": meta,
		"items": items,
		"secondary": inventory_secondary(job_card, []).get("rows") if meta else [],
	}


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

	# Ownership: all SE must carry job_card (query already filters). Orphan WIP not owned → stop.
	for item in collapsed:
		jc_item = jc_items.get(item["item_code"])
		has_rm_activity = any(
			flt(item.get(k)) > 1e-9 for k in ("issued", "returned", "consumed", "component_scrap")
		)
		# Finished goods / secondary-only SE rows without a JC Item are not Phase-1 RM targets.
		if not jc_item and not has_rm_activity:
			continue
		if not jc_item and has_rm_activity:
			# RM movement without a JC Item row → orphan / manual review
			statuses.append(S.ORPHAN_WIP)
			statuses.append(S.MANUAL_REVIEW)
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
					"statuses": [S.ORPHAN_WIP, S.MANUAL_REVIEW],
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
			if lp.get("status") in S.APPLY_BLOCKERS:
				statuses.append(lp["status"])
			if lp.get("action") == "BACKFILL LINK":
				writes.append({"type": "sed_job_card_item", **lp})
		link_proposals.extend(links)
		statuses.extend(derived["statuses"])

		action = derived["action"]
		if any(lp.get("action") == "BACKFILL LINK" for lp in links):
			action = "UPDATE TRACKING" if action == "NO CHANGE" else action
			action = "BACKFILL LINK" if action == "NO CHANGE" else (
				"UPDATE TRACKING + BACKFILL LINK" if "UPDATE" in action else action
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
	statuses.extend(secondary.get("statuses") or [])
	statuses.extend(classify_secondary_unknown_block(secondary["rows"]))

	stage_guard = simulate_stage_output_guard_on_secondary(secondary["rows"])
	custom14 = inventory_custom14_server_script()

	# Suppress consumed_qty-only sync when WIP is already closed and all other
	# tracking/link fields already match (CTRL PO-JOB08761 pattern: Core
	# consumed_qty stayed 0 because Manufacture job_card_item was NULL, but
	# transferred/custom WIP fields already reflect a balanced remainder).
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
					fp["why"] = (
						fp.get("why")
						+ " Suppressed: WIP already closed and other tracking fields match (NO CHANGE)."
					)

	gate = apply_gate_statuses(statuses, stage_guard, secondary.get("statuses") or [])

	# Balanced when no tracking writes and no link backfills
	tracking_writes = [
		w
		for w in writes
		if (w.get("type") == "jc_item_field" and w.get("write_required"))
		or (w.get("type") == "sed_job_card_item" and w.get("action") == "BACKFILL LINK")
	]
	if not tracking_writes and gate["overall_status"] not in S.APPLY_BLOCKERS:
		gate["overall_status"] = S.BALANCED
		gate["apply_allowed"] = False

	if tracking_writes and gate["apply_allowed"]:
		gate["overall_status"] = S.REBUILD_READY
	wo = frappe.db.get_value("Job Card", job_card, "work_order")

	return {
		"run_id": str(uuid.uuid4()),
		"job_card": job_card,
		"work_order": wo,
		"fingerprint": evidence["fingerprint"],
		"posting_order_warning": evidence.get("posting_order_warning"),
		"material_rows": material_rows,
		"link_proposals": link_proposals,
		"secondary": secondary,
		"stage_guard": stage_guard,
		"custom14": custom14,
		"writes": tracking_writes,
		"statuses": gate["statuses"],
		"warnings": gate["warnings"],
		"blockers": gate["blockers"],
		"apply_allowed": gate["apply_allowed"] and bool(tracking_writes),
		"overall_status": gate["overall_status"]
		if tracking_writes
		else (S.BALANCED if not gate["blockers"] else gate["overall_status"]),
		"manufacture_repair_required": gate["manufacture_repair_required"],
		"manufacture_blocked_by_stage_output_configuration": gate[
			"manufacture_blocked_by_stage_output_configuration"
		],
		"job_card_rebuild_valid": gate["job_card_rebuild_valid"],
		"evidence_summary": {
			"stock_entry_count": len(evidence["stock_entries"]),
			"movement_count": len(evidence["movements"]),
		},
	}


def scan_job_card(
	job_card: str, item_filter: str | None = None, batch_filter: str | None = None
) -> dict:
	"""SCAN — read-only evidence + status (no mutations)."""
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "SCAN"
	plan["mutated"] = False
	return plan


def preview_rebuild(
	job_card: str, item_filter: str | None = None, batch_filter: str | None = None
) -> dict:
	"""PREVIEW — same as scan plus proposed field/link table (no mutations)."""
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "PREVIEW"
	plan["mutated"] = False
	plan["proposed_snapshot"] = {
		"writes": plan["writes"],
		"material_rows": [
			{
				"item_code": r["item_code"],
				"jc_item": r["jc_item"],
				"field_plan": r["field_plan"],
				"issued": r["issued"],
				"returned": r["returned"],
				"consumed": r["consumed"],
				"wip_remainder": r["wip_remainder"],
				"net_available_for_manufacture": r["net_available_for_manufacture"],
			}
			for r in plan["material_rows"]
		],
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
			# Link backfill only — no qty/rate/SLE/GL mutation
			frappe.db.set_value(
				"Stock Entry Detail",
				w["detail_name"],
				"job_card_item",
				w["derived"],
				update_modified=False,
			)
			applied.append(w)
	return applied


def _verify_after(job_card: str, plan: dict) -> dict:
	"""Reload and confirm derived tracking matches DB."""
	fresh = _build_plan(job_card)
	mismatches = []
	for row in plan["material_rows"]:
		if not row.get("jc_item"):
			continue
		cur = frappe.db.get_value(
			"Job Card Item",
			row["jc_item"],
			list(TRACKING_FIELDS),
			as_dict=1,
		)
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
	return {
		"ok": not mismatches,
		"mismatches": mismatches,
		"fresh_overall": fresh.get("overall_status"),
		"fresh_apply_allowed": fresh.get("apply_allowed"),
	}


def _downstream_checks(job_card: str, plan: dict) -> dict:
	sim = simulate_manufacture_candidates(job_card)
	checks = {"simulation": sim, "rm_checks": [], "ok": sim.get("ok", False)}
	if not sim.get("ok"):
		checks["status"] = S.DOWNSTREAM_MANUFACTURE_BLOCKED
		return checks
	for row in plan["material_rows"]:
		expected = flt(row.get("net_available_for_manufacture"))
		if expected <= 0:
			continue
		# Only require candidate when missing/partial manufacture consumption
		if S.MISSING_MANUFACTURE_CONSUMPTION in row.get("statuses", []) or S.PARTIAL_MANUFACTURE_CONSUMPTION in row.get(
			"statuses", []
		):
			chk = expect_rm_candidate(sim, row["item_code"], expected)
			checks["rm_checks"].append(chk)
			if not chk["ok"]:
				checks["ok"] = False
				checks["status"] = S.DOWNSTREAM_MANUFACTURE_BLOCKED
	return checks


def _write_audit(payload: dict) -> str | None:
	"""Persist Job Card Stock Rebuild Log when DocType exists."""
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
				},
				default=str,
			),
			"proposed_snapshot": json.dumps(payload.get("proposed_snapshot") or payload.get("writes") or {}, default=str),
			"after_snapshot": json.dumps(payload.get("after_snapshot") or {}, default=str),
			"verification_snapshot": json.dumps(payload.get("verification") or {}, default=str),
			"warnings": json.dumps(payload.get("warnings") or [], default=str),
			"affected_links": json.dumps(
				[w for w in (payload.get("applied") or []) if w.get("type") == "sed_job_card_item"],
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
) -> dict:
	"""DRY RUN — real rebuild inside a rolled-back transaction."""
	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "DRY_RUN"
	if fingerprint and fingerprint != plan["fingerprint"]:
		plan["overall_status"] = S.STALE_PREVIEW
		plan["apply_allowed"] = False
		plan["blockers"] = list(dict.fromkeys((plan.get("blockers") or []) + [S.STALE_PREVIEW]))
		plan["dry_run"] = "FAIL"
		plan["error"] = "STALE_PREVIEW — re-scan required"
		plan["mutated"] = False
		return plan

	if not plan.get("apply_allowed") and plan.get("overall_status") == S.BALANCED:
		plan["dry_run"] = "PASS"
		plan["dry_run_status"] = "DRY_RUN_PASS"
		plan["note"] = "BALANCED / NO REBUILD REQUIRED"
		plan["mutated"] = False
		return plan

	if plan.get("blockers"):
		plan["dry_run"] = "FAIL"
		plan["dry_run_status"] = "DRY_RUN_FAIL"
		plan["error"] = "Blocked: " + ", ".join(plan["blockers"])
		plan["mutated"] = False
		return plan

	before = _snapshot_jc(job_card)
	sp = f"jc_rebuild_dry_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	try:
		frappe.db.sql("select name from `tabJob Card` where name=%s for update", job_card)
		applied = _apply_writes(plan["writes"])
		verification = _verify_after(job_card, plan)
		if not verification["ok"]:
			raise RuntimeError(f"Post-write verification failed: {verification['mismatches']}")
		downstream = _downstream_checks(job_card, plan)
		stage_after = simulate_stage_output_guard_on_secondary(plan["secondary"]["rows"])
		result = deepcopy(plan)
		result["before_snapshot"] = before
		result["applied"] = applied
		result["verification"] = verification
		result["downstream"] = downstream
		result["stage_guard_after"] = stage_after
		result["mutated"] = False  # savepoint rolled back
		if not downstream.get("ok"):
			result["dry_run"] = "FAIL"
			result["dry_run_status"] = "DRY_RUN_FAIL"
			result["error"] = downstream.get("status") or downstream.get("simulation", {}).get("error")
			result["overall_status"] = S.DOWNSTREAM_MANUFACTURE_BLOCKED
		elif stage_after.get("manufacture_blocked_by_stage_output_configuration"):
			result["dry_run"] = "PASS"
			result["dry_run_status"] = "DRY_RUN_PASS"
			result["job_card_rebuild_valid"] = True
			result["manufacture_blocked_by_stage_output_configuration"] = True
			result["note"] = (
				"JOB_CARD_REBUILD_VALID BUT MANUFACTURE_BLOCKED_BY_STAGE_OUTPUT_CONFIGURATION"
			)
		else:
			result["dry_run"] = "PASS"
			result["dry_run_status"] = "DRY_RUN_PASS"
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
) -> dict:
	"""APPLY — atomic rebuild of approved tracking/link fields."""
	if not cint_truthy(confirm):
		frappe.throw(frappe._("Confirmation required before Apply."))

	plan = _build_plan(job_card, item_filter, batch_filter)
	plan["mode"] = "APPLY"
	if fingerprint != plan["fingerprint"]:
		plan["overall_status"] = S.STALE_PREVIEW
		plan["apply_allowed"] = False
		plan["error"] = "STALE_PREVIEW — re-scan required"
		plan["mutated"] = False
		_write_audit(plan)
		return plan

	if plan.get("overall_status") == S.BALANCED or not plan.get("apply_allowed"):
		plan["status"] = S.NO_CHANGE
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
		applied = _apply_writes(plan["writes"])
		verification = _verify_after(job_card, plan)
		if not verification["ok"]:
			raise RuntimeError(f"Verification failed: {verification['mismatches']}")
		downstream = _downstream_checks(job_card, plan)
		if not downstream.get("ok"):
			raise RuntimeError(
				downstream.get("status")
				or downstream.get("simulation", {}).get("error")
				or "DOWNSTREAM_MANUFACTURE_BLOCKED"
			)
		after = _snapshot_jc(job_card)
		plan["before_snapshot"] = before
		plan["after_snapshot"] = after
		plan["applied"] = applied
		plan["verification"] = verification
		plan["downstream"] = downstream
		plan["overall_status"] = S.REBUILT
		plan["status"] = S.REBUILT
		plan["mutated"] = True
		plan["manufacture_repair_required"] = plan.get("manufacture_repair_required")
		log_name = _write_audit(plan)
		plan["audit_log"] = log_name
		return plan
	except Exception as exc:
		frappe.db.rollback(save_point=sp)
		plan["error"] = str(exc)
		plan["overall_status"] = S.MANUAL_REVIEW if str(exc) != S.STALE_PREVIEW else S.STALE_PREVIEW
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
		"Job Card",
		filters={"work_order": work_order},
		pluck="name",
		order_by="name",
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
