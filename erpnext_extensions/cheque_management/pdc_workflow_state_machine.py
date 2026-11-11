# Copyright (c) 2025, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Canonical **workflow state machine** and **accounting policy** for Post Dated Cheque.

The DocType stores the current stage in ``workflow_state`` (Link → **Workflow State**).
Desk may offer a superset of actions via ERPNext **Workflow** transitions; **this module**
defines which edges are actually valid per ``cheque_direction`` and what happens on save.

**Target architecture (journal-centric lifecycle)**

* **Journal Entry only** for posting the PDC lifecycle. **Payment Entry is not used** for PDC workflow
  transitions.
* **Receivable:** party-side settlement **only** at **Registered** (Draft → Registered).
* **Payable:** party-side settlement **only** at **Registered** (Draft → Registered); **Issued** is operational only.
* **Cleared:** **only** moves value between **bank** GL and **intermediary / pool** accounts via
  **Journal Entry** — **no party** on those lines.
* **Allocation** of amounts to invoices or advances is **not** owned by this state machine; it lives in
  a separate allocation layer decoupled from workflow edges.

**Transition tables**

* :data:`RECEIVABLE_WORKFLOW_TRANSITIONS` — customer cheques (in hand, sent to bank, bounce, endorsement, …).
* :data:`PAYABLE_WORKFLOW_TRANSITIONS` — own cheques (register, issue, clear, return, …).

**Terminal states** (no change *away* from these except staying put): **Cleared**, **Cancelled**,
**Replaced**. Locked in :func:`is_workflow_transition_allowed`, :func:`get_allowed_next_workflow_states`,
and first in :func:`get_pdc_workflow_transition_validation_error` via :data:`PDC_TERMINAL_WORKFLOW_STATES`.

**Direction-only workflow labels**

Some ``workflow_state`` values only make sense for one direction (e.g. **Bounced** / **Sent to Bank**
for Receivable, **Issued** for Payable). :func:`get_pdc_workflow_transition_validation_error` rejects
invalid combinations even if Workflow UI exposes the action.

**Accounting** (current policy tables vs design intent)

:data:`_RECEIVABLE_ACCOUNTING_DECISIONS` and :data:`_PAYABLE_ACCOUNTING_DECISIONS` map ``(from, to)``
edges to ``journal_entry`` or ``no_document`` only. Missing keys mean “no policy row”;
``post_dated_cheque.get_accounting_action`` treats that as ``no_document``. Receivable **→ Cleared** and
Payable **Issued → Cleared** use **Journal Entry** (bank vs pool; **no party** on clear lines).

**Integration**

Wire :func:`get_pdc_workflow_transition_validation_error` into **Post Dated Cheque** ``validate()``.
For hints without a full Document, use :func:`get_allowed_next_workflow_states` /
:class:`PDCWorkflowTransitionSource` with :func:`get_allowed_transitions`.

