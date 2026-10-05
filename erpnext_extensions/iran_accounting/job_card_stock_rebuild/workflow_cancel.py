# Copyright (c) 2026, ERPNext Extensions contributors
"""Workflow-consistent Stock Entry cancellation helpers for Job Card Stock Rebuild.

Core ``Document._save`` skips ``_validate()`` when ``_action == "cancel"``, so
``set_workflow_state_on_action`` never runs on a direct ``doc.cancel()``.
Interactive cancels go through ``apply_workflow``, which stamps
``workflow_state`` to the transition's ``next_state`` *before* cancel.

Repair must mirror that stamp without requiring a browser workflow action.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint


def resolve_cancel_workflow_state(doc) -> tuple[str | None, str | None]:
	"""Return ``(workflow_state_field, cancel_state)`` from the active Workflow.

	Resolution order (no English-string dependency):
	1. Transition from the document's current state whose next state has
	   ``doc_status == 2``.
	2. Fallback matching Core ``set_workflow_state_on_action``: first Workflow
	   Document State with ``doc_status == 2``.

	Returns ``(None, None)`` when the DocType has no active Workflow or no
	configured cancellation state.
	"""
	workflow_name = doc.meta.get_workflow()
	if not workflow_name:
		return None, None

	workflow = frappe.get_doc("Workflow", workflow_name)
	field = workflow.workflow_state_field
	if not field:
		return None, None

	cancel_states = [s.state for s in workflow.states if str(s.doc_status) == "2"]
	if not cancel_states:
		return field, None

	current = doc.get(field)
	if current:
		# Already on a configured cancel state — keep it.
		if current in cancel_states:
			return field, current
		for transition in workflow.transitions:
			if transition.state == current and transition.next_state in cancel_states:
				return field, transition.next_state

	# Core set_workflow_state_on_action iterates workflow.states in order.
	return field, cancel_states[0]


def stamp_cancel_workflow_state(doc) -> str | None:
	"""Stamp the configured cancel ``workflow_state`` on the in-memory document.

	Call *before* ``doc.cancel()`` so the cancel ``db_update`` persists both
	``docstatus`` and ``workflow_state`` together (same pattern as
	``apply_workflow``). Does nothing when there is no applicable Workflow.
	"""
	field, target = resolve_cancel_workflow_state(doc)
	if not field or not target:
		return None
	if doc.get(field) != target:
		doc.set(field, target)
	return target


def ensure_cancel_workflow_state_persisted(doc) -> str | None:
	"""After a successful ``doc.cancel()``, persist cancel workflow_state if needed.

	Only writes when ``docstatus == 2``. Never stamps on a failed cancel.
	"""
	if cint(doc.docstatus) != 2:
		return None

	field, target = resolve_cancel_workflow_state(doc)
	if not field or not target:
		return None

	db_val = frappe.db.get_value(doc.doctype, doc.name, field)
	if db_val != target:
		doc.db_set(field, target, update_modified=False)
	elif doc.get(field) != target:
		doc.set(field, target)
	return target


def expected_cancel_state_for_stock_entry(name: str) -> dict[str, Any]:
	"""Resolve expected cancel workflow_state for one Stock Entry (read-only)."""
	doc = frappe.get_doc("Stock Entry", name)
	workflow_name = doc.meta.get_workflow()
	field, target = resolve_cancel_workflow_state(doc)
	return {
		"name": name,
		"workflow": workflow_name,
		"workflow_state_field": field,
		"expected_cancel_state": target,
		"workflow_state": doc.get(field) if field else doc.get("workflow_state"),
		"docstatus": cint(doc.docstatus),
	}


def scan_stale_cancelled_stock_entry_workflow(
	names: list[str] | None = None,
	limit: int = 500,
) -> dict[str, Any]:
	"""Read-only diagnostic: cancelled Stock Entries with stale workflow_state.

	A row is stale when:
	- ``docstatus == 2``
	- an active Workflow applies
	- a configured cancellation state exists
	- ``workflow_state`` is not that cancellation state

	Documents with no Workflow are never flagged.
	Does not mutate data.
	"""
	filters: dict[str, Any] = {"docstatus": 2}
	if names:
		filters["name"] = ("in", names)

	rows = frappe.get_all(
		"Stock Entry",
		filters=filters,
		fields=[
			"name",
			"stock_entry_type",
			"purpose",
			"docstatus",
			"workflow_state",
			"posting_date",
			"job_card",
			"work_order",
			"modified",
		],
		order_by="modified desc",
		limit_page_length=limit,
	)

	stale: list[dict[str, Any]] = []
	ok: list[dict[str, Any]] = []
	skipped_no_workflow = 0
	skipped_no_cancel_state = 0

	for row in rows:
		doc = frappe.get_doc("Stock Entry", row.name)
		workflow_name = doc.meta.get_workflow()
		if not workflow_name:
			skipped_no_workflow += 1
			continue
		field, expected = resolve_cancel_workflow_state(doc)
		if not expected:
			skipped_no_cancel_state += 1
			continue
		current = doc.get(field) if field else row.workflow_state
		entry = {
			"name": row.name,
			"stock_entry_type": row.stock_entry_type,
			"purpose": row.purpose,
			"docstatus": cint(row.docstatus),
			"workflow_state": current,
			"expected_cancellation_state": expected,
			"workflow": workflow_name,
			"posting_date": str(row.posting_date) if row.posting_date else None,
			"job_card": row.job_card,
			"work_order": row.work_order,
			"modified": str(row.modified) if row.modified else None,
			"stale": current != expected,
		}
		if entry["stale"]:
			stale.append(entry)
		else:
			ok.append(entry)

	return {
		"ok": True,
		"read_only": True,
		"scanned": len(rows),
		"stale_count": len(stale),
		"consistent_count": len(ok),
		"skipped_no_workflow": skipped_no_workflow,
		"skipped_no_cancel_state": skipped_no_cancel_state,
		"stale": stale,
		"consistent_sample": ok[:20],
	}
