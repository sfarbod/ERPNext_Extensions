# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Journal Entry hooks for Facility Management receipt/repayment lifecycle.

Ensures Facility and Facility Repayment accounting references stay consistent
when a linked Journal Entry is cancelled.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.facility_management.facility_accounting import refresh_facility_paid_fields
from erpnext_extensions.facility_management.facility_je_reconciliation import (
	FLAG_IN_RECEIPT_REPAIR,
	FLAG_IN_REPAYMENT_CANCEL,
)


def _facilities_for_receipt_je(je_name: str) -> list[str]:
	return frappe.get_all(
		"Facility",
		filters={"receipt_journal_entry": je_name},
		pluck="name",
	)


def _repayments_for_je(je_name: str) -> list[dict]:
	return frappe.get_all(
		"Facility Repayment",
		filters={"journal_entry": je_name},
		fields=["name", "facility", "docstatus", "repayment_method"],
	)


def before_cancel(doc, method=None):
	"""Block unsafe cancellation of Facility-linked Journal Entries."""
	if getattr(frappe.flags, FLAG_IN_RECEIPT_REPAIR, False):
		return

	je_name = doc.name
	facilities = _facilities_for_receipt_je(je_name)
	for fac_name in facilities:
		submitted = frappe.get_all(
			"Facility Repayment",
			filters={"facility": fac_name, "docstatus": 1},
			pluck="name",
		)
		if submitted:
			frappe.throw(
				_(
					"Cannot cancel receipt Journal Entry {0}: Facility {1} has submitted "
					"repayment(s) {2}. Cancel those repayments first."
				).format(je_name, fac_name, ", ".join(submitted)),
				title=_("Facility Receipt Dependency"),
			)

	# Repayment JE: when cancelled from outside Facility Repayment.on_cancel, require
	# cancelling the repayment document instead (do not auto-cancel dependents).
	if getattr(frappe.flags, FLAG_IN_REPAYMENT_CANCEL, False):
		return

	for rep in _repayments_for_je(je_name):
		if cint(rep.docstatus) == 1:
			frappe.throw(
				_(
					"Cannot cancel Journal Entry {0} directly: it is the settlement JE for "
					"submitted Facility Repayment {1}. Cancel the Facility Repayment instead."
				).format(je_name, rep.name),
				title=_("Facility Repayment Dependency"),
			)


def on_cancel(doc, method=None):
	"""Reconcile Facility / Facility Repayment state after JE cancellation."""
	if getattr(frappe.flags, FLAG_IN_RECEIPT_REPAIR, False):
		return

	je_name = doc.name

	# Receipt JE → restore Facility for replacement eligibility
	for fac_name in _facilities_for_receipt_je(je_name):
		_reconcile_facility_after_receipt_je_cancel(fac_name, je_name)

	# If cancellation originated from FacilityRepayment.on_cancel, repayment will
	# clear journal_entry and refresh balances itself.
	if getattr(frappe.flags, FLAG_IN_REPAYMENT_CANCEL, False):
		return

	for rep in _repayments_for_je(je_name):
		if cint(rep.docstatus) == 2:
			# Stale pointer on already-cancelled repayment
			if frappe.db.get_value("Facility Repayment", rep.name, "journal_entry") == je_name:
				frappe.db.set_value(
					"Facility Repayment", rep.name, "journal_entry", None, update_modified=False
				)
			if rep.facility:
				refresh_facility_paid_fields(rep.facility)


def _reconcile_facility_after_receipt_je_cancel(facility_name: str, je_name: str) -> None:
	"""Clear cancelled receipt reference and restore Draft when safe.

	Raises if Facility update cannot be applied consistently (caller / cancel rolls back).
	"""
	fac = frappe.get_doc("Facility", facility_name)
	if (fac.receipt_journal_entry or "") != je_name:
		return

	submitted = frappe.get_all(
		"Facility Repayment",
		filters={"facility": facility_name, "docstatus": 1},
		pluck="name",
	)
	if submitted:
		# Should have been blocked in before_cancel; fail closed.
		frappe.throw(
			_(
				"Receipt Journal Entry {0} cancelled but Facility {1} still has submitted "
				"repayments {2}. Cancellation cannot complete safely."
			).format(je_name, facility_name, ", ".join(submitted)),
			title=_("Facility Receipt Rollback Failed"),
		)

	updates: dict = {
		"receipt_journal_entry": None,
		"received_amount": 0,
	}
	if not cint(fac.is_opening_facility) and fac.status == "Active":
		updates["status"] = "Draft"

	try:
		frappe.db.set_value("Facility", facility_name, updates, update_modified=True)
		refresh_facility_paid_fields(facility_name)
		# refresh_facility_paid_fields may set received_amount from principal when receipt JE set;
		# ensure cleared.
		frappe.db.set_value("Facility", facility_name, "received_amount", 0, update_modified=False)
	except Exception:
		frappe.throw(
			_(
				"Failed to restore Facility {0} after cancelling receipt Journal Entry {1}."
			).format(facility_name, je_name),
			title=_("Facility Receipt Rollback Failed"),
		)

	# Verify
	left = frappe.db.get_value("Facility", facility_name, "receipt_journal_entry")
	if left:
		frappe.throw(
			_("Facility {0} still references receipt Journal Entry after cancel.").format(facility_name),
			title=_("Facility Receipt Rollback Failed"),
		)
