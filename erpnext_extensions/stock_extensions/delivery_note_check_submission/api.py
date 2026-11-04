# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted API for Delivery Note Check Submission (strict read-only)."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.stock_extensions.delivery_note_check_submission.validator import (
	validate_delivery_note_submission,
)


@frappe.whitelist()
def validate_delivery_note_before_submit(name: str | None = None, delivery_note: str | None = None):
	"""Run strict read-only submission validations for a Draft Delivery Note.

	Accepts ``name`` or ``delivery_note`` (document name only — never trusts a
	client-supplied document payload). Requires Write permission; does **not**
	require Submit permission. Never mutates the database.
	"""
	docname = name or delivery_note
	if not docname:
		frappe.throw(_("Delivery Note name is required."), frappe.ValidationError)

	if not isinstance(docname, str):
		frappe.throw(_("Delivery Note name must be a string."), frappe.ValidationError)

	docname = docname.strip()
	if not docname or docname.startswith("{") or "\n" in docname:
		frappe.throw(_("Invalid Delivery Note name."), frappe.ValidationError)

	return validate_delivery_note_submission(docname)
