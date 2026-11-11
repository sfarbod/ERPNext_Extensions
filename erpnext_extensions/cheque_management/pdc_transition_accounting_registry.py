# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Declarative **PDC transition accounting** metadata (single place for policy, not GL builders).

**Receivable clearing:** **Journal Entry** only — Dr bank GL, Cr cheques in hand / clearing / protested
(see ``pdc_receivable_accounting``). No party on clear (customer is settled only at registration).

**Payable clearing:** **Journal Entry**: Dr notes payable pool, Cr bank GL (no party on clear, no invoice reference — only liability vs bank).

**Payable register (Draft → Registered):** Party/AP lines may carry **Purchase Invoice** ``reference_type`` / ``reference_name`` (from allocations: PI or PR→PI) so ERPNext settles invoices at register; clear JEs do not.

This module records, per edge: document type, whether the **party GL may be touched**, and short
notes. Actual account resolution stays in ``post_dated_cheque.py`` (needs doc + PDC Settings).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	PDC_ACCOUNTING_JOURNAL_ENTRY,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_CANCELLED,
	WORKFLOW_CLEARED,
	WORKFLOW_DRAFT,
	WORKFLOW_ENDORSED,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	WORKFLOW_RETURNED,
	WORKFLOW_SENT_TO_BANK,
	WORKFLOW_UNDER_LEGAL_ACTION,
)


@dataclass(frozen=True, slots=True)
class PDCAccountingTransitionSpec:
	"""Accounting meaning of one successful workflow edge (after state normalization)."""

	cheque_direction: str
	from_state: str
	to_state: str
	document_type: str
	touches_party: bool
	"""If True, at least one JE may carry ``party_type`` / ``party`` for GL with party dimension."""

	summary: str


def _spec(
	direction: str,
	fr: str,
	to: str,
	document_type: str,
	touches_party: bool,
	summary: str,
) -> tuple[tuple[str, str, str], PDCAccountingTransitionSpec]:
	return (direction, fr, to), PDCAccountingTransitionSpec(
		cheque_direction=direction,
		from_state=fr,
		to_state=to,
		document_type=document_type,
		touches_party=touches_party,
		summary=summary,
	)


# Keys are (cheque_direction, from_state, to_state) with **normalized** state labels.
PDC_ACCOUNTING_TRANSITION_REGISTRY: Final[dict[tuple[str, str, str], PDCAccountingTransitionSpec]] = dict(
	[
		# --- Receivable: party only where the business event changes receivable ---
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_DRAFT,
			WORKFLOW_REGISTERED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Cheques in Hand, Cr Party AR — drawer Party on both rows.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_REGISTERED,
			WORKFLOW_SENT_TO_BANK,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Cheques in Clearing, Cr Cheques in Hand — drawer Party on both rows.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_REGISTERED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Return from Bank: Dr Cheques in Hand, Cr Cheques in Clearing — drawer Party on both rows.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_REGISTERED,
			WORKFLOW_CLEARED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"JE: Dr Bank (no Party), Cr Cheques in Hand (drawer Party).",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_CLEARED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"JE: Dr Bank (no Party), Cr Cheques in Clearing (drawer Party).",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_UNDER_LEGAL_ACTION,
			WORKFLOW_CLEARED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"JE: Dr Bank (no Party), Cr protested/clearing/in-hand (drawer Party).",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_BOUNCED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Protested or Cheques in Hand, Cr Cheques in Clearing — drawer Party on both rows.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_REGISTERED,
			WORKFLOW_RETURNED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Party AR, Cr Cheques in Hand — drawer Party on both rows.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_REGISTERED,
			WORKFLOW_ENDORSED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr endorsement/holder AR, Cr Cheques in Hand.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_REGISTERED,
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Debt Purchase In Collection, Cr Cheques in Hand — drawer Party on both rows.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			WORKFLOW_BOUNCED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Protested, Cr Debt Purchase In Collection — drawer Party on both rows; no CIH fallback.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			WORKFLOW_REGISTERED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr assignment credit pool (usually CIH), Cr Debt Purchase In Collection — Return from Debt Purchase.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_BOUNCED,
			WORKFLOW_SENT_TO_BANK,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Cheques in Clearing, Cr bounce holding (protested or CIH) — re-send after bounce; Party on both.",
		),
		_spec(
			CHEQUE_DIRECTION_RECEIVABLE,
			WORKFLOW_BOUNCED,
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Debt Purchase In Collection, Cr bounce holding (protested or CIH) — re-assign after bounce; Party on both.",
		),
		# Payable: register / return / cancel / clear; clear uses JE (not Payment Entry — journal-centric architecture)
		_spec(
			CHEQUE_DIRECTION_PAYABLE,
			WORKFLOW_DRAFT,
			WORKFLOW_REGISTERED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr Party payable (PI ref on line when allocations map to invoices), Cr notes payable pool — supplier Party on both.",
		),
		_spec(
			CHEQUE_DIRECTION_PAYABLE,
			WORKFLOW_REGISTERED,
			WORKFLOW_CANCELLED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"Dr notes payable pool, Cr Party payable — reverse register settlement (PI refs when applicable); Party on both.",
		),
		_spec(
			CHEQUE_DIRECTION_PAYABLE,
			WORKFLOW_ISSUED,
			WORKFLOW_CLEARED,
			PDC_ACCOUNTING_JOURNAL_ENTRY,
			True,
			"JE: Dr notes payable pool (supplier Party), Cr Bank (no Party).",
		),
	]
)


def get_accounting_transition_spec(
	cheque_direction: str,
	from_state: str,
	to_state: str,
) -> PDCAccountingTransitionSpec | None:
	"""Return the registry row for this edge, or ``None`` if not documented (policy may still exist elsewhere)."""
	return PDC_ACCOUNTING_TRANSITION_REGISTRY.get(
		(cheque_direction, from_state, to_state),
	)


__all__ = [
	"PDC_ACCOUNTING_TRANSITION_REGISTRY",
	"PDCAccountingTransitionSpec",
	"get_accounting_transition_spec",
]
