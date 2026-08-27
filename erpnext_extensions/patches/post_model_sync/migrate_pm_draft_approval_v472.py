# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.2: Draft approval until final Finance submit (Pending* stay docstatus=0).

Hard cutover for first-time apply: aborts if any in-flight Pending* Request/
Clearance exist (no grandfathering of submitted Pending docs into draft
Pending* states).

Idempotent / version-aware paths (4.8.0):
- If the active PM workflows already encode draft-approval (Pending* doc_status
  0 + Return-for-Correction transitions), complete without requiring an empty
  Pending queue and without mutating PM Request / Clearance documents.
- If cutover is still required but in-flight Pending* docs exist, defer (do not
  rebuild workflow, do not alter documents) and leave a site flag so
  after_migrate can retry when the queue is clear. This unblocks unrelated
  schema migrates (e.g. ERPNext field renames) without waiving cutover safety.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
	_bulk_apply_pending_assignments,
	_rebuild_pm_clearance_workflow,
	_rebuild_pm_request_workflow,
	_seed_assignment_rules,
	_wf,
)
from erpnext_extensions.petty_management.services.workflow_utils import realign_doctype_workflow_states

REQUEST_PENDING_TITLES = (
	"Pending Manager Approval",
	"Pending CEO Approval",
	"Pending Finance Approval",
)
CLEARANCE_PENDING_TITLES = (
	"Pending Manager Approval",
	"Pending Finance Review",
)

DEFERRED_FLAG_KEY = "pm_draft_approval_v472_deferred"
APPLIED_FLAG_KEY = "pm_draft_approval_v472_applied"


def _workflow_state_names_for_titles(titles: tuple[str, ...]) -> list[str]:
	names: list[str] = []
	for title in titles:
		link = _wf(title)
		if link and link not in names:
			names.append(link)
		# Also match rows stored as the display title itself
		if title not in names:
			names.append(title)
	return names


def count_in_flight_pending_pm_docs() -> dict:
	"""Count Pending* PM Request / PM Clearance (any docstatus)."""
	req_states = _workflow_state_names_for_titles(REQUEST_PENDING_TITLES)
	clr_states = _workflow_state_names_for_titles(CLEARANCE_PENDING_TITLES)

	req_names: list[str] = []
	clr_names: list[str] = []
	if frappe.db.has_table("PM Request"):
		req_names = frappe.get_all(
			"PM Request",
			filters={"workflow_state": ("in", req_states)},
			pluck="name",
			order_by="modified desc",
		)
	if frappe.db.has_table("PM Clearance"):
		clr_names = frappe.get_all(
			"PM Clearance",
			filters={"workflow_state": ("in", clr_states)},
			pluck="name",
			order_by="modified desc",
		)
	return {
		"request_count": len(req_names),
		"clearance_count": len(clr_names),
		"request_names": req_names,
		"clearance_names": clr_names,
	}


def assert_no_in_flight_pending_pm_docs() -> None:
	"""Abort cutover when any Pending* docs exist (before workflow rebuild)."""
	stats = count_in_flight_pending_pm_docs()
	req_n = stats["request_count"]
	clr_n = stats["clearance_count"]
	if req_n == 0 and clr_n == 0:
		return

	def _sample(names: list[str], limit: int = 20) -> str:
		shown = names[:limit]
		extra = len(names) - len(shown)
		text = ", ".join(shown) if shown else "—"
		if extra > 0:
			text += _(" … and {0} more").format(extra)
		return text

	frappe.throw(
		_(
			"Cannot migrate to v4.7.2 Draft Approval: {0} PM Request(s) and {1} PM Clearance(s) "
			"are still in Pending* workflow states. Finish, return, or clear them first.\n\n"
			"PM Request ({0}): {2}\n"
			"PM Clearance ({1}): {3}"
		).format(req_n, clr_n, _sample(stats["request_names"]), _sample(stats["clearance_names"])),
		title=_("In-flight Pending documents"),
	)


def _pending_doc_statuses(workflow_name: str, titles: tuple[str, ...]) -> list[int]:
	"""doc_status values for Pending* rows on an active workflow definition."""
	if not frappe.db.exists("Workflow", workflow_name):
		return []
	states = _workflow_state_names_for_titles(titles)
	rows = frappe.get_all(
		"Workflow Document State",
		filters={"parent": workflow_name, "state": ("in", states)},
		fields=["doc_status"],
	)
	return [cint(r.doc_status) for r in rows]


def _has_return_for_correction(workflow_name: str) -> bool:
	if not frappe.db.exists("Workflow", workflow_name):
		return False
	return bool(
		frappe.db.exists(
			"Workflow Transition",
			{"parent": workflow_name, "action": "PM Return for Correction"},
		)
	)


def is_draft_approval_workflow_applied() -> bool:
	"""True when PM workflows already encode v4.7.2 draft-approval semantics.

	Checks Pending* ``doc_status=0`` and presence of Return-for-Correction
	transitions. Does not inspect or mutate business documents.
	"""
	req_statuses = _pending_doc_statuses("PM Request Workflow", REQUEST_PENDING_TITLES)
	clr_statuses = _pending_doc_statuses("PM Clearance Workflow", CLEARANCE_PENDING_TITLES)
	if not req_statuses or not clr_statuses:
		return False
	if any(s != 0 for s in req_statuses):
		return False
	if any(s != 0 for s in clr_statuses):
		return False
	if not _has_return_for_correction("PM Request Workflow"):
		return False
	if not _has_return_for_correction("PM Clearance Workflow"):
		return False
	return True


