# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.4 — Rewind legacy Pending* PM docs from docstatus 1 → 0 (same workflow_state)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
	CLEARANCE_PENDING_TITLES,
	REQUEST_PENDING_TITLES,
	_wf,
)
from erpnext_extensions.petty_management.services.business_status_service import (
	CLEARANCE_PENDING_WORKFLOW_TITLES,
	REQUEST_FINANCE_CLEARED_WORKFLOW_TITLES,
	REQUEST_PENDING_WORKFLOW_TITLES,
	clearance_is_finance_approved,
	request_is_finance_cleared,
	sync_pm_clearance_business_status,
	sync_pm_request_business_status,
)

V484_APPLIED_FLAG_KEY = "pm_legacy_pending_lifecycle_v484_applied"


def _workflow_state_names_for_titles(titles: tuple[str, ...]) -> list[str]:
	names: list[str] = []
	for title in titles:
		link = _wf(title)
		if link and link not in names:
			names.append(link)
		if title not in names:
			names.append(title)
	return names


def _workflow_title_from_link(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def find_legacy_pending_submitted_docs() -> dict:
	"""Pending* PM docs still at docstatus=1 (pre-v4.7.2 submitted Pending lifecycle)."""
	req_states = _workflow_state_names_for_titles(REQUEST_PENDING_TITLES)
	clr_states = _workflow_state_names_for_titles(CLEARANCE_PENDING_TITLES)

	req_names: list[str] = []
	clr_names: list[str] = []
	if frappe.db.has_table("PM Request"):
		req_names = frappe.get_all(
			"PM Request",
			filters={"workflow_state": ("in", req_states), "docstatus": 1},
			pluck="name",
			order_by="name asc",
		)
	if frappe.db.has_table("PM Clearance"):
		clr_names = frappe.get_all(
			"PM Clearance",
			filters={"workflow_state": ("in", clr_states), "docstatus": 1},
			pluck="name",
			order_by="name asc",
		)
	return {
		"request_count": len(req_names),
		"clearance_count": len(clr_names),
		"request_names": req_names,
		"clearance_names": clr_names,
	}


def _assert_no_submitted_payment_entries(pm_request: str) -> None:
	from erpnext_extensions.petty_management.services.funding_queries import (
		count_payment_entries_for_pm_request,
	)

	counts = count_payment_entries_for_pm_request(pm_request)
	if counts.get("submitted_payment_entry_count", 0) > 0:
		frappe.throw(
			_(
				"PM Request {0} has submitted Payment Entry(ies) and cannot be rewound "
				"to draft Pending lifecycle."
			).format(pm_request),
			title=_("Legacy Pending migration"),
		)


def validate_legacy_pending_doc_can_rewind(doc: Document) -> None:
	"""Abort migration when a Pending* doc has finance artifacts or wrong shape."""
	if cint(doc.docstatus) != 1:
		return

	title = _workflow_title_from_link(doc.workflow_state)
	if doc.doctype == "PM Request":
		if title not in REQUEST_PENDING_WORKFLOW_TITLES:
			frappe.throw(
				_("PM Request {0} is not in a Pending* workflow state.").format(doc.name),
				title=_("Legacy Pending migration"),
			)
		if title in REQUEST_FINANCE_CLEARED_WORKFLOW_TITLES or request_is_finance_cleared(doc):
			frappe.throw(
				_("PM Request {0} is finance-cleared and cannot be rewound.").format(doc.name),
				title=_("Legacy Pending migration"),
			)
		ps = (getattr(doc, "payment_status", None) or "").strip()
		if ps in ("Paid", "Partially Paid"):
			frappe.throw(
				_("PM Request {0} has payment_status={1} and cannot be rewound.").format(doc.name, ps),
				title=_("Legacy Pending migration"),
			)
		_assert_no_submitted_payment_entries(doc.name)
		return

	if doc.doctype == "PM Clearance":
		if title not in CLEARANCE_PENDING_WORKFLOW_TITLES:
			frappe.throw(
				_("PM Clearance {0} is not in a Pending* workflow state.").format(doc.name),
				title=_("Legacy Pending migration"),
			)
		if title == "Approved" or clearance_is_finance_approved(doc):
			frappe.throw(
				_("PM Clearance {0} is finance-approved and cannot be rewound.").format(doc.name),
				title=_("Legacy Pending migration"),
			)
		je = (getattr(doc, "journal_entry", None) or "").strip()
		if je and frappe.db.exists("Journal Entry", je):
			je_ds = cint(frappe.db.get_value("Journal Entry", je, "docstatus"))
			if je_ds == 1:
				frappe.throw(
					_("PM Clearance {0} has submitted Journal Entry {1} and cannot be rewound.").format(
						doc.name, je
					),
					title=_("Legacy Pending migration"),
				)
		return

	frappe.throw(_("Unsupported doctype {0} for legacy Pending migration.").format(doc.doctype))


def _snapshot_preservation_fields(doc: Document) -> dict:
	return {
		"name": doc.name,
		"workflow_state": doc.workflow_state,
		"owner": doc.owner,
		"manager_approver": getattr(doc, "manager_approver", None),
		"ceo_approver": getattr(doc, "ceo_approver", None),
		"finance_approver": getattr(doc, "finance_approver", None),
		"comment_count": frappe.db.count(
			"Comment",
			{"reference_doctype": doc.doctype, "reference_name": doc.name},
		),
		"version_count": frappe.db.count(
			"Version", {"ref_doctype": doc.doctype, "docname": doc.name}
		),
		"open_todo_count": frappe.db.count(
			"ToDo",
			{
				"reference_type": doc.doctype,
				"reference_name": doc.name,
				"status": "Open",
			},
		),
	}


def convert_legacy_pending_doc_to_draft_lifecycle(doc: Document) -> dict:
	"""Rewind docstatus 1→0 for a Pending* doc; workflow_state and identity unchanged."""
	validate_legacy_pending_doc_can_rewind(doc)
	before = _snapshot_preservation_fields(doc)
	workflow_state = doc.workflow_state
	wf_title = _workflow_title_from_link(workflow_state)

	frappe.db.set_value(doc.doctype, doc.name, "docstatus", 0, update_modified=False)
	doc.docstatus = 0

	if doc.doctype == "PM Request":
		sync_pm_request_business_status(doc)
		frappe.db.set_value(doc.doctype, doc.name, "status", doc.status, update_modified=False)
	else:
		sync_pm_clearance_business_status(doc, persist=True)

	doc.reload()
	after = _snapshot_preservation_fields(doc)

	if after["name"] != before["name"]:
		frappe.throw(_("Document identity changed during legacy Pending migration."))
	if after["workflow_state"] != before["workflow_state"]:
		frappe.throw(
			_("Workflow state changed during legacy Pending migration for {0}.").format(doc.name)
		)
	if cint(doc.docstatus) != 0:
		frappe.throw(
			_("Expected docstatus 0 after legacy Pending migration for {0}.").format(doc.name)
		)

	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"workflow_title": wf_title,
		"before": before,
		"after": after,
		"status": doc.status,
	}


