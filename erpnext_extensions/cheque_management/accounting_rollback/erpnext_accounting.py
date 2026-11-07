"""Thin wrappers around ERPNext accounting services — no duplicated GL/PLE logic."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import flt

OUTSTANDING_VOUCHERS = frozenset({"Sales Invoice", "Purchase Invoice"})


def cancel_journal_entry_voucher(je_name: str) -> None:
	"""Cancel submitted JE (reverses GL/PLE via ERPNext).

	Does not set ignore_links / ignore_linked_doctypes — rollback must detach
	operational PDC Journal References first. Lifecycle Event.journal_entry is
	audit Data (v4.7.0+) and must not block cancel.
	"""
	je = frappe.get_doc("Journal Entry", je_name)
	if je.docstatus == 1:
		je.flags.ignore_permissions = True
		je.cancel()
	elif je.docstatus == 0:
		je.flags.ignore_permissions = True
		je.delete()


def refresh_outstanding_for_journal_entry(je_name: str) -> list[dict[str, Any]]:
	"""Always refresh party voucher outstanding after cancelling a JE with invoice references."""
	from erpnext.accounts.utils import update_voucher_outstanding

	updated: list[dict[str, Any]] = []
	seen: set[tuple] = set()
	for row in frappe.get_all(
		"Journal Entry Account",
		filters={"parent": je_name},
		fields=["reference_type", "reference_name", "party_type", "party", "account"],
	):
		ref_type = (row.reference_type or "").strip()
		ref_name = (row.reference_name or "").strip()
		if ref_type not in OUTSTANDING_VOUCHERS or not ref_name:
			continue
		key = (ref_type, ref_name, row.account, row.party_type, row.party)
		if key in seen:
			continue
		seen.add(key)
		before = flt(frappe.db.get_value(ref_type, ref_name, "outstanding_amount"))
		update_voucher_outstanding(ref_type, ref_name, row.account, row.party_type, row.party)
		after = flt(frappe.db.get_value(ref_type, ref_name, "outstanding_amount"))
		updated.append(
			{
				"voucher_type": ref_type,
				"voucher_no": ref_name,
				"outstanding_before": before,
				"outstanding_after": after,
			}
		)
	return updated


def journal_entry_impact_snapshot(je_name: str) -> dict[str, Any]:
	"""Read-only counts + outstanding preview for UI (dry-run)."""
	gl_count = frappe.db.count("GL Entry", {"voucher_no": je_name, "is_cancelled": 0})
	ple_count = frappe.db.count("Payment Ledger Entry", {"voucher_no": je_name, "delinked": 0})
	outstanding_effects: list[dict[str, Any]] = []
	for row in frappe.get_all(
		"Journal Entry Account",
		filters={"parent": je_name},
		fields=[
			"reference_type",
			"reference_name",
			"debit_in_account_currency",
			"credit_in_account_currency",
		],
	):
		ref_type = (row.reference_type or "").strip()
		ref_name = (row.reference_name or "").strip()
		if ref_type not in OUTSTANDING_VOUCHERS or not ref_name:
			continue
		current = flt(frappe.db.get_value(ref_type, ref_name, "outstanding_amount"))
		# Cancelling JE reverses its effect on party/outstanding (register lines credit party/AP).
		delta = flt(row.credit_in_account_currency) - flt(row.debit_in_account_currency)
		after_est = current + delta
		outstanding_effects.append(
			{
				"voucher_type": ref_type,
				"voucher_no": ref_name,
				"outstanding_current": current,
				"outstanding_after_rollback": after_est,
				"outstanding_unchanged": abs(delta) < 1e-9,
			}
		)
	if not outstanding_effects:
		outstanding_effects.append(
			{
				"voucher_type": None,
				"voucher_no": None,
				"outstanding_current": None,
				"outstanding_after_rollback": None,
				"outstanding_unchanged": True,
				"note": "No invoice outstanding affected by this Journal Entry.",
			}
		)
	return {
		"journal_entry": je_name,
		"gl_entry_count": gl_count,
		"payment_ledger_entry_count": ple_count,
		"outstanding_effects": outstanding_effects,
	}


def rollback_journal_reference_row(row_name: str | None) -> None:
	if not row_name or not frappe.db.exists("PDC Journal Reference", row_name):
		return
	from erpnext_extensions.cheque_management.pdc_journal_reference_guard import (
		authorize_journal_reference_removal,
	)

	meta = frappe.db.get_value(
		"PDC Journal Reference",
		row_name,
		["parent", "pdc_transition_key"],
		as_dict=True,
	)
	pdc_name = (meta.parent if meta else "") or ""
	tkey = (meta.pdc_transition_key if meta else "") or ""
	with authorize_journal_reference_removal(
		pdc_name,
		rows=[row_name],
		transition_keys=[tkey] if tkey else None,
	):
		frappe.delete_doc("PDC Journal Reference", row_name, force=1, ignore_permissions=True)


def find_journal_reference_rows_for_je(pdc_name: str, je_name: str) -> list[str]:
	"""Return PDC Journal Reference names on ``pdc_name`` pointing at ``je_name``."""
	pdc_name = (pdc_name or "").strip()
	je_name = (je_name or "").strip()
	if not pdc_name or not je_name:
		return []
	return frappe.get_all(
		"PDC Journal Reference",
		filters={
			"parent": pdc_name,
			"parenttype": "Post Dated Cheque",
			"journal_entry": je_name,
		},
		pluck="name",
		order_by="idx asc",
	)


def resolve_journal_reference_row_for_rollback(
	pdc_name: str,
	je_name: str,
	*,
	preferred_row: str | None = None,
	require_row: bool = True,
) -> str | None:
	"""Resolve the operational JR row to remove before cancelling ``je_name``.

	- Prefer ``preferred_row`` when it still exists and points at this JE on this PDC.
	- Otherwise resolve by parent + journal_entry.
	- Exactly one match → return it.
	- Zero matches → fail closed when ``require_row`` (accounting undo expects a JR).
	- Multiple matches → always fail closed (ambiguous).
	"""
	pdc_name = (pdc_name or "").strip()
	je_name = (je_name or "").strip()
	preferred = (preferred_row or "").strip() or None

	if preferred and frappe.db.exists("PDC Journal Reference", preferred):
		row = frappe.db.get_value(
			"PDC Journal Reference",
			preferred,
			["name", "parent", "parenttype", "journal_entry"],
			as_dict=True,
		)
		if (
			row
			and (row.parent or "") == pdc_name
			and (row.parenttype or "") == "Post Dated Cheque"
			and (row.journal_entry or "").strip() == je_name
		):
			return preferred

	matches = find_journal_reference_rows_for_je(pdc_name, je_name)
	if len(matches) > 1:
		raise ValidationError(
			_(
				"Rollback is blocked: Journal Entry {0} has multiple PDC Journal References "
				"on {1}. Refusing ambiguous cleanup."
			).format(je_name, pdc_name)
		)
	if len(matches) == 1:
		return matches[0]
	if require_row:
		raise ValidationError(
			_(
				"Rollback is blocked: no PDC Journal Reference found for Journal Entry {0} "
				"on {1}. Cannot safely detach the operational link before cancellation."
			).format(je_name, pdc_name)
		)
	return None


def remove_operational_journal_reference_for_step(pdc_name: str, step) -> str | None:
	"""Delete the rollback-owned PDC Journal Reference for an accounting step.

	Returns the removed row name (if any). Fail-closed on ambiguous/missing links
	when the step has a Journal Entry to cancel.
	"""
	je_name = (getattr(step, "journal_entry", None) or "").strip()
	if not je_name:
		return None
	preferred = (getattr(step, "journal_reference_row", None) or "").strip() or None
	row_name = resolve_journal_reference_row_for_rollback(
		pdc_name,
		je_name,
		preferred_row=preferred,
		require_row=True,
	)
	if row_name:
		rollback_journal_reference_row(row_name)
		if preferred and preferred != row_name:
			step.journal_reference_row = row_name
	return row_name


def cancel_exchange_gain_loss_for_pair(
	parent_doctype: str, parent_name: str, referenced_doctype: str, referenced_name: str
) -> None:
	from erpnext.accounts.utils import cancel_exchange_gain_loss_journal

	parent = frappe.get_doc(parent_doctype, parent_name)
	cancel_exchange_gain_loss_journal(parent, referenced_doctype, referenced_name)
