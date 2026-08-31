# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.6.8 — PM Request cancel / delete eligibility (independent helpers).

Cancel = open financial process only (not reservation, not history).
Delete = history-based; must never share cancel decision logic.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from erpnext_extensions.petty_management.services.funding_queries import (
	list_payment_entries_for_pm_request,
)

# Terminal Clearance states — historical only; do not block Request cancel.
_CANCEL_CLEARANCE_TERMINAL_STATUSES = frozenset({"Cancelled", "Rejected"})

# Open Clearance processes (business/workflow status). Not reservation SQL.
_CANCEL_CLEARANCE_OPEN_STATUSES = frozenset(
	{
		"Draft",
		"Pending Approval",
		"Pending Manager Approval",  # workflow title alias → treat as open
		"Pending Finance Review",
		"Approved",
		"Pending Journal Entry Submission",
		"Settled",
	}
)


def _pm_request_name(doc: Document | str) -> str:
	if isinstance(doc, str):
		return doc
	return doc.name


def _linked_payment_entries(pm_request: str) -> list[dict]:
	"""All funding PEs (draft / submitted / cancelled) linked to this Request."""
	return list_payment_entries_for_pm_request(pm_request)


def _clearance_allocations_for_request(pm_request: str) -> list[dict]:
	"""Every Clearance allocation row pointing at this Request (any parent status)."""
	return frappe.db.sql(
		"""
		SELECT
			a.parent AS clearance,
			cl.docstatus AS clearance_docstatus,
			IFNULL(cl.status, '') AS clearance_status,
			IFNULL(cl.workflow_state, '') AS clearance_workflow_state,
			IFNULL(cl.journal_entry, '') AS clearance_journal_entry,
			a.allocated_amount
		FROM `tabPM Clearance Request Allocation` a
		INNER JOIN `tabPM Clearance` cl
			ON cl.name = a.parent AND a.parenttype = 'PM Clearance'
		WHERE a.parentfield = 'request_allocations'
			AND IFNULL(a.is_legacy_row, 0) = 0
			AND a.pm_request = %s
		ORDER BY a.parent
		""",
		(pm_request,),
		as_dict=True,
	)


def _is_open_clearance_for_cancel(row: dict) -> bool:
	"""True when Clearance is still an open financial process (blocks Request cancel)."""
	status = (row.clearance_status or "").strip()
	ds = cint(row.clearance_docstatus)
	if ds == 2 or status in _CANCEL_CLEARANCE_TERMINAL_STATUSES:
		return False
	if status in _CANCEL_CLEARANCE_OPEN_STATUSES:
		return True
	# Unknown non-terminal status with submitted/draft parent → fail closed (treat as open).
	return ds in (0, 1)


def _open_clearances_for_cancel(pm_request: str) -> list[dict]:
	return [row for row in _clearance_allocations_for_request(pm_request) if _is_open_clearance_for_cancel(row)]


def _format_names(names: list[str], limit: int = 8) -> str:
	uniq = sorted({n for n in names if n})
	if not uniq:
		return ""
	shown = uniq[:limit]
	extra = len(uniq) - len(shown)
	text = ", ".join(frappe.bold(n) for n in shown)
	if extra > 0:
		text += _(" and {0} more").format(extra)
	return text


def _clearance_status_label(status: str) -> str:
	"""Human label for messages (Pending Approval ≈ Pending Manager Approval in product copy)."""
	s = (status or "").strip() or _("Unknown")
	if s == "Pending Approval":
		return _("Pending Manager Approval")
	return s


