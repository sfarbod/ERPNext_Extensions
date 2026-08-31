"""PM Clearance hooks — notify linked PM Request Desk sessions for Connections refresh."""

from __future__ import annotations

import frappe
from frappe.model.document import Document


def _notify_pm_requests_for_clearance(clearance: str | Document, event: str) -> None:
	from erpnext_extensions.petty_management.services.funding_queries import (
		find_pm_requests_for_clearance,
	)
	from erpnext_extensions.petty_management.services.request_api_guard import (
		notify_pm_request_funding_updated,
	)

	for name in find_pm_requests_for_clearance(clearance):
		try:
			notify_pm_request_funding_updated(name, event)
		except Exception:
			pass


def on_pm_clearance_after_insert(doc: Document, method=None) -> None:
	_notify_pm_requests_for_clearance(doc, "on_pm_clearance_created")


def on_pm_clearance_submit(doc: Document, method=None) -> None:
	_notify_pm_requests_for_clearance(doc, "on_pm_clearance_submitted")


def on_pm_clearance_cancel(doc: Document, method=None) -> None:
	_notify_pm_requests_for_clearance(doc, "on_pm_clearance_cancelled")


def on_pm_clearance_trash(doc: Document, method=None) -> None:
	_notify_pm_requests_for_clearance(doc, "on_pm_clearance_trashed")


def on_pm_clearance_update(doc: Document, method=None) -> None:
	"""Workflow return / status transitions that do not submit or cancel."""
	if frappe.flags.in_install or frappe.flags.in_patch:
		return
	before = getattr(doc, "_doc_before_save", None)
	if not before:
		return
	watched = ("workflow_state", "docstatus", "journal_entry", "status")
	if not any(doc.get(field) != before.get(field) for field in watched):
		return
	_notify_pm_requests_for_clearance(doc, "on_pm_clearance_updated")
