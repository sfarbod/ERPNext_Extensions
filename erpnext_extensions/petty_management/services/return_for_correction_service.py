# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.2 — PM Return for Correction (atomic same-doc Pending* → Draft)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from erpnext_extensions.petty_management.services.business_status_service import (
	CLEARANCE_PENDING_WORKFLOW_TITLES,
	REQUEST_PENDING_WORKFLOW_TITLES,
)

PM_RETURN_FOR_CORRECTION = "PM Return for Correction"

REQUEST_STAMP_FIELDS = ("manager_approver", "ceo_approver", "finance_approver")
CLEARANCE_STAMP_FIELDS = ("manager_approver", "finance_approver")

RETURN_TIMELINE_MARKER = "Returned for correction by"


def _workflow_title_from_link(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def pending_titles_for_doctype(doctype: str) -> frozenset[str]:
	if doctype == "PM Request":
		return frozenset(REQUEST_PENDING_WORKFLOW_TITLES)
	if doctype == "PM Clearance":
		return frozenset(CLEARANCE_PENDING_WORKFLOW_TITLES)
	return frozenset()


def lock_pm_document_for_return(doctype: str, name: str) -> dict:
	"""Row-lock the document and return current workflow_state / docstatus.

	Must run before Return transition so concurrent callers serialize.
	"""
	if doctype not in ("PM Request", "PM Clearance") or not name:
		frappe.throw(_("Invalid document for Return for Correction"), title=_("Return for Correction"))
	# Identifier is a fixed doctype name from our allow-list only.
	rows = frappe.db.sql(
		f"""
		SELECT name, workflow_state, docstatus, manager_approver, owner
		FROM `tab{doctype}`
		WHERE name = %s
		FOR UPDATE
		""",
		(name,),
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("{0} {1} not found").format(doctype, name), frappe.DoesNotExistError)
	return rows[0]


def assert_return_allowed_under_lock(doctype: str, row: dict) -> str:
	"""Re-check Pending* under lock. Returns workflow title.

	If already Draft, fail with a clean business message (idempotent second caller).
	"""
	title = _workflow_title_from_link(row.get("workflow_state"))
	if title == "Draft":
		frappe.throw(
			_("{0} {1} is already Draft (already returned for correction or never submitted).").format(
				doctype, row.get("name")
			),
			title=_("Already Draft"),
		)
	pending = pending_titles_for_doctype(doctype)
	if title not in pending:
		frappe.throw(
			_("Return for Correction is only allowed from Pending approval states (current: {0}).").format(
				title or _("(blank)")
			),
			title=_("Return for Correction"),
		)
	if cint(row.get("docstatus")) != 0:
		frappe.throw(
			_("Return for Correction requires docstatus 0 (current docstatus={0}).").format(
				row.get("docstatus")
			),
			title=_("Return for Correction"),
		)
	return title


def clear_approver_stamps(doc: Document) -> None:
	"""Clear named approver stamps after return to Draft. Failures must propagate."""
	fields = REQUEST_STAMP_FIELDS if doc.doctype == "PM Request" else CLEARANCE_STAMP_FIELDS
	values = {f: None for f in fields if hasattr(doc, f) or frappe.get_meta(doc.doctype).has_field(f)}
	for field in values:
		setattr(doc, field, None)
	if getattr(doc, "name", None) and values:
		frappe.db.set_value(doc.doctype, doc.name, values, update_modified=False)


def close_todos_for_doc(doctype: str, name: str) -> int:
	"""Close open ToDos linked to this document. Failures must propagate."""
	if not name:
		return 0
	open_todos = frappe.get_all(
		"ToDo",
		filters={"reference_type": doctype, "reference_name": name, "status": "Open"},
		pluck="name",
	)
	for todo_name in open_todos:
		frappe.db.set_value("ToDo", todo_name, "status", "Closed", update_modified=False)
	return len(open_todos)


def close_open_workflow_actions(doctype: str, name: str) -> int:
	"""Complete open Workflow Actions for this document (if DocType exists)."""
	if not name or not frappe.db.has_table("Workflow Action"):
		return 0
	meta = frappe.get_meta("Workflow Action")
	status_field = "status" if meta.has_field("status") else None
	filters: dict = {"reference_doctype": doctype, "reference_name": name}
	if status_field:
		filters["status"] = "Open"
	names = frappe.get_all("Workflow Action", filters=filters, pluck="name")
	closed = 0
	for wa_name in names:
		if status_field:
			frappe.db.set_value("Workflow Action", wa_name, "status", "Completed", update_modified=False)
		else:
			frappe.delete_doc("Workflow Action", wa_name, ignore_permissions=True, force=True)
		closed += 1
	return closed


def assign_requester(doc: Document) -> None:
	"""Assign the document owner (requester). Mandatory — never swallow failures.

	Administrator/Guest-owned documents skip assignment (no human requester) without
	failing; real Users must be assignable or the Return aborts.
	"""
	owner = (getattr(doc, "owner", None) or "").strip()
	if not owner or owner in ("Administrator", "Guest"):
		return
	if not frappe.db.exists("User", owner):
		frappe.throw(
			_("Cannot assign requester: User {0} does not exist.").format(owner),
			title=_("Return for Correction"),
		)
	from frappe.desk.form import assign_to

	assign_to.add(
		{
			"assign_to": [owner],
			"doctype": doc.doctype,
			"name": doc.name,
			"description": _("Returned for correction: {0}").format(doc.name),
			"notify": 0,
		}
	)


def count_return_timeline_comments(doctype: str, name: str) -> int:
	"""Count Info comments that look like Return-for-Correction markers."""
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": doctype,
			"reference_name": name,
			"comment_type": "Info",
		},
		pluck="content",
	)
	n = 0
	for content in rows:
		if RETURN_TIMELINE_MARKER in (content or "") or "Returned for correction by" in (content or ""):
			n += 1
	return n


