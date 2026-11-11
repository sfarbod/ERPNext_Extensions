# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""PDC accounting **transition keys** and idempotent resolution.

**Canonical key** (stored on ``PDC Journal Reference.pdc_transition_key``)::

    ``{cheque_name}|{cheque_direction}|{from_state}|{to_state}``

* ``cheque_name`` — trimmed PDC name (``tabPost Dated Cheque.name``).
* ``cheque_direction`` — canonical **Receivable** or **Payable** (see
  :func:`normalize_cheque_direction_for_accounting_key`).
* ``from_state`` / ``to_state`` — normalized with
  :func:`~pdc_workflow_state_machine.normalize_workflow_state_value`
  (blank/``None`` → **Draft**).

**Rules**

* Most edges: at most one Journal Entry per key for the life of the PDC.
* **Cycleable bank-collection edges** (Receivable ``Registered ↔ Sent to Bank``):
  open-occurrence idempotency — reuse JE only while the latest matching occurrence is
  still open (not closed by the opposite edge). A later opposite transition allows a
  new JE for the same ``from|to`` key (Send → Return → Send cycles).
* Return from Bank does **not** require a prior Send JE (opening-balance safe).
* ``journal_references`` stores the key plus ``purpose``, ``posting_date``, ``amount``,
  and ``journal_entry``.

**Legacy key** (still matched for existing DB rows)::

    ``{cheque_direction}|{from_state}|{to_state}``

