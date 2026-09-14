"""Journal Entry hooks for Petty Management settlement linkage.

Lifecycle distinction (v5.2.10):

* **CASE A** — user cancels the PM Clearance document itself → clearance ``docstatus=2``.
* **CASE B** — user cancels/deletes only the settlement Journal Entry → clearance stays
  submitted/Approved; settlement link is cleared so Settle can recreate a JE.

These hooks implement CASE B only. They must **never** call ``PM Clearance.cancel()``
or set clearance ``docstatus`` / ``status=Cancelled``.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint


def on_journal_entry_submit(doc, method=None):
	"""When a settlement JE is submitted, mark linked PM Clearance as Settled."""
	from erpnext_extensions.petty_management import petty_audit

	names = _clearance_names_for_settlement_je(doc)
	from erpnext_extensions.petty_management.services.clearance_action_policy import sync_clearance_lifecycle

	for cl_name in names:
		cl = frappe.get_doc("PM Clearance", cl_name)
		# Ensure journal_entry field points at this JE when linked only via custom field.
		if (cl.journal_entry or "").strip() != doc.name:
			frappe.db.set_value(
				"PM Clearance",
				cl_name,
				{"journal_entry": doc.name},
				update_modified=False,
			)
			cl.journal_entry = doc.name
		sync_clearance_lifecycle(cl, persist=True)
	for cl_name in names:
		try:
			row = frappe.db.get_value(
				"PM Clearance",
				cl_name,
				["holder", "employee", "company"],
				as_dict=True,
			)
			petty_audit.log_event(
				"pm_journal_entry_submitted",
				pm_clearance=cl_name,
				journal_entry=doc.name,
				holder=row.get("holder") if row else None,
				employee=row.get("employee") if row else None,
				company=row.get("company") if row else None,
			)
		except Exception:
			pass
	_notify_pm_requests_for_journal_entry(doc.name, "on_journal_entry_submitted")


def _notify_pm_requests_for_journal_entry(je_name: str, event: str) -> None:
	from erpnext_extensions.petty_management.services.funding_queries import (
		find_pm_requests_for_journal_entry,
	)
	from erpnext_extensions.petty_management.services.request_api_guard import (
		notify_pm_request_funding_updated,
	)

	for name in find_pm_requests_for_journal_entry(je_name):
		try:
			notify_pm_request_funding_updated(name, event)
		except Exception:
			pass


def _clearance_names_for_settlement_je(doc) -> list[str]:
	"""Clearances linked by ``journal_entry`` field and/or ``custom_pm_clearance``."""
	names: set[str] = set(
		frappe.get_all(
			"PM Clearance",
			filters={"journal_entry": doc.name},
			pluck="name",
		)
	)
	custom = ""
	if hasattr(doc, "get"):
		custom = (doc.get("custom_pm_clearance") or "").strip()
	else:
		custom = (getattr(doc, "custom_pm_clearance", None) or "").strip()
	if not custom and frappe.get_meta("Journal Entry").has_field("custom_pm_clearance"):
		custom = (frappe.db.get_value("Journal Entry", doc.name, "custom_pm_clearance") or "").strip()
	if custom and frappe.db.exists("PM Clearance", custom):
		names.add(custom)
	return sorted(names)


def unlink_settlement_je_from_clearances(je_name: str, *, event: str) -> None:
	"""CASE B: detach settlement JE from clearances without cancelling them.

	Clears ``journal_entry`` / generated detail links and resyncs business status from
	approval workflow (typically back to Approved). Never mutates clearance ``docstatus``.
	"""
	from erpnext_extensions.petty_management import petty_audit
	from erpnext_extensions.petty_management.services.clearance_action_policy import (
		sync_clearance_lifecycle,
	)

	je_name = (je_name or "").strip()
	if not je_name:
		return

	for row_name in frappe.get_all(
		"PM Clearance Detail",
		filters={"generated_doctype": "Journal Entry", "generated_document": je_name},
		pluck="name",
	):
		frappe.db.set_value(
			"PM Clearance Detail",
			row_name,
			{"generated_doctype": None, "generated_document": None},
			update_modified=False,
		)

	# Build a lightweight stand-in so name-based lookup works even after trash.
	stand_in = frappe._dict(name=je_name)
	if frappe.get_meta("Journal Entry").has_field("custom_pm_clearance"):
		stand_in.custom_pm_clearance = frappe.db.get_value(
			"Journal Entry", je_name, "custom_pm_clearance"
		) if frappe.db.exists("Journal Entry", je_name) else None
		# On trash, row may already be gone — also search by historical custom field via SQL if needed.
		if not stand_in.custom_pm_clearance:
			# Fallback: clearances that still point at this JE name.
			pass

	names = set(
		frappe.get_all(
			"PM Clearance",
			filters={"journal_entry": je_name},
			pluck="name",
		)
	)
	if frappe.get_meta("Journal Entry").has_field("custom_pm_clearance") and frappe.db.exists(
		"Journal Entry", je_name
	):
		custom = (frappe.db.get_value("Journal Entry", je_name, "custom_pm_clearance") or "").strip()
		if custom:
			names.add(custom)

	for cl_name in sorted(names):
		# Guard: never cascade-cancel.
		cl_ds = cint(frappe.db.get_value("PM Clearance", cl_name, "docstatus"))
		try:
			row = frappe.db.get_value(
				"PM Clearance",
				cl_name,
				["holder", "employee", "company", "docstatus", "workflow_state", "status"],
				as_dict=True,
			)
			petty_audit.log_event(
				event,
				pm_clearance=cl_name,
				journal_entry=je_name,
				holder=row.get("holder") if row else None,
				employee=row.get("employee") if row else None,
				company=row.get("company") if row else None,
				clearance_docstatus=cl_ds,
			)
		except Exception:
			pass

		frappe.db.set_value(
			"PM Clearance",
			cl_name,
			{"journal_entry": None},
			update_modified=False,
		)
		if cl_ds == 2:
			# Clearance already cancelled (CASE A) — only clear link; do not revive.
			continue
		cl = frappe.get_doc("PM Clearance", cl_name)
		cl.journal_entry = None
		sync_clearance_lifecycle(cl, persist=True)

	_notify_pm_requests_for_journal_entry(je_name, event)


def on_journal_entry_before_cancel(doc, method=None):
	"""CASE B: JE cancel → unlink from clearance; clearance stays Approved/submitted."""
	unlink_settlement_je_from_clearances(doc.name, event="pm_journal_entry_cancelled")


def on_journal_entry_on_trash(doc, method=None):
	"""CASE B: draft or cancelled JE delete → same unlink/heal as cancel.

	Draft JEs are deleted (not cancelled); without this hook the clearance would keep a
	stale ``journal_entry`` and Settle would stay hidden.
	"""
	unlink_settlement_je_from_clearances(doc.name, event="pm_journal_entry_trashed")