def migrate_all_legacy_pending_submitted_docs() -> dict:
	"""Convert every legacy Pending* docstatus=1 document; all-or-nothing via patch txn."""
	stats = find_legacy_pending_submitted_docs()
	report: dict = {
		"converted_requests": [],
		"converted_clearances": [],
		"request_count": stats["request_count"],
		"clearance_count": stats["clearance_count"],
	}

	for name in stats["request_names"]:
		doc = frappe.get_doc("PM Request", name)
		row = convert_legacy_pending_doc_to_draft_lifecycle(doc)
		report["converted_requests"].append(row)

	for name in stats["clearance_names"]:
		doc = frappe.get_doc("PM Clearance", name)
		row = convert_legacy_pending_doc_to_draft_lifecycle(doc)
		report["converted_clearances"].append(row)

	return report


def assert_post_migration_lifecycle_invariants(*, converted_names: dict | None = None) -> None:
	"""Verify no Pending* remain at docstatus=1 after legacy rewind."""
	legacy = find_legacy_pending_submitted_docs()
	if legacy["request_count"] or legacy["clearance_count"]:
		frappe.throw(
			_(
				"Post-migration check failed: {0} Request(s) and {1} Clearance(s) "
				"remain Pending* at docstatus=1."
			).format(legacy["request_count"], legacy["clearance_count"]),
			title=_("Legacy Pending migration"),
		)

	if not converted_names:
		return

	for name in converted_names.get("requests") or []:
		ds = cint(frappe.db.get_value("PM Request", name, "docstatus"))
		if ds != 0:
			frappe.throw(
				_("Converted PM Request {0} must be docstatus=0 (found {1}).").format(name, ds),
				title=_("Legacy Pending migration"),
			)
	for name in converted_names.get("clearances") or []:
		ds = cint(frappe.db.get_value("PM Clearance", name, "docstatus"))
		if ds != 0:
			frappe.throw(
				_("Converted PM Clearance {0} must be docstatus=0 (found {1}).").format(name, ds),
				title=_("Legacy Pending migration"),
			)
