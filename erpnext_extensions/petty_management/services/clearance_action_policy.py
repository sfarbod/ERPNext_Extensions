from __future__ import annotations

"""PM Clearance lifecycle, action matrix, and accounting lock (single source of truth)."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

# Business lifecycle values stored on ``PM Clearance.status``.
LIFECYCLE_DRAFT = "Draft"
LIFECYCLE_PENDING_REVIEW = "Pending Approval"  # v4.0.2 business label; workflow may be Manager/Finance
LIFECYCLE_APPROVED = "Approved"
LIFECYCLE_PENDING_JE = "Pending Journal Entry Submission"
LIFECYCLE_SETTLED = "Settled"
LIFECYCLE_REJECTED = "Rejected"
LIFECYCLE_CANCELLED = "Cancelled"

TERMINAL_LIFECYCLE = frozenset({LIFECYCLE_REJECTED, LIFECYCLE_CANCELLED})


def workflow_state_title(ws_link: str | None) -> str:
	if not ws_link:
		return ""
	return (frappe.db.get_value("Workflow State", ws_link, "workflow_state_name") or ws_link or "").strip()


def journal_entry_docstatus(journal_entry: str | None) -> int | None:
	if not journal_entry or not frappe.db.exists("Journal Entry", journal_entry):
		return None
	return cint(frappe.db.get_value("Journal Entry", journal_entry, "docstatus"))


def is_accounting_locked(doc: Document | str) -> bool:
	"""Submitted settlement JE exists — workflow reject/rollback and clearance cancel are blocked."""
	if isinstance(doc, str):
		doc = frappe.get_doc("PM Clearance", doc)
	je = (getattr(doc, "journal_entry", None) or "").strip()
	return journal_entry_docstatus(je) == 1


def has_active_settlement_je(doc: Document) -> bool:
	"""Any linked JE that is not cancelled (draft or submitted)."""
	je = (getattr(doc, "journal_entry", None) or "").strip()
	ds = journal_entry_docstatus(je)
	return ds is not None and ds in (0, 1)


def ensure_workflow_state_record(lifecycle: str) -> str | None:
	"""Ensure a Workflow State row exists for lifecycle title; return link name."""
	title = (lifecycle or "").strip()
	if not title:
		return None
	name = frappe.db.get_value("Workflow State", {"workflow_state_name": title}, "name")
	if name:
		return name
	if frappe.db.exists("Workflow State", title):
		return title
	doc = frappe.new_doc("Workflow State")
	doc.workflow_state_name = title
	doc.insert(ignore_permissions=True)
	return doc.name


def workflow_state_link_for_lifecycle(lifecycle: str) -> str | None:
	return ensure_workflow_state_record(lifecycle)


def lifecycle_from_workflow(doc: Document) -> str:
	ws = (getattr(doc, "workflow_state", None) or "").strip()
	if not ws:
		return LIFECYCLE_DRAFT
	title = workflow_state_title(ws)
	mapping = {
		"Draft": LIFECYCLE_DRAFT,
		"Pending Finance Review": LIFECYCLE_PENDING_REVIEW,
		"Approved": LIFECYCLE_APPROVED,
		"Rejected": LIFECYCLE_REJECTED,
		"Pending Journal Entry Submission": LIFECYCLE_PENDING_JE,
		"Settled": LIFECYCLE_SETTLED,
		"Cancelled": LIFECYCLE_CANCELLED,
	}
	return mapping.get(title, title or LIFECYCLE_DRAFT)


def compute_lifecycle_status(doc: Document) -> str:
	"""Derive business lifecycle from JE then approval workflow (status only)."""
	from erpnext_extensions.petty_management.services.business_status_service import (
		sync_pm_clearance_business_status,
	)

	# Compute without requiring persist; uses same rules as sync.
	prev = getattr(doc, "status", None)
	lifecycle = sync_pm_clearance_business_status(doc, persist=False)
	# Keep in-memory status consistent for callers that only compute.
	doc.status = lifecycle or prev
	return lifecycle


def sync_clearance_lifecycle(doc: Document, *, persist: bool = False) -> str:
	"""Set ``status`` from JE + approval workflow. Never writes ``workflow_state`` (v4.0.2)."""
	from erpnext_extensions.petty_management.services.business_status_service import (
		sync_pm_clearance_business_status,
	)

	return sync_pm_clearance_business_status(doc, persist=persist)


def sync_clearance_lifecycle_if_stale(doc: Document) -> str:
	"""Persist ``status`` when DB disagrees with accounting/approval-derived lifecycle."""
	lifecycle = sync_clearance_lifecycle(doc, persist=False)
	if not getattr(doc, "name", None):
		return lifecycle
	# New documents: name is set in autoname but row is not inserted yet — never UPDATE here.
	if doc.get("__islocal") or getattr(getattr(doc, "flags", None), "in_insert", False):
		return lifecycle

	stored_status = (frappe.db.get_value("PM Clearance", doc.name, "status") or "").strip()
	if stored_status != lifecycle:
		sync_clearance_lifecycle(doc, persist=True)
	return lifecycle


# Backward-compatible alias used across services/tests
def sync_clearance_status_from_workflow(doc: Document) -> None:
	"""Refresh business status only (does not write workflow_state)."""
	sync_clearance_lifecycle(doc, persist=False)


def _pm_clearance_workflow_defines_reject(doc: Document) -> bool:
	"""True when PM Reject is defined from the clearance's current workflow state."""
	from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link

	ws = (getattr(doc, "workflow_state", None) or "").strip()
	if not ws:
		return False
	canonical_ws = resolve_workflow_state_link(ws) or ws
	ws_title = workflow_state_title(ws)

	wf_name = frappe.db.get_value("Workflow", {"document_type": "PM Clearance", "is_active": 1}, "name")
	if not wf_name:
		return False
	for row in frappe.get_all(
		"Workflow Transition",
		filters={"parent": wf_name, "action": "PM Reject"},
		fields=["state"],
	):
		state_link = (row.get("state") or "").strip()
		if not state_link:
			continue
		canonical_state = resolve_workflow_state_link(state_link) or state_link
		if canonical_state == canonical_ws or state_link == ws:
			return True
		if ws_title and workflow_state_title(state_link) == ws_title:
			return True
	return False


