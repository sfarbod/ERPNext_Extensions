"""Journal Entry hooks for Petty Management settlement linkage."""

from __future__ import annotations

import frappe


def on_journal_entry_submit(doc, method=None):
	"""When a settlement JE is submitted, mark linked PM Clearance as Settled."""
	from erpnext_extensions.petty_management import petty_audit

	names = frappe.get_all(
		"PM Clearance",
		filters={"journal_entry": doc.name},
		pluck="name",
	)
	from erpnext_extensions.petty_management.services.clearance_action_policy import sync_clearance_lifecycle

	for cl_name in names:
		cl = frappe.get_doc("PM Clearance", cl_name)
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


def on_journal_entry_before_cancel(doc, method=None):
	"""Run **before** Journal Entry cancel so PM Clearance link is cleared before ERPNext link checks.

	Previously ``on_cancel`` ran too late: Frappe blocks JE cancel while ``PM Clearance.journal_entry``
	still points at this JE (LinkExistsError).

	Settlement also stamps ``PM Clearance Detail.generated_document`` → JE; that dynamic link must be
	cleared before cancel completes (``check_no_back_links_exist``).
	"""
	from erpnext_extensions.petty_management import petty_audit
	from erpnext_extensions.petty_management.services.clearance_action_policy import sync_clearance_lifecycle

	for row_name in frappe.get_all(
		"PM Clearance Detail",
		filters={"generated_doctype": "Journal Entry", "generated_document": doc.name},
		pluck="name",
	):
		frappe.db.set_value(
			"PM Clearance Detail",
			row_name,
			{"generated_doctype": None, "generated_document": None},
			update_modified=False,
		)

	names = frappe.get_all(
		"PM Clearance",
		filters={"journal_entry": doc.name},
		pluck="name",
	)
	for cl_name in names:
		try:
			row = frappe.db.get_value(
				"PM Clearance",
				cl_name,
				["holder", "employee", "company"],
				as_dict=True,
			)
			petty_audit.log_event(
				"pm_journal_entry_cancelled",
				pm_clearance=cl_name,
				journal_entry=doc.name,
				holder=row.get("holder") if row else None,
				employee=row.get("employee") if row else None,
				company=row.get("company") if row else None,
			)
		except Exception:
			pass
	for cl_name in names:
		frappe.db.set_value("PM Clearance", cl_name, {"journal_entry": None}, update_modified=False)
		cl = frappe.get_doc("PM Clearance", cl_name)
		sync_clearance_lifecycle(cl, persist=True)
	_notify_pm_requests_for_journal_entry(doc.name, "on_journal_entry_cancelled")