def get_pm_request_cancel_blockers(doc: Document | str) -> list[str]:
	"""Return cancel blockers for open financial processes (empty ⇒ eligible).

	Does not check DocPerm. Does not use reservation SQL or payment_entry pointer.
	Cancelled PEs and Rejected/Cancelled Clearances are historical for Cancel only.
	"""
	name = _pm_request_name(doc)
	if isinstance(doc, str):
		row = frappe.db.get_value(
			"PM Request", name, ["docstatus", "journal_entry", "is_closed"], as_dict=True
		)
		if not row:
			return [_("PM Request {0} not found").format(name)]
		docstatus = cint(row.docstatus)
		journal_entry = row.journal_entry
		is_closed = cint(row.is_closed)
	else:
		# Frappe Document.cancel() sets in-memory docstatus=2 before before_cancel/save.
		# Authoritative eligibility uses DB docstatus (still 1 until cancel commits).
		db_row = (
			frappe.db.get_value(
				"PM Request", name, ["docstatus", "journal_entry", "is_closed"], as_dict=True
			)
			if name
			else None
		)
		if db_row:
			docstatus = cint(db_row.docstatus)
			journal_entry = getattr(doc, "journal_entry", None) or db_row.journal_entry
			is_closed = cint(db_row.is_closed)
		else:
			docstatus = cint(doc.docstatus)
			journal_entry = getattr(doc, "journal_entry", None)
			is_closed = cint(getattr(doc, "is_closed", 0))

	blockers: list[str] = []
	if docstatus != 1:
		blockers.append(
			_("Only a submitted PM Request can be cancelled (current docstatus={0}).").format(docstatus)
		)
		return blockers

	if is_closed:
		blockers.append(_("Cannot cancel: this PM Request is closed."))
		return blockers

	# --- Payment Entry: open process = Draft or Submitted (authoritative PE list) ---
	pes = _linked_payment_entries(name)
	draft_pes = [r["payment_entry"] for r in pes if (r.get("status") or "") == "Draft"]
	submitted_pes = [r["payment_entry"] for r in pes if (r.get("status") or "") == "Submitted"]
	# Cancelled PEs intentionally ignored for Cancel.

	if draft_pes:
		blockers.append(
			_("Cannot cancel: Draft Payment Entry exists: {0}.").format(_format_names(draft_pes))
		)
	if submitted_pes:
		blockers.append(
			_("Cannot cancel: Submitted Payment Entry exists: {0}.").format(_format_names(submitted_pes))
		)

	# --- Clearance: open workflow/business state (not reservation) ---
	open_clr = _open_clearances_for_cancel(name)
	by_status: dict[str, list[str]] = {}
	for row in open_clr:
		st = (row.clearance_status or "").strip() or _("Unknown")
		by_status.setdefault(st, []).append(row.clearance)
	for status, names in sorted(by_status.items(), key=lambda x: x[0]):
		label = _clearance_status_label(status)
		blockers.append(
			_("Cannot cancel: {0} Clearance exists: {1}.").format(label, _format_names(names))
		)

	# --- Request-level Journal Entry (draft or submitted blocks cancel) ---
	meta = frappe.get_meta("PM Request")
	if meta.has_field("journal_entry") and journal_entry:
		if not frappe.db.exists("Journal Entry", journal_entry):
			pass
		else:
			je_ds = cint(frappe.db.get_value("Journal Entry", journal_entry, "docstatus"))
			if je_ds == 0:
				blockers.append(
					_("Cannot cancel: draft Journal Entry {0} is linked on this Request.").format(
						frappe.bold(journal_entry)
					)
				)
			elif je_ds == 1:
				blockers.append(
					_("Cannot cancel: submitted Journal Entry {0} is linked on this Request.").format(
						frappe.bold(journal_entry)
					)
				)

	return blockers


def user_may_execute_pm_request_cancel(doc: Document | str) -> bool:
	"""v4.8.5 — business cancel permission (independent of DocPerm cancel)."""
	user = frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	if "Petty Management Accountant" in roles:
		return True
	from erpnext_extensions.petty_management.permissions import get_operational_pm_visibility_role

	if get_operational_pm_visibility_role() in roles:
		return True
	return False


def cancel_pm_request_action_flags(doc: Document) -> tuple[bool, str]:
	"""Desk visibility for Cancel PM Request (financial + role policy)."""
	from erpnext_extensions.petty_management.services.business_status_service import (
		request_is_finance_cleared,
	)

	if not user_may_execute_pm_request_cancel(doc):
		return False, ""
	if cint(getattr(doc, "docstatus", 0)) != 1:
		return False, ""
	if not request_is_finance_cleared(doc):
		return False, ""
	blockers = get_pm_request_cancel_blockers(doc)
	if blockers:
		return False, blockers[0]
	return True, ""


def user_may_execute_pm_request_delete(doc: Document | str) -> bool:
	"""v4.8.6 — administrative delete permission (Administrator only)."""
	return frappe.session.user == "Administrator"


def delete_pm_request_action_flags(doc: Document) -> tuple[bool, str]:
	"""Desk visibility for Delete PM Request (cancelled + history eligibility + role policy)."""
	if not user_may_execute_pm_request_delete(doc):
		return False, ""
	if cint(getattr(doc, "docstatus", 0)) != 2:
		return False, ""
	blockers = get_pm_request_delete_blockers(doc)
	if blockers:
		return False, blockers[0]
	return True, ""