def get_pm_clearance_action_flags(pm_clearance: str | Document) -> dict:
	doc = frappe.get_doc("PM Clearance", pm_clearance) if isinstance(pm_clearance, str) else pm_clearance
	if not frappe.has_permission("PM Clearance", "read", doc=doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	lifecycle = compute_lifecycle_status(doc)
	locked = is_accounting_locked(doc)
	je = (doc.journal_entry or "").strip()
	je_ds = journal_entry_docstatus(je)

	terminal = lifecycle in TERMINAL_LIFECYCLE or cint(doc.docstatus) == 2
	approved = lifecycle == LIFECYCLE_APPROVED or clearance_is_approved_for_actions(doc, lifecycle)
	submitted_doc = cint(doc.docstatus) == 1

	can_preview = bool(doc.name) and cint(doc.docstatus) in (0, 1) and not terminal
	can_settle = (
		submitted_doc
		and approved
		and not je
		and not terminal
		and lifecycle not in (LIFECYCLE_SETTLED, LIFECYCLE_PENDING_JE)
	)
	can_open_je = bool(je and frappe.db.exists("Journal Entry", je))
	# v4.7.2: Return/Reject available while Pending* at docstatus 0
	can_reject = (
		cint(doc.docstatus) in (0, 1)
		and not locked
		and not has_active_settlement_je(doc)
		and lifecycle
		not in (LIFECYCLE_SETTLED, LIFECYCLE_PENDING_JE, LIFECYCLE_REJECTED, LIFECYCLE_CANCELLED)
	)
	from erpnext_extensions.petty_management.services.workflow_utils import get_allowed_workflow_actions

	wf_actions = [t.get("action") for t in get_allowed_workflow_actions(doc) if t.get("action")]
	has_reject_transition = (
		lifecycle == LIFECYCLE_APPROVED
		or "PM Reject" in wf_actions
		or "PM Return for Correction" in wf_actions
		or _pm_clearance_workflow_defines_reject(doc)
	)
	if can_reject and not has_reject_transition:
		can_reject = False
	can_cancel = cint(doc.docstatus) == 1 and not locked and lifecycle != LIFECYCLE_CANCELLED

	pi_ready = True
	pi_readiness_message = ""
	try:
		from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
			get_purchase_invoice_readiness,
		)

		readiness = get_purchase_invoice_readiness(doc)
		pi_ready = bool(readiness.get("ready"))
		pi_readiness_message = readiness.get("message") or ""
	except Exception:
		pi_ready = True

	return {
		"can_preview": can_preview and pi_ready,
		"can_settle": can_settle and pi_ready,
		"can_reject": can_reject,
		"can_cancel": can_cancel,
		"can_open_je": can_open_je,
		"accounting_locked": locked,
		"lifecycle_state": lifecycle,
		"journal_entry": je,
		"journal_entry_docstatus": je_ds,
		"workflow_state": doc.workflow_state,
		"workflow_state_title": workflow_state_title(getattr(doc, "workflow_state", None)),
		"allowed_workflow_actions": wf_actions,
		"docstatus": cint(doc.docstatus),
		"pi_ready": pi_ready,
		"pi_readiness_message": pi_readiness_message,
	}