def _set_site_flag(key: str, value) -> None:
	frappe.db.set_default(key, json.dumps(value) if not isinstance(value, str) else value)


def _get_site_flag(key: str):
	raw = frappe.db.get_default(key)
	if raw in (None, ""):
		return None
	try:
		return json.loads(raw)
	except Exception:
		return raw


def _clear_site_flag(key: str) -> None:
	frappe.db.set_default(key, "")


def _apply_draft_approval_cutover(report: dict) -> dict:
	"""Rebuild workflows + seed assignment rules. Caller ensures queue safety."""
	_rebuild_pm_request_workflow()
	_rebuild_pm_clearance_workflow()
	realign_doctype_workflow_states("PM Request")
	realign_doctype_workflow_states("PM Clearance")
	report["assignment_rules"] = _seed_assignment_rules()
	# Bulk-apply while still inside the patch transaction (Frappe commits after Patch Log).
	report["bulk_apply"] = _bulk_apply_pending_assignments()
	_set_site_flag(APPLIED_FLAG_KEY, "1")
	_clear_site_flag(DEFERRED_FLAG_KEY)
	report["path"] = "applied"
	return report


def try_complete_deferred_pm_draft_approval() -> dict:
	"""after_migrate retry: apply cutover only when deferred and queue is empty.

	Never mutates PM Request / Clearance workflow_state or docstatus. Returns a
	small report for logging.
	"""
	report: dict = {"attempted": False, "applied": False, "skipped_reason": None}
	if is_draft_approval_workflow_applied():
		_set_site_flag(APPLIED_FLAG_KEY, "1")
		_clear_site_flag(DEFERRED_FLAG_KEY)
		report["skipped_reason"] = "already_applied"
		return report

	deferred = _get_site_flag(DEFERRED_FLAG_KEY)
	if not deferred:
		report["skipped_reason"] = "not_deferred"
		return report

	stats = count_in_flight_pending_pm_docs()
	if stats["request_count"] or stats["clearance_count"]:
		report["skipped_reason"] = "still_in_flight"
		report["in_flight"] = {
			"request_count": stats["request_count"],
			"clearance_count": stats["clearance_count"],
		}
		return report

	report["attempted"] = True
	frappe.flags.in_patch = True
	try:
		_apply_draft_approval_cutover(report)
		report["applied"] = True
	finally:
		frappe.flags.in_patch = False
	frappe.cache().set_value("pm_draft_approval_v472_migration_report", report)
	print(json.dumps({"pm_draft_approval_v472_after_migrate": report}, indent=2, default=str))
	return report


def execute():
	frappe.flags.in_patch = True
	report: dict = {
		"aborted": False,
		"path": None,
		"assignment_rules": [],
		"bulk_apply": {},
		"in_flight": {},
	}
	try:
		# Path B: workflow already encodes draft approval — do not require an
		# empty Pending queue and do not rebuild/realign (avoids document writes).
		if is_draft_approval_workflow_applied():
			report["path"] = "already_applied"
			report["assignment_rules"] = _seed_assignment_rules()
			_set_site_flag(APPLIED_FLAG_KEY, "1")
			_clear_site_flag(DEFERRED_FLAG_KEY)
			frappe.cache().set_value("pm_draft_approval_v472_migration_report", report)
			print(json.dumps({"pm_draft_approval_v472": report}, indent=2, default=str))
			return

		stats = count_in_flight_pending_pm_docs()
		report["in_flight"] = {
			"request_count": stats["request_count"],
			"clearance_count": stats["clearance_count"],
		}
		if stats["request_count"] or stats["clearance_count"]:
			# Defer cutover: keep old workflow, mutate no documents, allow migrate
			# to continue for unrelated schema work. after_migrate retries later.
			report["path"] = "deferred_in_flight"
			_set_site_flag(
				DEFERRED_FLAG_KEY,
				{
					"request_count": stats["request_count"],
					"clearance_count": stats["clearance_count"],
				},
			)
			msg = _(
				"PM v4.7.2 Draft Approval cutover deferred: {0} PM Request(s) and "
				"{1} PM Clearance(s) remain in Pending* states. Finish or clear them, "
				"then re-run migrate (after_migrate will apply the cutover). "
				"No PM documents or workflow definitions were changed."
			).format(stats["request_count"], stats["clearance_count"])
			print(msg)
			frappe.msgprint(msg, title=_("PM Draft Approval deferred"), indicator="orange")
			frappe.cache().set_value("pm_draft_approval_v472_migration_report", report)
			print(json.dumps({"pm_draft_approval_v472": report}, indent=2, default=str))
			return

		_apply_draft_approval_cutover(report)
	except Exception:
		report["aborted"] = True
		frappe.flags.in_patch = False
		frappe.cache().set_value("pm_draft_approval_v472_migration_report", report)
		raise
	finally:
		frappe.flags.in_patch = False

	frappe.cache().set_value("pm_draft_approval_v472_migration_report", report)
	print(json.dumps({"pm_draft_approval_v472": report}, indent=2, default=str))
