# Copyright (c) 2026, ERPNext Extensions contributors
"""Controlled workflow rollback for Post Dated Cheque (Administrator / System Manager only).

Rolls ``workflow_state`` backward along valid forward paths, cancels transition Journal Entries
recorded in ``journal_references``, restores cheque leaf + instrument flags, and appends audit rows
on ``workflow_rollback_logs``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint

from erpnext_extensions.cheque_management.accounting_rollback.engine import execute_rollback_plan
from erpnext_extensions.cheque_management.accounting_rollback.pdc.execute import (
	apply_pdc_instrument_after_rollback,
)
from erpnext_extensions.cheque_management.accounting_rollback.pdc.plan import (
	_bfs_forward_path,
	_edges_to_undo,
	_forward_edges_on_path,
	build_pdc_rollback_plan,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	ALL_WORKFLOW_STATES,
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_CLEARED,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	WORKFLOW_DRAFT,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	WORKFLOW_RETURNED,
	WORKFLOW_SENT_TO_BANK,
	normalize_workflow_state_value,
)


def user_may_rollback_pdc_workflow(pdc_name: str | None = None) -> bool:
	from erpnext_extensions.cheque_management.pdc_workflow_rollback_permission import (
		user_may_rollback_pdc,
	)

	return user_may_rollback_pdc(pdc_name)


def _require_rollback_permission(pdc_name: str | None = None) -> None:
	if not user_may_rollback_pdc_workflow(pdc_name):
		raise ValidationError(
			_("You do not have permission to rollback Post Dated Cheque workflow. Check PDC Settings roles.")
		)


def get_rollback_target_states(pdc_name: str) -> list[str]:
	"""Return workflow states that are valid rollback targets for the current PDC."""
	doc = frappe.get_doc("Post Dated Cheque", pdc_name)
	direction = (doc.cheque_direction or "").strip()
	if direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
		return []
	if cint(doc.docstatus) != 1:
		return []
	current = normalize_workflow_state_value(doc.workflow_state)
	if current == WORKFLOW_DRAFT:
		return []
	# Debt Purchase Settled is owned by Facility Repayment. Cancel the repayment
	# first (restores Assigned); do not undo settlement via PDC rollback.
	if current == WORKFLOW_DEBT_PURCHASE_SETTLED:
		return []

	if cint(doc.is_opening_import):
		from erpnext_extensions.cheque_management.accounting_rollback.pdc.plan import (
			event_history_rollback_targets,
		)

		event_targets = event_history_rollback_targets(doc)
		if event_targets is not None:
			return event_targets
		return _opening_import_rollback_targets(doc)

	from erpnext_extensions.cheque_management.accounting_rollback.pdc.plan import (
		event_history_rollback_targets,
	)

	event_targets = event_history_rollback_targets(doc)
	if event_targets is not None:
		return event_targets

	targets: list[str] = []
	for state in ALL_WORKFLOW_STATES:
		state = normalize_workflow_state_value(state)
		if state == current:
			continue
		if _bfs_forward_path(direction, state, current):
			targets.append(state)
	targets = sorted(set(targets), key=lambda s: (_workflow_rank(direction, s), s))
	return targets


def _opening_import_rollback_targets(doc) -> list[str]:
	from erpnext_extensions.cheque_management.pdc_opening_import_baseline import (
		_workflow_rank,
		resolve_opening_import_baseline_state,
	)

	direction = (doc.cheque_direction or "").strip()
	current = normalize_workflow_state_value(doc.workflow_state)
	baseline = resolve_opening_import_baseline_state(doc)
	if not baseline:
		return []
	if _workflow_rank(direction, current) <= _workflow_rank(direction, baseline):
		return []

	targets: list[str] = []
	for state in ALL_WORKFLOW_STATES:
		state = normalize_workflow_state_value(state)
		if state == current:
			continue
		if _workflow_rank(direction, state) < _workflow_rank(direction, baseline):
			continue
		if _workflow_rank(direction, state) >= _workflow_rank(direction, current):
			continue
		if not _bfs_forward_path(direction, state, current):
			continue
		try:
			edges = _edges_to_undo(direction, current, state, pdc=doc)
		except ValidationError:
			continue
		if edges:
			targets.append(state)
			continue
		# Baseline is a valid target after post-import progress even when undo edges are
		# operational-only (e.g. Registered→Issued without a Journal Reference).
		if state == baseline and _workflow_rank(direction, current) > _workflow_rank(direction, baseline):
			targets.append(state)
	return sorted(set(targets), key=lambda s: (_workflow_rank(direction, s), s))


def _workflow_rank(direction: str, state: str) -> int:
	order_payable = [WORKFLOW_DRAFT, WORKFLOW_REGISTERED, WORKFLOW_ISSUED, WORKFLOW_CLEARED]
	# Assigned DP sits after Registered (parallel to Sent to Bank on the collection path).
	order_receivable = [
		WORKFLOW_DRAFT,
		WORKFLOW_REGISTERED,
		WORKFLOW_SENT_TO_BANK,
		WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		WORKFLOW_CLEARED,
	]
	order = order_payable if direction == CHEQUE_DIRECTION_PAYABLE else order_receivable
	state = normalize_workflow_state_value(state)
	try:
		return order.index(state)
	except ValueError:
		return 99


def _validate_pdc_rollback_request(pdc_name: str, target_state: str):
	pdc_name = (pdc_name or "").strip()
	target_state = normalize_workflow_state_value(target_state)
	if not pdc_name or not frappe.db.exists("Post Dated Cheque", pdc_name):
		raise ValidationError(_("Post Dated Cheque {0} does not exist.").format(pdc_name))

	pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
	if cint(pdc.docstatus) != 1:
		raise ValidationError(_("Only submitted Post Dated Cheques can be rolled back."))

	current = normalize_workflow_state_value(pdc.workflow_state)
	if current == WORKFLOW_DEBT_PURCHASE_SETTLED:
		raise ValidationError(
			_(
				"Debt Purchase Settled cheques cannot be rolled back from Post Dated Cheque. "
				"Cancel the linked Facility Repayment first to restore Assigned to Bank for Debt Purchase."
			)
		)
	allowed = get_rollback_target_states(pdc_name)
	if target_state not in allowed:
		raise ValidationError(_("Target state {0} is not allowed for rollback.").format(target_state))
	return pdc, current, target_state


def _run_pdc_rollback_plan(pdc, target_state: str, *, reason: str = "", dry_run: bool) -> dict[str, Any]:
	plan = build_pdc_rollback_plan(pdc, target_state, reason=reason)
	return execute_rollback_plan(
		plan,
		pdc,
		dry_run=dry_run,
		apply_instrument_fn=apply_pdc_instrument_after_rollback,
	)


@frappe.whitelist()
def get_pdc_rollback_target_states(pdc_name: str) -> list[str]:
	_require_rollback_permission(pdc_name)
	return get_rollback_target_states(pdc_name)


@frappe.whitelist()
def get_pdc_workflow_rollback_preview(pdc_name: str, target_state: str) -> dict[str, Any]:
	_require_rollback_permission(pdc_name)
	pdc, _current, target_state = _validate_pdc_rollback_request(pdc_name, target_state)
	return _run_pdc_rollback_plan(pdc, target_state, dry_run=True)


@frappe.whitelist()
def rollback_workflow_state(pdc_name: str, target_state: str, reason: str) -> dict[str, Any]:
	"""Rollback PDC workflow to ``target_state``; cancel transition JEs and restore instrument state."""
	_require_rollback_permission(pdc_name)
	reason = (reason or "").strip()
	if not reason:
		raise ValidationError(_("Rollback reason is required."))

	pdc_name = (pdc_name or "").strip()
	# Serialize competing rollbacks on the same cheque (symmetric with Facility DP settle lock).
	from frappe.utils.synchronization import filelock

	lock_name = f"pdc_workflow_rollback_{pdc_name}"
	with filelock(lock_name, timeout=120):
		pdc, _current, target_state = _validate_pdc_rollback_request(pdc_name, target_state)
		result = _run_pdc_rollback_plan(pdc, target_state, reason=reason, dry_run=False)
		frappe.db.commit()
		return result


def sql_verify_no_orphan_gl_for_pdc(pdc_name: str, journal_entries: list[str]) -> dict[str, int]:
	"""Test helper: counts GL/PLE rows for cancelled JEs (should be 0 when docstatus=2)."""
	if not journal_entries:
		return {"gl_entry": 0, "payment_ledger_entry": 0}
	gl = frappe.db.count("GL Entry", {"voucher_no": ["in", journal_entries], "is_cancelled": 0})
	ple = frappe.db.count("Payment Ledger Entry", {"voucher_no": ["in", journal_entries], "delinked": 0})
	return {"gl_entry": gl, "payment_ledger_entry": ple}


def sql_verify_pdc_rollback_integrity(
	pdc_name: str,
	*,
	cancelled_journal_entries: list[str] | None = None,
) -> dict[str, int | bool]:
	"""Integration/E2E helper: verify accounting + audit tables after a rollback step."""
	cancelled_journal_entries = list(cancelled_journal_entries or [])
	out: dict[str, int | bool] = {}

	active_refs = frappe.get_all(
		"PDC Journal Reference",
		filters={"parent": pdc_name, "parenttype": "Post Dated Cheque"},
		fields=["name", "journal_entry"],
	)
	out["pdc_journal_reference"] = len(active_refs)

	for row in active_refs:
		je = row.journal_entry
		if not je or not frappe.db.exists("Journal Entry", je):
			out["orphan_journal_reference"] = True
			continue
		if frappe.db.get_value("Journal Entry", je, "docstatus") != 1:
			out["orphan_journal_reference"] = True

	out.setdefault("orphan_journal_reference", False)

	for je in cancelled_journal_entries:
		if not je:
			continue
		out[f"je_{je}_docstatus"] = frappe.db.get_value("Journal Entry", je, "docstatus") or 0
		out[f"je_{je}_gl_active"] = frappe.db.count("GL Entry", {"voucher_no": je, "is_cancelled": 0})
		out[f"je_{je}_ple_active"] = frappe.db.count(
			"Payment Ledger Entry", {"voucher_no": je, "delinked": 0}
		)
		out[f"je_{je}_pdc_ref"] = frappe.db.count(
			"PDC Journal Reference",
			{"parent": pdc_name, "journal_entry": je},
		)

	out["version"] = frappe.db.count("Version", {"ref_doctype": "Post Dated Cheque", "docname": pdc_name})
	out["comment"] = frappe.db.count(
		"Comment",
		{"reference_doctype": "Post Dated Cheque", "reference_name": pdc_name},
	)

	pdc = frappe.db.get_value(
		"Post Dated Cheque",
		pdc_name,
		["workflow_state", "cheque_status", "cheque_leaf", "cheque_direction"],
		as_dict=True,
	)
	if pdc and pdc.cheque_leaf:
		leaf = frappe.db.get_value(
			"Cheque Leaf",
			pdc.cheque_leaf,
			["status", "linked_post_dated_cheque", "reserved_by_pdc"],
			as_dict=True,
		)
		out["cheque_leaf_status"] = (leaf.status or "") if leaf else ""
		out["cheque_leaf_linked_pdc"] = (leaf.linked_post_dated_cheque or "") if leaf else ""
		out["cheque_leaf_reserved_by"] = (leaf.reserved_by_pdc or "") if leaf else ""

	# Stray GL/PLE pointing at this PDC voucher (should only exist via active submitted JEs)
	out["gl_entry_on_pdc_voucher"] = frappe.db.count(
		"GL Entry",
		{"voucher_type": "Post Dated Cheque", "voucher_no": pdc_name, "is_cancelled": 0},
	)
	out["payment_ledger_on_pdc_voucher"] = frappe.db.count(
		"Payment Ledger Entry",
		{"voucher_type": "Post Dated Cheque", "voucher_no": pdc_name, "delinked": 0},
	)

	return out


def sql_integrity_is_clean(
	report: dict[str, int | bool], cancelled_journal_entries: list[str] | None = None
) -> bool:
	if report.get("orphan_journal_reference"):
		return False
	for je in cancelled_journal_entries or []:
		if report.get(f"je_{je}_gl_active", 0):
			return False
		if report.get(f"je_{je}_ple_active", 0):
			return False
		if report.get(f"je_{je}_pdc_ref", 0):
			return False
		if report.get(f"je_{je}_docstatus") not in (None, 2):
			return False
	return True


def validate_workflow_rollback_logs_immutable(doc) -> None:
	"""Prevent edit/delete of workflow rollback audit rows on normal PDC saves."""
	if doc.is_new() or getattr(frappe.flags, "in_pdc_workflow_rollback", None):
		return
	prev = set(frappe.get_all("PDC Workflow Rollback Log", filters={"parent": doc.name}, pluck="name"))
	if not prev:
		return
	current = {r.name for r in (doc.get("workflow_rollback_logs") or []) if r.name}
	if prev - current:
		frappe.throw(_("Workflow rollback audit log is immutable; rows cannot be removed."))
	immutable_fields = (
		"from_state",
		"to_state",
		"reason",
		"transition_key",
		"journal_entry",
		"rolled_back_on",
		"rolled_back_by",
		"deleted_documents",
	)
	for row in doc.get("workflow_rollback_logs") or []:
		if not row.name or row.name not in prev:
			continue
		old = frappe.db.get_value("PDC Workflow Rollback Log", row.name, immutable_fields, as_dict=True)
		for field in immutable_fields:
			if str(row.get(field) or "") != str((old or {}).get(field) or ""):
				frappe.throw(_("Workflow rollback audit log is immutable; rows cannot be modified."))


@frappe.whitelist()
def check_user_may_rollback_pdc_workflow(pdc_name: str) -> bool:
	"""Desk: policy-based rollback button visibility."""
	return user_may_rollback_pdc_workflow(pdc_name)


def opening_import_rollback_diagnostics(pdc_name: str) -> dict[str, Any]:
	"""Support / test helper: snapshot rollback inputs for opening-import PDCs."""
	from erpnext_extensions.cheque_management.accounting_rollback.pdc.plan import (
		build_pdc_rollback_plan,
		index_journal_references,
	)
	from erpnext_extensions.cheque_management.pdc_opening_import_baseline import (
		resolve_opening_import_baseline_state,
	)

	doc = frappe.get_doc("Post Dated Cheque", pdc_name)
	direction = (doc.cheque_direction or "").strip()
	current = normalize_workflow_state_value(doc.workflow_state)
	baseline = resolve_opening_import_baseline_state(doc) if cint(doc.is_opening_import) else None
	refs = index_journal_references(pdc_name)
	targets = get_rollback_target_states(pdc_name)
	out: dict[str, Any] = {
		"pdc_name": pdc_name,
		"cheque_direction": direction,
		"is_opening_import": cint(doc.is_opening_import),
		"opening_import": doc.get("opening_import"),
		"workflow_state": current,
		"opening_import_workflow_state": doc.get("opening_import_workflow_state"),
		"resolved_baseline": baseline,
		"journal_references": list(refs.values()),
		"transition_keys": [r.get("pdc_transition_key") for r in refs.values()],
		"rollback_targets": targets,
	}
	if targets:
		target = targets[-1]
		try:
			plan = build_pdc_rollback_plan(doc, target, reason="diagnostics")
			out["rollback_plan_steps"] = [
				{"from": s.from_state, "to": s.to_state, "je": s.journal_entry} for s in plan.steps
			]
			out["undo_edges"] = [(s.from_state, s.to_state) for s in plan.steps]
			out["preview"] = plan.to_api_dict()
			out["ignored_historical_journal_entries"] = list(
				getattr(plan, "ignored_historical_journal_entries", None) or []
			)
		except Exception as e:
			out["plan_error"] = str(e)
	else:
		out["undo_edges"] = []
	return out


__all__ = [
	"_bfs_forward_path",
	"_edges_to_undo",
	"_forward_edges_on_path",
	"check_user_may_rollback_pdc_workflow",
	"get_pdc_workflow_rollback_preview",
	"get_rollback_target_states",
	"opening_import_rollback_diagnostics",
	"rollback_workflow_state",
	"sql_integrity_is_clean",
	"sql_verify_no_orphan_gl_for_pdc",
	"sql_verify_pdc_rollback_integrity",
	"user_may_rollback_pdc_workflow",
]
