# Copyright (c) 2026, ERPNext Extensions contributors
"""Detect unsupported / unsafe validations for Check Submission."""

from __future__ import annotations

import frappe
from frappe import _


# Hooks we deliberately invoke in a controlled, read-only way.
SAFE_DN_VALIDATE_HOOKS = {
	"erpnext_extensions.iran_accounting.buying_selling.validate_delivery_note",
}


def collect_structural_limitations(doc) -> list[dict]:
	"""Return limitations that affect whether a DN can be reported as fully ready."""
	out: list[dict] = []
	out.extend(_server_script_limitations())
	out.extend(_doc_event_hook_limitations())
	out.extend(_mutating_path_limitations(doc))
	return out


def _server_script_limitations() -> list[dict]:
	if not frappe.db.exists("DocType", "Server Script"):
		return []
	scripts = frappe.get_all(
		"Server Script",
		filters={
			"disabled": 0,
			"script_type": "DocType Event",
			"reference_doctype": "Delivery Note",
			"doctype_event": (
				"in",
				(
					"Before Validate",
					"Before Save",
					"Before Submit",
					"After Submit",
				),
			),
		},
		fields=["name", "doctype_event"],
	)
	if not scripts:
		return []
	names = ", ".join(f"{s.name} ({s.doctype_event})" for s in scripts)
	return [
		{
			"code": "server_scripts",
			"severity": "blocking",
			"message": _(
				"Active Delivery Note Server Scripts were not executed (they may mutate data or "
				"call external systems): {0}. Check Submission cannot certify readiness while "
				"these scripts exist."
			).format(names),
		}
	]


def _doc_event_hook_limitations() -> list[dict]:
	events = frappe.get_hooks("doc_events") or {}
	dn_hooks = events.get("Delivery Note") or {}
	unsafe: list[str] = []
	for event in ("before_validate", "validate", "before_submit", "on_submit"):
		for path in dn_hooks.get(event) or []:
			if path in SAFE_DN_VALIDATE_HOOKS and event == "validate":
				continue
			unsafe.append(f"{event}: {path}")
	if not unsafe:
		return []
	return [
		{
			"code": "doc_event_hooks",
			"severity": "blocking",
			"message": _(
				"Custom Delivery Note doc_event hooks were not executed in Check Submission: {0}."
			).format("; ".join(unsafe)),
		}
	]


def _mutating_path_limitations(doc) -> list[dict]:
	out: list[dict] = [
		{
			"code": "sle_gle_simulation",
			"severity": "advisory",
			"message": _(
				"Stock Ledger Entry creation, General Ledger posting, Bin updates, Sales Order "
				"delivered qty, Stock Reservation Entry updates, Serial/Batch Bundle creation, "
				"notifications, webhooks, and background jobs are not executed."
			),
		},
		{
			"code": "repost_future_sle_and_gle",
			"severity": "advisory",
			"message": _(
				"Future Stock/GL repost interactions (Repost Item Valuation) are not validated."
			),
		},
		{
			"code": "update_pick_list_status",
			"severity": "advisory",
			"message": _("Pick List status updates are not validated."),
		},
	]
	if doc.get("is_return") and doc.get("issue_credit_note"):
		out.append(
			{
				"code": "make_return_invoice",
				"severity": "blocking",
				"message": _(
					"Return Delivery Notes with Issue Credit Note create a Sales Invoice on submit; "
					"that path is not validated read-only."
				),
			}
		)
	# Pick-list driven SABB duplication writes to DB — flag if present.
	if any(r.get("against_pick_list") and not r.get("serial_and_batch_bundle") for r in (doc.get("items") or [])):
		out.append(
			{
				"code": "pick_list_serial_batch_bundle",
				"severity": "blocking",
				"message": _(
					"This Delivery Note references a Pick List without a Serial and Batch Bundle on "
					"some rows. Submit-time bundle duplication is not simulated read-only."
				),
			}
		)
	return out


def has_blocking_limitations(limitations: list[dict]) -> bool:
	return any(l.get("severity") == "blocking" for l in limitations)
