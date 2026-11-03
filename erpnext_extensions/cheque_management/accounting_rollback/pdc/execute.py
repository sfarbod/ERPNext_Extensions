"""Apply workflow / leaf / audit after accounting rollback steps."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from erpnext_extensions.cheque_management.accounting_rollback.models import RollbackPlan
from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	_pdc_get_cheque_leaf_row_for_update,
	_pdc_reserve_leaf_for_pdc,
)
from erpnext_extensions.cheque_management.pdc_lifecycle_events import (
	SNAPSHOT_FIELDS,
	mark_lifecycle_events_rolled_back,
	parse_snapshot_json,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	WORKFLOW_CLEARED,
	WORKFLOW_DRAFT,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	normalize_workflow_state_value,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	map_workflow_state_to_cheque_status,
)


def clear_outcome_fields_for_target(pdc, target_state: str) -> dict[str, Any]:
	target = normalize_workflow_state_value(target_state)
	updates: dict[str, Any] = {}
	if target != WORKFLOW_CLEARED:
		updates["cleared_date"] = None
		updates["clear_je_posted"] = 0
	if target in (WORKFLOW_DRAFT, WORKFLOW_REGISTERED):
		updates["returned_date"] = None
		updates["return_reason"] = None
		# Debt Purchase assignment stamps handover_date; clear when undoing to Registered/Draft.
		updates["handover_date"] = None
	if target in (WORKFLOW_DRAFT, WORKFLOW_REGISTERED, WORKFLOW_ISSUED):
		updates["instrument_dead"] = 0
		updates["instrument_dead_reason"] = None
	if target in (WORKFLOW_DRAFT, WORKFLOW_REGISTERED):
		updates["recognition_je_posted"] = 0
	return updates


def operational_updates_from_steps(pdc, plan: RollbackPlan) -> dict[str, Any]:
	"""Restore operational fields from the oldest undone event snapshot when present.

	Falls back to target-state clearing only when no snapshot exists (legacy documents).
	"""
	target = plan.target_workflow_state
	snapshot: dict[str, Any] = {}
	for step in reversed(plan.steps):
		parsed = parse_snapshot_json(step.snapshot_json)
		if parsed:
			snapshot = parsed
			break
	if not snapshot:
		return clear_outcome_fields_for_target(pdc, target)

	updates: dict[str, Any] = {}
	for field in SNAPSHOT_FIELDS:
		if field in ("workflow_state", "cheque_status", "docstatus"):
			continue
		if field in snapshot:
			updates[field] = snapshot[field]
	if "docstatus" in snapshot and target == WORKFLOW_DRAFT:
		updates["docstatus"] = 0
	elif "docstatus" in snapshot:
		updates["docstatus"] = snapshot["docstatus"]
	return updates


def _append_rollback_audit_row(pdc_name: str, row: dict[str, Any]) -> str:
	"""Insert-only audit — never reload/rewrite existing rollback log rows."""
	idx = frappe.db.count("PDC Workflow Rollback Log", {"parent": pdc_name}) + 1
	doc = frappe.get_doc(
		{
			"doctype": "PDC Workflow Rollback Log",
			"parent": pdc_name,
			"parenttype": "Post Dated Cheque",
			"parentfield": "workflow_rollback_logs",
			"idx": idx,
			**row,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def apply_pdc_instrument_after_rollback(
	pdc,
	plan: RollbackPlan,
	removed: list[dict],
	outstanding_updates: list[dict],
) -> dict[str, Any]:
	target = plan.target_workflow_state
	direction = (pdc.cheque_direction or "").strip()
	from_state = normalize_workflow_state_value(pdc.workflow_state)
	cheque_status = map_workflow_state_to_cheque_status(direction, target)
	if not cheque_status:
		frappe.throw(_("Cannot map workflow state {0} to cheque status.").format(target))

	frappe.flags.in_pdc_workflow_rollback = pdc.name
	try:
		updates = {
			"workflow_state": target,
			"cheque_status": cheque_status,
			**operational_updates_from_steps(pdc, plan),
		}
		if target == WORKFLOW_DRAFT:
			updates["docstatus"] = 0

		for field, value in updates.items():
			frappe.db.set_value("Post Dated Cheque", pdc.name, field, value, update_modified=True)

		_restore_leaf_for_target(pdc, target)

		event_names = [s.lifecycle_event_name for s in plan.steps if s.lifecycle_event_name]
		log_name = ""
		for step in plan.steps:
			step_removed = [
				r
				for r in removed
				if r.get("transition_key") == step.transition_key or r.get("name") == step.journal_entry
			]
			log_name = _append_rollback_audit_row(
				pdc.name,
				{
					"rolled_back_on": now_datetime(),
					"rolled_back_by": frappe.session.user,
					"from_state": step.from_state,
					"to_state": step.to_state,
					"transition_key": step.transition_key or "",
					"journal_entry": step.journal_entry or "",
					"reason": plan.reason,
					"deleted_documents": json.dumps(step_removed or removed, default=str),
				},
			)
		if event_names:
			mark_lifecycle_events_rolled_back(pdc.name, event_names, rollback_log=log_name)

		comment = _("Workflow rolled back: {0} → {1}. Reason: {2}").format(from_state, target, plan.reason)
		frappe.get_doc("Post Dated Cheque", pdc.name).add_comment("Workflow", comment)
	finally:
		frappe.flags.in_pdc_workflow_rollback = None

	return {
		"name": pdc.name,
		"workflow_state": target,
		"cheque_status": cheque_status,
		"outstanding_updates": outstanding_updates,
	}


def _restore_leaf_for_target(pdc, target_state: str) -> None:
	leaf = (getattr(pdc, "cheque_leaf", None) or "").strip()
	if pdc.cheque_direction != "Payable" or not leaf:
		return
	target = normalize_workflow_state_value(target_state)
	if target == WORKFLOW_DRAFT:
		row = _pdc_get_cheque_leaf_row_for_update(leaf)
		if row and row.status == "Used" and (row.linked_post_dated_cheque or "") == pdc.name:
			frappe.db.set_value(
				"Cheque Leaf",
				leaf,
				{
					"status": "Reserved",
					"linked_post_dated_cheque": None,
					"used_on": None,
					"reserved_by_pdc": pdc.name,
					"reserved_on": now_datetime(),
				},
				update_modified=False,
			)
		elif row and row.status == "Available":
			_pdc_reserve_leaf_for_pdc(leaf, pdc)
	elif target in (WORKFLOW_REGISTERED, WORKFLOW_ISSUED):
		row = _pdc_get_cheque_leaf_row_for_update(leaf)
		if row and row.status == "Reserved" and (row.reserved_by_pdc or "") == pdc.name:
			frappe.db.set_value(
				"Cheque Leaf",
				leaf,
				{
					"status": "Used",
					"linked_post_dated_cheque": pdc.name,
					"used_on": now_datetime(),
					"reserved_by_pdc": None,
					"reserved_on": None,
				},
				update_modified=False,
			)