See also: ``pdc_workflow_to_cheque_status.py`` (operational ``cheque_status``), ``DEVELOPER.md``.
"""

from __future__ import annotations

from typing import Final, Protocol

import frappe

# --- Cheque direction (DocType `cheque_direction`) ---

CHEQUE_DIRECTION_RECEIVABLE: Final = "Receivable"
CHEQUE_DIRECTION_PAYABLE: Final = "Payable"

# --- Workflow state labels (DocType `workflow_state` Select options) ---

WORKFLOW_DRAFT: Final = "Draft"
WORKFLOW_REGISTERED: Final = "Registered"
WORKFLOW_SENT_TO_BANK: Final = "Sent to Bank"
WORKFLOW_ISSUED: Final = "Issued"
WORKFLOW_CLEARED: Final = "Cleared"
WORKFLOW_RETURNED: Final = "Returned"
WORKFLOW_BOUNCED: Final = "Bounced"
WORKFLOW_ENDORSED: Final = "Endorsed"
WORKFLOW_CANCELLED: Final = "Cancelled"
WORKFLOW_REPLACED: Final = "Replaced"
WORKFLOW_UNDER_LEGAL_ACTION: Final = "Under Legal Action"
WORKFLOW_ASSIGNED_DEBT_PURCHASE: Final = "Assigned to Bank for Debt Purchase"
WORKFLOW_DEBT_PURCHASE_SETTLED: Final = "Debt Purchase Settled"

# All workflow states that appear in DocType options (order matches DocType for traceability)
ALL_WORKFLOW_STATES: Final[tuple[str, ...]] = (
	WORKFLOW_DRAFT,
	WORKFLOW_REGISTERED,
	WORKFLOW_SENT_TO_BANK,
	WORKFLOW_ISSUED,
	WORKFLOW_CLEARED,
	WORKFLOW_RETURNED,
	WORKFLOW_BOUNCED,
	WORKFLOW_ENDORSED,
	WORKFLOW_CANCELLED,
	WORKFLOW_REPLACED,
	WORKFLOW_UNDER_LEGAL_ACTION,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
)

# --- Allowed transitions: from_state -> set of allowed to_state ---
#
# These dicts ARE the workflow state machine for Post Dated Cheque.workflow_state.
#
# Receivable:
#   Draft -> Registered, Cancelled
#   Registered -> Sent to Bank, Cleared, Returned, Endorsed, Replaced, Under Legal Action, Cancelled
#   Sent to Bank -> Cleared, Bounced, Registered (Return from Bank)
#   Bounced -> Returned, Replaced, Under Legal Action, Sent to Bank (re-send),
#              Assigned to Bank for Debt Purchase (re-assign after bounce)
#   Returned -> Replaced, Cancelled
#   Under Legal Action -> Cleared, Returned
#   Endorsed -> (no transitions) — instrument left the company; see PDC_VALIDATION_ENDORSED_NO_BANK_CLEAR
#
# Payable:
#   Draft -> Registered, Cancelled
#   Registered -> Issued, Cancelled
#   Issued -> Cleared, Returned, Replaced, Cancelled
#   Returned -> Replaced, Cancelled
#
# Terminal (see PDC_TERMINAL_WORKFLOW_STATES): Cleared, Cancelled, Replaced.
# **Endorsed** (Receivable) is a holding state with **no outgoing** transitions: no Sent to Bank / Cleared
# by this company after endorsement (holder_history + GL endorsement move the asset off-books here).

RECEIVABLE_WORKFLOW_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
	WORKFLOW_DRAFT: frozenset({WORKFLOW_REGISTERED}),
	WORKFLOW_REGISTERED: frozenset(
		{
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_CLEARED,
			WORKFLOW_RETURNED,
			WORKFLOW_ENDORSED,
			WORKFLOW_REPLACED,
			WORKFLOW_UNDER_LEGAL_ACTION,
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		}
	),
	WORKFLOW_SENT_TO_BANK: frozenset({WORKFLOW_CLEARED, WORKFLOW_BOUNCED, WORKFLOW_REGISTERED}),
	WORKFLOW_BOUNCED: frozenset(
		{
			WORKFLOW_RETURNED,
			WORKFLOW_REPLACED,
			WORKFLOW_UNDER_LEGAL_ACTION,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		}
	),
	WORKFLOW_RETURNED: frozenset({WORKFLOW_REPLACED}),
	WORKFLOW_UNDER_LEGAL_ACTION: frozenset({WORKFLOW_CLEARED, WORKFLOW_RETURNED}),
	WORKFLOW_ENDORSED: frozenset(),
	# Assigned DP: Bounce (dishonour) or Return from Debt Purchase → Registered (unassign).
	# Settled is Facility Repayment only (not in this set). Cleared / Returned remain forbidden.
	WORKFLOW_ASSIGNED_DEBT_PURCHASE: frozenset({WORKFLOW_BOUNCED, WORKFLOW_REGISTERED}),
	WORKFLOW_DEBT_PURCHASE_SETTLED: frozenset(),
}

PAYABLE_WORKFLOW_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
	WORKFLOW_DRAFT: frozenset({WORKFLOW_REGISTERED}),
	WORKFLOW_REGISTERED: frozenset({WORKFLOW_ISSUED}),
	WORKFLOW_ISSUED: frozenset({WORKFLOW_CLEARED, WORKFLOW_RETURNED, WORKFLOW_REPLACED}),
	WORKFLOW_RETURNED: frozenset({WORKFLOW_REPLACED}),
}

_TRANSITION_MAP: Final[dict[str, dict[str, frozenset[str]]]] = {
	CHEQUE_DIRECTION_RECEIVABLE: RECEIVABLE_WORKFLOW_TRANSITIONS,
	CHEQUE_DIRECTION_PAYABLE: PAYABLE_WORKFLOW_TRANSITIONS,
}

# --- Accounting decisions (document type) per transition ---

PDC_ACCOUNTING_JOURNAL_ENTRY: Final = "journal_entry"
PDC_ACCOUNTING_NO_DOCUMENT: Final = "no_document"

# Per-edge posting: see get_pdc_accounting_decision. Allowed workflow edges **without** a row here
# (e.g. Registered→Replaced, Registered→Under Legal Action, Returned→Cancelled) imply no auto GL line.
_RECEIVABLE_ACCOUNTING_DECISIONS: Final[dict[tuple[str, str], str]] = {
	# Draft → Registered = JE (initial PDC register)
	(WORKFLOW_DRAFT, WORKFLOW_REGISTERED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Sent to Bank = JE (move from in-hand to clearing)
	(WORKFLOW_REGISTERED, WORKFLOW_SENT_TO_BANK): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Cleared = JE (Dr bank, Cr cheques in hand) — no party
	(WORKFLOW_REGISTERED, WORKFLOW_CLEARED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Sent to Bank → Cleared = JE (Dr bank, Cr cheques in clearing) — no party
	(WORKFLOW_SENT_TO_BANK, WORKFLOW_CLEARED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Sent to Bank → Bounced = JE (bank return)
	(WORKFLOW_SENT_TO_BANK, WORKFLOW_BOUNCED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Sent to Bank → Registered = JE (Return from Bank; reverse of Send to Bank)
	(WORKFLOW_SENT_TO_BANK, WORKFLOW_REGISTERED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Returned = JE (business return from in-hand)
	(WORKFLOW_REGISTERED, WORKFLOW_RETURNED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Endorsed = JE (endorsement / transfer)
	(WORKFLOW_REGISTERED, WORKFLOW_ENDORSED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Assigned to Bank for Debt Purchase = JE (Dr DP in collection, Cr in hand)
	(WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Assigned DP → Bounced = JE (Dr protested, Cr DP in collection) — Protested required
	(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Assigned DP → Registered = JE (Return from Debt Purchase; reverse of assignment)
	(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Assigned DP → Settled: Facility Repayment owns the JE (PDC journal_reference only)
	(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_DEBT_PURCHASE_SETTLED): PDC_ACCOUNTING_NO_DOCUMENT,
	# Bounced → Returned = no business accounting document (workflow-only / customer follow-up)
	(WORKFLOW_BOUNCED, WORKFLOW_RETURNED): PDC_ACCOUNTING_NO_DOCUMENT,
	# Bounced → Replaced = JE (replacement cheque after bank bounce)
	(WORKFLOW_BOUNCED, WORKFLOW_REPLACED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Bounced → Under Legal Action = no document (legal follow-up only)
	(WORKFLOW_BOUNCED, WORKFLOW_UNDER_LEGAL_ACTION): PDC_ACCOUNTING_NO_DOCUMENT,
	# Bounced → Sent to Bank = JE (re-send after bounce; Dr clearing, Cr bounce holding)
	(WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Bounced → Assigned DP = JE (re-assign after bounce; Dr DPIC, Cr bounce holding)
	(WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Returned → Replaced = JE (issue replacement against returned)
	(WORKFLOW_RETURNED, WORKFLOW_REPLACED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Cancelled = no document (until explicit reverse logic is added)
	(WORKFLOW_REGISTERED, WORKFLOW_CANCELLED): PDC_ACCOUNTING_NO_DOCUMENT,
	# Under Legal Action → Cleared = JE (Dr bank, Cr protested/clearing/in-hand) — no party
	(WORKFLOW_UNDER_LEGAL_ACTION, WORKFLOW_CLEARED): PDC_ACCOUNTING_JOURNAL_ENTRY,
}

# Payable accounting policy (see design doc §7.4): matches allowed edges in PAYABLE_WORKFLOW_TRANSITIONS.
_PAYABLE_ACCOUNTING_DECISIONS: Final[dict[tuple[str, str], str]] = {
	# Draft → Registered: Dr AP (PI refs), Cr notes-payable pool — supplier settlement here.
	(WORKFLOW_DRAFT, WORKFLOW_REGISTERED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	# Registered → Issued: operational step only (no GL).
	(WORKFLOW_REGISTERED, WORKFLOW_ISSUED): PDC_ACCOUNTING_NO_DOCUMENT,
	(WORKFLOW_ISSUED, WORKFLOW_CLEARED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	(WORKFLOW_ISSUED, WORKFLOW_RETURNED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	(WORKFLOW_ISSUED, WORKFLOW_REPLACED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	(WORKFLOW_ISSUED, WORKFLOW_CANCELLED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	(WORKFLOW_RETURNED, WORKFLOW_REPLACED): PDC_ACCOUNTING_JOURNAL_ENTRY,
	(WORKFLOW_RETURNED, WORKFLOW_CANCELLED): PDC_ACCOUNTING_NO_DOCUMENT,
	(WORKFLOW_DRAFT, WORKFLOW_CANCELLED): PDC_ACCOUNTING_NO_DOCUMENT,
	# Registered → Cancelled: reverse Draft→Registered settlement (after register, before issue).
	(WORKFLOW_REGISTERED, WORKFLOW_CANCELLED): PDC_ACCOUNTING_JOURNAL_ENTRY,
}

# Post Dated Cheque: explicit rule for workflow_state Bounced (stricter UX message)
PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK: Final = (
	"Bounced is only allowed after Sent to Bank or Assigned to Bank for Debt Purchase "
	"for Receivable cheques."
)

PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY: Final = "Endorsed is only valid for Receivable cheques."

PDC_VALIDATION_ENDORSED_NO_BANK_CLEAR: Final = (
	"Endorsed receivable cheques cannot be sent to the bank or cleared here — "
	"the instrument was transferred to the endorsed holder (use holder history for the handover record)."
)

PDC_VALIDATION_ISSUED_PAYABLE_ONLY: Final = "Issued is only valid for Payable cheques."

PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY: Final = "Sent to Bank is only valid for Receivable cheques."

PDC_VALIDATION_DEBT_PURCHASE_RECEIVABLE_ONLY: Final = (
	"Assigned to Bank for Debt Purchase and Debt Purchase Settled are only valid for Receivable cheques."
)

PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_CLEAR: Final = (
	"Cheques Assigned to Bank for Debt Purchase cannot be Cleared. "
	"Use Debt Purchase Settled via Facility Repayment, or Bounce the cheque."
)

PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_RETURN: Final = (
	"Cheques Assigned to Bank for Debt Purchase cannot be Returned to the party. "
	"Use Return from Debt Purchase to restore Registered (cheques in hand), "
	"Bounce Cheque if the bank rejects the instrument, or settle via Facility Repayment."
)

PDC_VALIDATION_DEBT_PURCHASE_SETTLED_FACILITY_ONLY: Final = (
	"Debt Purchase Settled is only allowed via Facility Repayment (Debt Purchase Cheque method)."
)

PDC_VALIDATION_CLEARED_IS_TERMINAL: Final = (
	"Cleared is a terminal Workflow State. Further changes to Workflow State are not allowed."
)

PDC_VALIDATION_CANCELLED_IS_TERMINAL: Final = (
	"Cancelled is a terminal Workflow State. Further changes to Workflow State are not allowed."
)

PDC_VALIDATION_REPLACED_IS_TERMINAL: Final = (
	"Replaced is a terminal Workflow State. Further changes to Workflow State are not allowed."
)

PDC_VALIDATION_DEBT_PURCHASE_SETTLED_IS_TERMINAL: Final = (
	"Debt Purchase Settled is a terminal Workflow State. "
	"Cancel the linked Facility Repayment to restore Assigned to Bank for Debt Purchase."
)

PDC_TERMINAL_WORKFLOW_STATE_ERRORS: Final[dict[str, str]] = {
	WORKFLOW_CLEARED: PDC_VALIDATION_CLEARED_IS_TERMINAL,
	WORKFLOW_CANCELLED: PDC_VALIDATION_CANCELLED_IS_TERMINAL,
	WORKFLOW_REPLACED: PDC_VALIDATION_REPLACED_IS_TERMINAL,
	WORKFLOW_DEBT_PURCHASE_SETTLED: PDC_VALIDATION_DEBT_PURCHASE_SETTLED_IS_TERMINAL,
}

PDC_TERMINAL_WORKFLOW_STATES: Final[frozenset[str]] = frozenset(PDC_TERMINAL_WORKFLOW_STATE_ERRORS.keys())


class PDCWorkflowTransitionSource(Protocol):
	"""Minimal shape for :func:`get_allowed_transitions`.

	Any object with these attributes works (Frappe Document, ``types.SimpleNamespace``,
	or a test double). Missing attributes are treated like ``None``.
	"""

	cheque_direction: str | None
	workflow_state: str | None


def get_workflow_transition_map(cheque_direction: str) -> dict[str, frozenset[str]]:
	"""Return the transition table for the given cheque direction.

	Raises:
		ValueError: if ``cheque_direction`` is not Receivable or Payable.
	"""
	if cheque_direction not in _TRANSITION_MAP:
		raise ValueError(
			f"Invalid cheque_direction {cheque_direction!r}; "
			f"expected {CHEQUE_DIRECTION_RECEIVABLE!r} or {CHEQUE_DIRECTION_PAYABLE!r}."
		)
	return _TRANSITION_MAP[cheque_direction]


def get_allowed_workflow_targets(cheque_direction: str, from_state: str) -> frozenset[str]:
	"""Return allowed ``workflow_state`` values when moving from ``from_state``."""
	table = get_workflow_transition_map(cheque_direction)
	return table.get(from_state, frozenset())


def get_allowed_next_workflow_states(
	cheque_direction: str | None,
	workflow_state: str | None,
) -> list[str]:
	"""Return valid next ``workflow_state`` labels for a direction and current state.

	Pure function: pass primitives (no Frappe document required). Suitable for unit
	tests and for building dropdowns / client hints when you already have
	``cheque_direction`` and ``workflow_state`` strings.

	``workflow_state`` is normalized via :func:`normalize_workflow_state_value`.
	Invalid or missing ``cheque_direction`` yields an empty list.

	When the current state is **terminal** (:data:`PDC_TERMINAL_WORKFLOW_STATES`), returns
	an empty list (no further transitions).

	The result is a **sorted list of distinct targets**; it excludes the \"stay put\"
	same-state case (allowed by :func:`is_workflow_transition_allowed` but not a
	\"transition\").
	"""
	if cheque_direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
		return []
	current = normalize_workflow_state_value(workflow_state)
	if current in PDC_TERMINAL_WORKFLOW_STATES:
		return []
	return sorted(get_allowed_workflow_targets(cheque_direction, current))


def get_allowed_transitions(doc: PDCWorkflowTransitionSource) -> list[str]:
	"""Return valid next ``workflow_state`` labels from a PDC-shaped object.

	Based only on ``cheque_direction`` and current ``workflow_state`` (after
	:func:`normalize_workflow_state_value`), using the same transition tables as
	:func:`is_workflow_transition_allowed`. The result is a sorted list; terminal
	current states yield an empty list.

	For **unit tests without a document**, prefer :func:`get_allowed_next_workflow_states`
	(primitive arguments); use this when exercising the doc-shaped entry point.
	"""
	return get_allowed_next_workflow_states(
		getattr(doc, "cheque_direction", None),
		getattr(doc, "workflow_state", None),
	)


def get_receivable_accounting_decision(
	from_state: str,
	to_state: str,
) -> str | None:
	"""Return accounting decision for a Receivable PDC workflow transition.

	Decisions:

	* ``journal_entry``  – create / adjust Journal Entry
	* ``no_document``    – no accounting document is created (business-only move)

	Returns:
		The decision string, or ``None`` when no explicit rule is defined.
	"""
	key = (from_state, to_state)
	return _RECEIVABLE_ACCOUNTING_DECISIONS.get(key)


def get_pdc_accounting_decision(
	cheque_direction: str,
	from_state: str,
	to_state: str,
) -> str | None:
	"""Return which accounting document type should follow this *successful* transition.

	Args:
		from_state / to_state: Normalized labels (use :func:`normalize_workflow_state_value` if raw).

	Returns:
		One of ``journal_entry``, ``no_document``, or ``None`` if there is
		no explicit policy row for this edge (caller may treat as “no auto document” —
		``post_dated_cheque.get_accounting_action`` maps ``None`` → ``no_document``).

	Raises:
		Nothing; unknown ``cheque_direction`` yields ``None``.
	"""
	if cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
		return get_receivable_accounting_decision(from_state, to_state)
	if cheque_direction == CHEQUE_DIRECTION_PAYABLE:
		return _PAYABLE_ACCOUNTING_DECISIONS.get((from_state, to_state))
	return None


def is_workflow_transition_allowed(cheque_direction: str, from_state: str, to_state: str) -> bool:
	"""Return True if changing ``workflow_state`` from ``from_state`` to ``to_state`` is allowed.

	Same-state (no change) is always allowed.
	**Cleared**, **Cancelled**, and **Replaced** are terminal: no transition away from
	those states (to a different state) is allowed.
	"""
	if from_state == to_state:
		return True
	if from_state in PDC_TERMINAL_WORKFLOW_STATES and to_state != from_state:
		return False
	allowed = get_allowed_workflow_targets(cheque_direction, from_state)
	return to_state in allowed


def normalize_workflow_state_value(value: str | None) -> str:
	"""Normalize stored/user input so blank and new rows compare equal to **Draft** (implicit first state)."""
	if value is None:
		return WORKFLOW_DRAFT
	s = str(value).strip()
	return s if s else WORKFLOW_DRAFT


def is_terminal_workflow_state(workflow_state: str | None) -> bool:
	"""True when ``workflow_state`` (after :func:`normalize_workflow_state_value`) is terminal."""
	return normalize_workflow_state_value(workflow_state) in PDC_TERMINAL_WORKFLOW_STATES


def is_workflow_previous_empty(previous: str | None) -> bool:
	"""True when there is no stored previous workflow state (new document or unset in DB)."""
	if previous is None:
		return True
	return not str(previous).strip()


def get_pdc_workflow_transition_validation_error(
	cheque_type: str,
	previous_workflow_state_raw: str | None,
	new_workflow_state_raw: str | None,
) -> str | None:
	"""Validate a ``workflow_state`` change for Post Dated Cheque.

	``cheque_type`` is the DocType field ``cheque_direction`` (**Receivable** / **Payable**) —
	the cheque category used to pick ``RECEIVABLE_WORKFLOW_TRANSITIONS`` vs
	``PAYABLE_WORKFLOW_TRANSITIONS``.

	Rules:

	* New value must be one of :data:`ALL_WORKFLOW_STATES` (after
	  :func:`normalize_workflow_state_value`).
	* If ``previous_workflow_state_raw`` is empty (``None`` or whitespace-only), only
	  **Draft** is allowed — no jump to Registered etc. until a stored state exists.
	* **Bounced** is only allowed for **Receivable** cheques and only when the previous
	  state is **Sent to Bank**, or when staying on **Bounced** (no change). Otherwise
	  returns :data:`PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK`.
	* **Endorsed** is only valid for **Receivable** cheques; **Payable** with
	  **Endorsed** returns :data:`PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY`.
	* **Issued** is only valid for **Payable** cheques; **Receivable** with
	  **Issued** returns :data:`PDC_VALIDATION_ISSUED_PAYABLE_ONLY`.
	* **Sent to Bank** is only valid for **Receivable** cheques; **Payable** with
	  **Sent to Bank** returns :data:`PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY`.
	* **Cleared**, **Cancelled**, and **Replaced** are **terminal** (for both Receivable
	  and Payable): evaluated **immediately after** validating the new value against
	  :data:`ALL_WORKFLOW_STATES`, **before** ``cheque_direction``-specific rules. If a
	  stored previous state exists and is terminal, the new state must be the same;
	  otherwise returns :data:`PDC_VALIDATION_CLEARED_IS_TERMINAL`,
	  :data:`PDC_VALIDATION_CANCELLED_IS_TERMINAL`, or :data:`PDC_VALIDATION_REPLACED_IS_TERMINAL`
	  as appropriate. This applies even when ``cheque_type`` is not Receivable/Payable
	  (pass a non-matching ``cheque_type`` so only terminal + unknown checks run).
	* Otherwise, the change must satisfy :func:`is_workflow_transition_allowed` for that
	  ``cheque_type``.

	Returns:
		``None`` if valid, else an English message suitable for ``frappe.throw(frappe._(msg))``.
	"""
	if getattr(frappe.flags, "in_pdc_workflow_rollback", None):
		return None
	if getattr(frappe.flags, "in_debt_purchase_facility_settlement", None):
		return None
	curr = normalize_workflow_state_value(new_workflow_state_raw)
	if curr not in ALL_WORKFLOW_STATES:
		return f"Invalid Workflow State {curr!r}. Must be one of: " f"{', '.join(ALL_WORKFLOW_STATES)}."

	had_previous = not is_workflow_previous_empty(previous_workflow_state_raw)
	prev = normalize_workflow_state_value(previous_workflow_state_raw) if had_previous else None
	if had_previous and prev is not None:
		term_err = PDC_TERMINAL_WORKFLOW_STATE_ERRORS.get(prev)
		if term_err is not None and curr != prev:
			return term_err

	# Endorsed (Receivable): no company bank collection — block the two operational paths explicitly.
	if (
		cheque_type == CHEQUE_DIRECTION_RECEIVABLE
		and had_previous
		and prev == WORKFLOW_ENDORSED
		and curr != WORKFLOW_ENDORSED
		and curr in (WORKFLOW_SENT_TO_BANK, WORKFLOW_CLEARED)
	):
		return PDC_VALIDATION_ENDORSED_NO_BANK_CLEAR

	# Debt Purchase Assigned: forbid Cleared / Returned (party return). Bounce, Return from
	# Debt Purchase → Registered, and Facility Settled remain the supported exits.
	if (
		cheque_type == CHEQUE_DIRECTION_RECEIVABLE
		and had_previous
		and prev == WORKFLOW_ASSIGNED_DEBT_PURCHASE
		and curr != WORKFLOW_ASSIGNED_DEBT_PURCHASE
	):
		if curr == WORKFLOW_CLEARED:
			return PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_CLEAR
		if curr == WORKFLOW_RETURNED:
			return PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_RETURN

	# Debt Purchase Settled only via Facility Repayment (flag bypasses this validator).
	if (
		cheque_type == CHEQUE_DIRECTION_RECEIVABLE
		and had_previous
		and prev == WORKFLOW_ASSIGNED_DEBT_PURCHASE
		and curr == WORKFLOW_DEBT_PURCHASE_SETTLED
	):
		return PDC_VALIDATION_DEBT_PURCHASE_SETTLED_FACILITY_ONLY

	# Bounced: Receivable only; previous must be Sent to Bank, Assigned DP, or unchanged Bounced.
	# Runs before ``cheque_type not in _TRANSITION_MAP`` so unset/invalid direction still rejects Bounced.
	if curr == WORKFLOW_BOUNCED:
		if cheque_type != CHEQUE_DIRECTION_RECEIVABLE:
			return PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK
		if had_previous and prev is not None:
			if prev not in (WORKFLOW_SENT_TO_BANK, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED):
				return PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK
	# Endorsed: Receivable only (before generic cheque_type gate).
	if curr == WORKFLOW_ENDORSED and cheque_type == CHEQUE_DIRECTION_PAYABLE:
		return PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY

	# Issued: Payable only (before generic cheque_type gate).
	if curr == WORKFLOW_ISSUED and cheque_type == CHEQUE_DIRECTION_RECEIVABLE:
		return PDC_VALIDATION_ISSUED_PAYABLE_ONLY

	# Sent to Bank: Receivable only (before generic cheque_type gate).
	if curr == WORKFLOW_SENT_TO_BANK and cheque_type == CHEQUE_DIRECTION_PAYABLE:
		return PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY

	# Debt Purchase states: Receivable only.
	if curr in (WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_DEBT_PURCHASE_SETTLED) and cheque_type == CHEQUE_DIRECTION_PAYABLE:
		return PDC_VALIDATION_DEBT_PURCHASE_RECEIVABLE_ONLY

	if cheque_type not in _TRANSITION_MAP:
		return None

	if not had_previous:
		if curr != WORKFLOW_DRAFT:
			return (
				f"When Workflow State was not set yet, only {WORKFLOW_DRAFT} is allowed. "
				f"Cannot set Workflow State to {curr}."
			)
		return None
	if is_workflow_transition_allowed(cheque_type, prev, curr):
		return None
	allowed = get_allowed_workflow_targets(cheque_type, prev)
	allowed_msg = ", ".join(sorted(allowed)) if allowed else "(none)"
	direction_label = "Receivable" if cheque_type == CHEQUE_DIRECTION_RECEIVABLE else "Payable"
	return (
		f"Invalid Workflow State transition for {direction_label} cheque: "
		f"from {prev} to {curr}. Allowed next states: {allowed_msg}"
	)


__all__ = [
	"ALL_WORKFLOW_STATES",
	"CHEQUE_DIRECTION_PAYABLE",
	"CHEQUE_DIRECTION_RECEIVABLE",
	"PDC_ACCOUNTING_JOURNAL_ENTRY",
	"PDC_ACCOUNTING_NO_DOCUMENT",
	"PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK",
	"PDC_VALIDATION_CANCELLED_IS_TERMINAL",
	"PDC_VALIDATION_CLEARED_IS_TERMINAL",
	"PDC_VALIDATION_ENDORSED_NO_BANK_CLEAR",
	"PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY",
	"PDC_VALIDATION_ISSUED_PAYABLE_ONLY",
	"PDC_VALIDATION_REPLACED_IS_TERMINAL",
	"PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY",
	"PDC_VALIDATION_DEBT_PURCHASE_RECEIVABLE_ONLY",
	"PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_CLEAR",
	"PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_RETURN",
	"PDC_VALIDATION_DEBT_PURCHASE_SETTLED_FACILITY_ONLY",
	"PDC_VALIDATION_DEBT_PURCHASE_SETTLED_IS_TERMINAL",
	"PDC_TERMINAL_WORKFLOW_STATE_ERRORS",
	"PDC_TERMINAL_WORKFLOW_STATES",
	"PAYABLE_WORKFLOW_TRANSITIONS",
	"RECEIVABLE_WORKFLOW_TRANSITIONS",
	"WORKFLOW_ASSIGNED_DEBT_PURCHASE",
	"WORKFLOW_BOUNCED",
	"WORKFLOW_CANCELLED",
	"WORKFLOW_CLEARED",
	"WORKFLOW_DEBT_PURCHASE_SETTLED",
	"WORKFLOW_DRAFT",
	"WORKFLOW_ENDORSED",
	"WORKFLOW_ISSUED",
	"WORKFLOW_REGISTERED",
	"WORKFLOW_REPLACED",
	"WORKFLOW_RETURNED",
	"WORKFLOW_SENT_TO_BANK",
	"WORKFLOW_UNDER_LEGAL_ACTION",
	"PDCWorkflowTransitionSource",
	"get_allowed_next_workflow_states",
	"get_allowed_transitions",
	"get_allowed_workflow_targets",
	"get_pdc_accounting_decision",
	"get_receivable_accounting_decision",
	"get_pdc_workflow_transition_validation_error",
	"get_workflow_transition_map",
	"is_terminal_workflow_state",
	"is_workflow_previous_empty",
	"is_workflow_transition_allowed",
	"normalize_workflow_state_value",
]