def add_return_timeline_comment(
	doc: Document,
	*,
	from_state: str | None = None,
	reason: str | None = None,
) -> None:
	"""Timeline: who returned, previous stage, timestamp, optional reason. Failures propagate."""
	from frappe.utils import now_datetime

	# Idempotency under lock: refuse a second Return marker if one already exists
	# after a successful prior Return (should not happen if state guard works).
	if count_return_timeline_comments(doc.doctype, doc.name) > 0 and _workflow_title_from_link(
		doc.workflow_state
	) == "Draft":
		frappe.throw(
			_("Return for Correction timeline already recorded for {0}.").format(doc.name),
			title=_("Already returned"),
		)

	user = frappe.session.user
	full_name = frappe.db.get_value("User", user, "full_name") or user
	when = now_datetime()
	lines = [
		_("Returned for correction by {0}").format(full_name),
		_("Previous workflow stage: {0}").format(from_state or _("(unknown)")),
		_("Timestamp: {0}").format(when),
	]
	reason_s = (reason or getattr(frappe.flags, "pm_return_reason", None) or "").strip()
	if reason_s:
		lines.append(_("Reason: {0}").format(reason_s))
	doc.add_comment("Info", "<br>".join(lines))


def sync_draft_business_status(doc: Document) -> None:
	"""Persist business status Draft after Return. Failures propagate."""
	if doc.doctype == "PM Request":
		from erpnext_extensions.petty_management.services.business_status_service import (
			REQ_DRAFT,
			sync_pm_request_business_status,
		)

		status = sync_pm_request_business_status(doc)
		if status != REQ_DRAFT and cint(doc.docstatus) == 0:
			status = REQ_DRAFT
		frappe.db.set_value(doc.doctype, doc.name, "status", status, update_modified=False)
		doc.status = status
	else:
		from erpnext_extensions.petty_management.services.business_status_service import (
			CLR_DRAFT,
			sync_pm_clearance_business_status,
		)

		lifecycle = sync_pm_clearance_business_status(doc, persist=False)
		if lifecycle != CLR_DRAFT and cint(doc.docstatus) == 0:
			lifecycle = CLR_DRAFT
		frappe.db.set_value(doc.doctype, doc.name, "status", lifecycle, update_modified=False)
		doc.status = lifecycle


def handle_return_for_correction(
	doc: Document,
	*,
	from_state: str | None = None,
	reason: str | None = None,
) -> Document:
	"""Post-transition Return side effects. All steps mandatory (except Admin owner assign skip).

	Caller must already have applied workflow Pending*→Draft in the same DB transaction.
	Any exception here aborts the request transaction so the document stays Pending*.
	"""
	if doc.doctype not in ("PM Request", "PM Clearance"):
		return doc
	doc.reload()
	close_todos_for_doc(doc.doctype, doc.name)
	close_open_workflow_actions(doc.doctype, doc.name)
	assign_requester(doc)
	add_return_timeline_comment(doc, from_state=from_state, reason=reason)
	clear_approver_stamps(doc)
	sync_draft_business_status(doc)
	doc.reload()
	return doc
