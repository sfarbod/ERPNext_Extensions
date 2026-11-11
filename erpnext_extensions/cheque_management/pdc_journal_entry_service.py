# Copyright (c) 2025, erpnext-extensions and contributors
# For license information, please see license.txt

"""Post **Journal Entry** vouchers for PDC workflow transitions (idempotent per transition key).

Uses payloads from :func:`build_pdc_journal_entry_data` in ``post_dated_cheque``. Transition keys and
duplicate detection live in ``pdc_accounting_idempotency``. For operational labels, see
``pdc_workflow_to_cheque_status``; for allowed edges, ``pdc_workflow_state_machine``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils.synchronization import filelock

from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	_pdc_bank_gl_account,
	build_pdc_journal_entry_data,
)
from erpnext_extensions.cheque_management.pdc_accounting_idempotency import (
	build_pdc_accounting_transition_key,
	build_pdc_transition_key_suffix,
	find_open_occurrence_journal_entry,
	is_cycleable_open_occurrence_edge,
	normalize_cheque_direction_for_accounting_key,
	stored_transition_key_matches,
)
from erpnext_extensions.cheque_management.pdc_bank_dimension import (
	build_je_account_row_from_pdc_payload,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_CANCELLED,
	WORKFLOW_CLEARED,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	WORKFLOW_DRAFT,
	WORKFLOW_ENDORSED,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	WORKFLOW_REPLACED,
	WORKFLOW_RETURNED,
	WORKFLOW_SENT_TO_BANK,
	WORKFLOW_UNDER_LEGAL_ACTION,
	normalize_workflow_state_value,
)


def build_pdc_transition_key(
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> str:
	"""``direction|from|to`` suffix (legacy); prefer :func:`build_pdc_accounting_transition_key` for the full key."""
	return build_pdc_transition_key_suffix(cheque_direction, from_state, to_state)


def _purpose_for_transition(
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> str:
	"""Map workflow transition to ``purpose`` on PDC Journal Reference (canonical Select values only).

	Receivable:
	    Draft → Registered → Receive; Registered → Sent to Bank → Under Collection;
	    Sent to Bank → Bounced → Returned (bank dishonour; same label as party return);
	    Registered → Returned → Returned; Registered → Endorsed → Endorsement;
	    Bounced|Returned → Replaced → Receive (replacement book entry).

	Payable:
	    Draft → Registered → Payable Issue (supplier settlement); Registered → Cancelled → Cancel;
	    Issued → Returned → Returned;
	    Issued → Cancelled → Cancel; Issued → Replaced → Returned (same GL pattern as return);
	    Issued → Cleared → Payable Clear;
	    Returned → Replaced → Payable Issue (re-issue).

	Receivable **→Cleared** is a **Journal Entry** with purpose **Collected**; Payable **Issued → Cleared**
	uses purpose **Payable Clear** (see ``build_pdc_journal_entry_data``).

	Payment Entry is not used for PDC lifecycle in the Journal-centric architecture.
	"""
	d = normalize_cheque_direction_for_accounting_key(cheque_direction)
	f = normalize_workflow_state_value(from_state)
	t = normalize_workflow_state_value(to_state)
	if d == CHEQUE_DIRECTION_RECEIVABLE:
		if f == WORKFLOW_DRAFT and t == WORKFLOW_REGISTERED:
			return "Receive"
		if f == WORKFLOW_REGISTERED and t == WORKFLOW_SENT_TO_BANK:
			return "Under Collection"
		if f == WORKFLOW_SENT_TO_BANK and t == WORKFLOW_REGISTERED:
			return "Return from Bank"
		if f == WORKFLOW_REGISTERED and t == WORKFLOW_ASSIGNED_DEBT_PURCHASE:
			return "Debt Purchase Assignment"
		if f == WORKFLOW_BOUNCED and t == WORKFLOW_SENT_TO_BANK:
			# Re-send after bounce: same operational purpose as Registered → Sent to Bank.
			return "Under Collection"
		if f == WORKFLOW_BOUNCED and t == WORKFLOW_ASSIGNED_DEBT_PURCHASE:
			# Re-assign after bounce: same purpose as Registered → Assigned DP.
			return "Debt Purchase Assignment"
		if f == WORKFLOW_ASSIGNED_DEBT_PURCHASE and t == WORKFLOW_REGISTERED:
			# Unassign: reverse of Debt Purchase Assignment (instrument back to cashbox).
			return "Return from Debt Purchase"
		if f == WORKFLOW_ASSIGNED_DEBT_PURCHASE and t == WORKFLOW_DEBT_PURCHASE_SETTLED:
			return "Debt Purchase Settlement"
		if f == WORKFLOW_ASSIGNED_DEBT_PURCHASE and t == WORKFLOW_BOUNCED:
			# Canonical bounce tag matches Sent to Bank → Bounced (legacy "Bounce" not used for new rows).
			return "Returned"
		if t == WORKFLOW_CLEARED and f in (
			WORKFLOW_REGISTERED,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_UNDER_LEGAL_ACTION,
		):
			return "Collected"
		if f == WORKFLOW_SENT_TO_BANK and t == WORKFLOW_BOUNCED:
			return "Returned"
		if f == WORKFLOW_REGISTERED and t == WORKFLOW_RETURNED:
			return "Returned"
		if f == WORKFLOW_REGISTERED and t == WORKFLOW_ENDORSED:
			return "Endorsement"
		if f in (WORKFLOW_BOUNCED, WORKFLOW_RETURNED) and t == WORKFLOW_REPLACED:
			return "Receive"
	else:
		if f == WORKFLOW_DRAFT and t == WORKFLOW_REGISTERED:
			return "Payable Issue"
		if f == WORKFLOW_REGISTERED and t == WORKFLOW_CANCELLED:
			return "Cancel"
		if f == WORKFLOW_ISSUED and t == WORKFLOW_CLEARED:
			return "Payable Clear"
		if f == WORKFLOW_ISSUED and t == WORKFLOW_RETURNED:
			return "Returned"
		if f == WORKFLOW_ISSUED and t == WORKFLOW_CANCELLED:
			return "Cancel"
		if f == WORKFLOW_ISSUED and t == WORKFLOW_REPLACED:
			return "Returned"
		if f == WORKFLOW_RETURNED and t == WORKFLOW_REPLACED:
			return "Payable Issue"
	return "Receive" if d == CHEQUE_DIRECTION_RECEIVABLE else "Payable Issue"


def get_existing_journal_entry_for_transition(
	pdc_name: str,
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> str | None:
	"""Return existing Journal Entry name if this transition was already posted for the PDC.

	Matches **canonical** key ``cheque_name|direction|from|to`` or **legacy** ``direction|from|to``,
	using the same normalization as when the row was created. If no exact ``pdc_transition_key``
	hit is found (e.g. minor historical variants), falls back to scanning ``journal_references``
	with :func:`stored_transition_key_matches`.

	For cycleable Registered ↔ Sent to Bank edges, returns the JE only while an **open**
	occurrence exists (not closed by the opposite edge). This allows Send → Return → Send
	cycles without requiring a prior Send JE for Return from Bank.
	"""
	name = (pdc_name or "").strip()
	if not name:
		return None
	if is_cycleable_open_occurrence_edge(from_state, to_state):
		return find_open_occurrence_journal_entry(name, cheque_direction, from_state, to_state)

	full = build_pdc_accounting_transition_key(name, cheque_direction, from_state, to_state)
	legacy = build_pdc_transition_key_suffix(cheque_direction, from_state, to_state)
	for key in (full, legacy):
		je = frappe.db.get_value(
			"PDC Journal Reference",
			{"parent": name, "parenttype": "Post Dated Cheque", "pdc_transition_key": key},
			"journal_entry",
		)
		if je:
			return je
	rows = frappe.get_all(
		"PDC Journal Reference",
		filters={"parent": name, "parenttype": "Post Dated Cheque"},
		fields=["journal_entry", "pdc_transition_key"],
	)
	for row in rows:
		if stored_transition_key_matches(
			row.get("pdc_transition_key"),
			name,
			cheque_direction,
			from_state,
			to_state,
		):
			return row.get("journal_entry")
	return None


def create_and_submit_journal_entry_from_payload(
	pdc,
	payload: dict[str, Any],
	from_state: str | None,
	to_state: str | None,
	*,
	purpose: str | None = None,
) -> str:
	"""
	Create a submitted Journal Entry from payload dict (same shape as build_pdc_journal_entry_data),
	append one PDC Journal Reference row, return Journal Entry name.

	Idempotent: if this transition already has a journal row on the PDC, returns existing JE without creating another.
	Uses a file lock so concurrent requests cannot double-post the same transition.
	"""
	if getattr(pdc, "meta", None) and pdc.meta.name != "Post Dated Cheque":
		pdc = frappe.get_doc("Post Dated Cheque", pdc.name)
	if not pdc.name:
		frappe.throw(_("Save Post Dated Cheque before creating a Journal Entry."))

	ch_dir = normalize_cheque_direction_for_accounting_key(getattr(pdc, "cheque_direction", None))
	from_n = normalize_workflow_state_value(from_state)
	to_n = normalize_workflow_state_value(to_state)
	transition_key = build_pdc_accounting_transition_key(pdc.name, ch_dir, from_n, to_n)
	lock_name = f"pdc_accounting_je_{pdc.name}"

	with filelock(lock_name, timeout=120):
		pdc.reload()
		existing = get_existing_journal_entry_for_transition(pdc.name, ch_dir, from_n, to_n)
		if existing:
			return existing

		purpose = purpose or _purpose_for_transition(ch_dir, from_n, to_n)
		posting_date = payload.get("posting_date") or pdc.cheque_due_date or frappe.utils.today()
		amount = _payload_amount(payload) or pdc.cheque_amount or 0

		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date
		je.company = pdc.company
		je.voucher_type = payload.get("voucher_type") or "Journal Entry"
		je.user_remark = payload.get("remarks") or ""
		if pdc.cheque_no:
			je.cheque_no = pdc.cheque_no
		if pdc.cheque_due_date:
			je.cheque_date = pdc.cheque_due_date

		accounts = payload.get("accounts") or []
		if not accounts:
			frappe.throw(_("Journal Entry payload has no accounts."))

		# Finance policy: party on all lines from payload except bank GL on clear (payload builder enforces).
		bank_gl_on_clear = _pdc_bank_gl_account(pdc) if to_n == WORKFLOW_CLEARED else None

		for row in accounts:
			entry = build_je_account_row_from_pdc_payload(
				pdc,
				row,
				bank_gl_party_strip=bank_gl_on_clear,
			)
			je.append("accounts", entry)

		je.flags.ignore_permissions = True
		# IMPORTANT (UX): avoid double validation + duplicate informational warnings.
		# `submit()` performs the save/validate cycle for new docs, so calling `save()` first causes
		# validation (and msgprint warnings) to run twice.
		je.submit()

		pdc.reload()
		from erpnext_extensions.cheque_management.pdc_journal_reference_guard import (
			authorize_journal_reference_creation,
		)

		# Task 5: Advance-mode recognition accounting uses the same transition idempotency key but must
		# also flip the instrument recognition flag so Task 3 open-advance starts reporting gross/open.
		if int(payload.get("set_recognition_je_posted") or 0):
			pdc.recognition_je_posted = 1
		pdc.flags.ignore_validate_update_after_submit = True
		pdc.flags.skip_pdc_accounting_orchestration = True
		# Internal self-save after JE posting is re-entrant: it re-enters allocation validation after
		# the JE has already settled the referenced invoice(s), so Sales Invoice outstanding can be 0.
		# Bypass only *settlement capacity* checks for this PDC during this internal save; structural
		# allocation validations (mode/party/company/currency/reference integrity) still run.
		try:
			prev_flag = getattr(frappe.flags, "skip_pdc_allocation_capacity_validation_for_pdc", None)
		except Exception:
			prev_flag = None
		try:
			frappe.flags.skip_pdc_allocation_capacity_validation_for_pdc = pdc.name
			with authorize_journal_reference_creation(
				pdc.name,
				transition_keys=[transition_key] if transition_key else None,
			):
				pdc.append(
					"journal_references",
					{
						"journal_entry": je.name,
						"purpose": purpose,
						"pdc_transition_key": transition_key,
						"posting_date": posting_date,
						"amount": amount,
					},
				)
				pdc.save(ignore_permissions=True)
		finally:
			try:
				frappe.flags.skip_pdc_allocation_capacity_validation_for_pdc = prev_flag
			except Exception:
				pass
			pdc.flags.skip_pdc_accounting_orchestration = False

		return je.name


def post_pdc_transition_journal_entry(
	pdc,
	from_state: str | None,
	to_state: str | None,
	*,
	posting_date=None,
) -> str | None:
	"""
	Build journal payload from workflow transition and create/skip-if-exists JE.

	Returns Journal Entry name, or None if build_pdc_journal_entry_data returns None (no accounting).
	"""
	if getattr(pdc, "meta", None) and pdc.meta.name != "Post Dated Cheque":
		pdc = frappe.get_doc("Post Dated Cheque", pdc.name)

	from_n = normalize_workflow_state_value(from_state)
	to_n = normalize_workflow_state_value(to_state)
	ch_dir = normalize_cheque_direction_for_accounting_key(getattr(pdc, "cheque_direction", None))
	existing = get_existing_journal_entry_for_transition(pdc.name, ch_dir, from_n, to_n)
	if existing:
		return existing

	payload = build_pdc_journal_entry_data(pdc, from_n, to_n, posting_date=posting_date)
	if not payload:
		return None

	if posting_date and payload.get("posting_date") != posting_date:
		payload = dict(payload)
		payload["posting_date"] = posting_date

	return create_and_submit_journal_entry_from_payload(pdc, payload, from_n, to_n)


def _payload_amount(payload: dict[str, Any]) -> float:
	total_debit = 0.0
	for row in payload.get("accounts") or []:
		total_debit += float(row.get("debit_in_account_currency") or 0)
	return total_debit