def clearance_is_approved_for_actions(doc: Document, lifecycle: str) -> bool:
	"""Settle eligibility uses business ``status`` (Approved), not accounting workflow titles."""
	if lifecycle == LIFECYCLE_APPROVED:
		return True
	from erpnext_extensions.petty_management.services.business_status_service import (
		clearance_is_finance_approved,
	)

	return clearance_is_finance_approved(doc)


def validate_pm_clearance_workflow_change(doc: Document) -> None:
	"""Block invalid workflow transitions (including API / Workflow Action)."""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if not before:
		return

	old_ws = before.get("workflow_state")
	new_ws = doc.get("workflow_state")
	if old_ws == new_ws:
		return

	new_title = workflow_state_title(new_ws)

	if is_accounting_locked(doc):
		frappe.throw(
			_(
				"Workflow cannot change while settlement Journal Entry {0} is submitted. "
				"Cancel the Journal Entry through accounting first."
			).format(doc.journal_entry),
			title=_("Accounting locked"),
		)

	if new_title == "Rejected" and has_active_settlement_je(doc):
		frappe.throw(
			_(
				"Cannot reject PM Clearance while a settlement Journal Entry exists. Cancel the Journal Entry first."
			),
			title=_("Reject not allowed"),
		)


def validate_apply_workflow_action(doc: Document, action: str) -> None:
	"""Called before workflow action is applied (via hook)."""
	action = (action or "").strip()
	if action == "PM Reject":
		if is_accounting_locked(doc):
			frappe.throw(
				_("Cannot reject after settlement Journal Entry is submitted."),
				title=_("Accounting locked"),
			)
		if has_active_settlement_je(doc):
			frappe.throw(
				_(
					"Cannot reject while a settlement Journal Entry is linked. Cancel the Journal Entry first."
				),
				title=_("Reject not allowed"),
			)
	from erpnext_extensions.petty_management.services.clearance_finance_review import (
		CLEARANCE_FINANCE_WORKFLOW_ACTIONS,
		validate_clearance_finance_workflow_action,
	)

	if action in CLEARANCE_FINANCE_WORKFLOW_ACTIONS:
		validate_clearance_finance_workflow_action(doc, action)
	if action in ("PM Finance Approve", "PM Approve"):
		from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
			validate_purchase_invoices_for_finance_approval,
		)

		validate_purchase_invoices_for_finance_approval(doc)