(no cheque name prefix). Kept only for historical transition-key compatibility; lifecycle posting
is **Journal Entry** only.
"""

from __future__ import annotations

from typing import Final

import frappe

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_REGISTERED,
	WORKFLOW_SENT_TO_BANK,
	normalize_workflow_state_value,
)

# Edges that may repeat across Send ↔ Return / bounce ↔ reassign cycles
# (same stored key, open-occurrence reuse).
CYCLEABLE_OPEN_OCCURRENCE_EDGES: Final[frozenset[tuple[str, str]]] = frozenset(
	{
		(WORKFLOW_REGISTERED, WORKFLOW_SENT_TO_BANK),
		(WORKFLOW_SENT_TO_BANK, WORKFLOW_REGISTERED),
		(WORKFLOW_SENT_TO_BANK, WORKFLOW_BOUNCED),
		(WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK),
		(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED),
		(WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE),
		(WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE),
		(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED),
	}
)

_OPPOSITE_CYCLEABLE_EDGE: Final[dict[tuple[str, str], tuple[str, str]]] = {
	(WORKFLOW_REGISTERED, WORKFLOW_SENT_TO_BANK): (WORKFLOW_SENT_TO_BANK, WORKFLOW_REGISTERED),
	(WORKFLOW_SENT_TO_BANK, WORKFLOW_REGISTERED): (WORKFLOW_REGISTERED, WORKFLOW_SENT_TO_BANK),
	(WORKFLOW_SENT_TO_BANK, WORKFLOW_BOUNCED): (WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK),
	(WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK): (WORKFLOW_SENT_TO_BANK, WORKFLOW_BOUNCED),
	(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED): (
		WORKFLOW_BOUNCED,
		WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	),
	(WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE): (
		WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		WORKFLOW_BOUNCED,
	),
	(WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE): (
		WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		WORKFLOW_REGISTERED,
	),
	(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED): (
		WORKFLOW_REGISTERED,
		WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	),
}


def normalize_cheque_direction_for_accounting_key(cheque_direction: str | None) -> str:
	"""Return canonical direction label used inside transition keys.

	Empty/unknown values default to **Receivable** (aligns with journal service fallback).
	"""
	s = (cheque_direction or "").strip()
	if not s:
		return CHEQUE_DIRECTION_RECEIVABLE
	low = s.lower()
	if low == "payable":
		return CHEQUE_DIRECTION_PAYABLE
	if low == "receivable":
		return CHEQUE_DIRECTION_RECEIVABLE
	return s


def build_pdc_transition_key_suffix(
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> str:
	"""Return ``direction|from|to`` only (legacy persisted form; unique per PDC when combined with parent)."""
	d = normalize_cheque_direction_for_accounting_key(cheque_direction)
	f = normalize_workflow_state_value(from_state)
	t = normalize_workflow_state_value(to_state)
	return f"{d}|{f}|{t}"


def build_pdc_accounting_transition_key(
	cheque_name: str,
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> str:
	"""Full transition key: ``cheque_name|cheque_direction|from_state|to_state``."""
	name = (cheque_name or "").strip()
	suffix = build_pdc_transition_key_suffix(cheque_direction, from_state, to_state)
	return f"{name}|{suffix}"


def stored_transition_key_matches(
	stored: str | None,
	cheque_name: str,
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> bool:
	"""True if ``stored`` matches the canonical full key or the legacy suffix."""
	if stored is None:
		return False
	s = str(stored).strip()
	if not s:
		return False
	full = build_pdc_accounting_transition_key(cheque_name, cheque_direction, from_state, to_state)
	suffix = build_pdc_transition_key_suffix(cheque_direction, from_state, to_state)
	return s == full or s == suffix


def parse_pdc_transition_key_parts(
	stored: str | None,
	cheque_name: str,
) -> tuple[str, str, str] | None:
	"""Parse ``pdc_transition_key`` into ``(direction, from_state, to_state)``."""
	if not stored:
		return None
	parts = [p.strip() for p in str(stored).split("|") if p.strip() != ""]
	name = (cheque_name or "").strip()
	if len(parts) == 4 and parts[0] == name:
		return (
			parts[1],
			normalize_workflow_state_value(parts[2]),
			normalize_workflow_state_value(parts[3]),
		)
	if len(parts) == 3:
		return (
			parts[0],
			normalize_workflow_state_value(parts[1]),
			normalize_workflow_state_value(parts[2]),
		)
	return None


def is_cycleable_open_occurrence_edge(from_state: str | None, to_state: str | None) -> bool:
	"""True for Receivable Registered ↔ Sent to Bank edges that support multi-cycle posting."""
	edge = (
		normalize_workflow_state_value(from_state),
		normalize_workflow_state_value(to_state),
	)
	return edge in CYCLEABLE_OPEN_OCCURRENCE_EDGES


def find_open_occurrence_journal_entry(
	pdc_name: str,
	cheque_direction: str,
	from_state: str | None,
	to_state: str | None,
) -> str | None:
	"""Return JE for an *open* occurrence of a cycleable edge, else ``None`` (post a new JE).

	Walk ``journal_references`` in order. Seeing the target edge opens/replaces the open JE;
	seeing the opposite edge closes it. Does **not** require a prior opposite JE to exist
	(Return from Bank is valid without a prior Send JE).
	"""
	name = (pdc_name or "").strip()
	if not name:
		return None
	edge = (
		normalize_workflow_state_value(from_state),
		normalize_workflow_state_value(to_state),
	)
	opposite = _OPPOSITE_CYCLEABLE_EDGE.get(edge)
	if not opposite:
		return None

	rows = frappe.get_all(
		"PDC Journal Reference",
		filters={"parent": name, "parenttype": "Post Dated Cheque"},
		fields=["journal_entry", "pdc_transition_key", "idx"],
		order_by="idx asc, creation asc",
	)
	open_je: str | None = None
	for row in rows:
		parts = parse_pdc_transition_key_parts(row.get("pdc_transition_key"), name)
		if not parts:
			continue
		_dir, f, t = parts
		if (f, t) == edge:
			je = (row.get("journal_entry") or "").strip()
			open_je = je or None
		elif (f, t) == opposite:
			open_je = None
	return open_je


__all__ = [
	"CYCLEABLE_OPEN_OCCURRENCE_EDGES",
	"build_pdc_accounting_transition_key",
	"build_pdc_transition_key_suffix",
	"find_open_occurrence_journal_entry",
	"is_cycleable_open_occurrence_edge",
	"normalize_cheque_direction_for_accounting_key",
	"parse_pdc_transition_key_parts",
	"stored_transition_key_matches",
]