def assert_pm_request_cancel_allowed(doc: Document | str) -> None:
	"""Throw if PM Request cancel is not allowed (open financial process remains)."""
	blockers = get_pm_request_cancel_blockers(doc)
	if blockers:
		frappe.throw("<br>".join(blockers), title=_("Cannot cancel PM Request"))


def get_pm_request_delete_blockers(doc: Document | str) -> list[str]:
	"""Return human-readable delete blockers (empty ⇒ eligible). Does not check DocPerm.

	Policy (history-based; independent of Cancel):
	- Submitted (docstatus=1): never.
	- Cancelled (docstatus=2): only if zero PE (any status) and zero Clearance allocations.
	- Draft (docstatus=0): mistaken cleanup only — same zero PE / zero Clearance rule.
	"""
	name = _pm_request_name(doc)
	if isinstance(doc, str):
		row = frappe.db.get_value("PM Request", name, ["docstatus", "journal_entry"], as_dict=True)
		if not row:
			return [_("PM Request {0} not found").format(name)]
		docstatus = cint(row.docstatus)
		journal_entry = row.journal_entry
	else:
		docstatus = cint(doc.docstatus)
		journal_entry = getattr(doc, "journal_entry", None)

	blockers: list[str] = []

	# v4.7.2: Pending* at docstatus 0 cannot be deleted
	from erpnext_extensions.petty_management.services.draft_approval_guards import (
		is_pending_approval_workflow,
	)

	if isinstance(doc, str):
		_doc_for_pending = frappe.get_doc("PM Request", name)
	else:
		_doc_for_pending = doc
	if is_pending_approval_workflow(_doc_for_pending):
		blockers.append(
			_("Cannot delete while pending approval. Use Return for Correction first.")
		)
		return blockers

	if docstatus == 1:
		blockers.append(
			_("Submitted PM Request cannot be deleted. Cancel it first (when eligible), then delete.")
		)
		return blockers

	if docstatus not in (0, 2):
		blockers.append(_("PM Request cannot be deleted (docstatus={0}).").format(docstatus))
		return blockers

	pes = _linked_payment_entries(name)
	if pes:
		# Any historical PE permanently blocks delete (including cancelled).
		parts = []
		for status in ("Submitted", "Draft", "Cancelled"):
			names = [r["payment_entry"] for r in pes if (r.get("status") or "") == status]
			if names:
				parts.append(f"{status}: {_format_names(names)}")
		blockers.append(
			_(
				"Cannot delete: accounting history exists — Payment Entry(ies) still linked "
				"({0}). Delete remains blocked even for cancelled Payment Entries."
			).format("; ".join(parts))
		)

	allocs = _clearance_allocations_for_request(name)
	if allocs:
		blockers.append(
			_(
				"Cannot delete: PM Clearance history exists: {0}. "
				"Delete remains blocked for any Clearance status (including cancelled/rejected)."
			).format(_format_names([r.clearance for r in allocs]))
		)

	meta = frappe.get_meta("PM Request")
	if meta.has_field("journal_entry") and journal_entry:
		if frappe.db.exists("Journal Entry", journal_entry):
			blockers.append(
				_("Cannot delete: Journal Entry {0} is still linked on this Request.").format(
					frappe.bold(journal_entry)
				)
			)

	return blockers


def assert_pm_request_delete_allowed(doc: Document | str) -> None:
	"""Throw if PM Request delete/trash is not allowed (v4.6.8)."""
	blockers = get_pm_request_delete_blockers(doc)
	if blockers:
		frappe.throw("<br>".join(blockers), title=_("Cannot delete PM Request"))


def apply_cancelled_business_status(doc: Document) -> None:
	"""Set business status=Cancelled after cancel without touching workflow_state."""
	from erpnext_extensions.petty_management.services.business_status_service import (
		REQ_CANCELLED,
		sync_pm_request_business_status,
	)

	doc.docstatus = 2
	status = sync_pm_request_business_status(doc)
	if status != REQ_CANCELLED:
		doc.status = REQ_CANCELLED
		status = REQ_CANCELLED
	if getattr(doc, "name", None):
		frappe.db.set_value("PM Request", doc.name, "status", status, update_modified=False)
