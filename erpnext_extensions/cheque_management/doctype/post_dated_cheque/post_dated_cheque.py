# Copyright (c) 2025, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Post Dated Cheque **Document** controller.

**Module map (concerns are split across files)**

* **Workflow graph & accounting policy** — ``pdc_workflow_state_machine`` (allowed transitions,
  ``get_pdc_accounting_decision`` → journal vs no document).
* **``cheque_status`` labels** — ``pdc_workflow_to_cheque_status`` (maps ``workflow_state`` + direction).
* **JE payloads (no GL insert here)** — ``build_pdc_journal_entry_data`` in this module; receivable
  clearing helpers in ``pdc_receivable_accounting``.
* **Allocation rows (planning / reporting only)** — ``pdc_allocation`` (summary sync + row validation).
* **Idempotency keys & JE posting** — ``pdc_accounting_idempotency``, ``pdc_journal_entry_service``.

**Journal-centric lifecycle**

All workflow GL posting uses **Journal Entry** only. **Payment Entry** is not part of the PDC
lifecycle architecture.

**Business rules**

* **Receivable:** party (AR) only on real Receivable settlement rows at **Registered** /
  **Returned** (and AR replacement). **Cleared / Sent to Bank / Bounce:** bank, CIH, clearing, and
  protested lines carry **no** Party.
* **Payable:** party (AP) only on real Payable settlement rows (register / reverse / return).
  **Cleared:** Dr notes-payable pool, Cr bank — **no** Party on either line.
* **Allocations** drive PI references on payable **party** lines only; see ``pdc_payable_purchase_invoice_je_refs``.

**Document hooks**

* ``validate`` / ``on_update`` — :meth:`_pdc_pre_save_workflow_sequence`, :meth:`_pdc_post_save_accounting_sequence`.
* ``journal_references`` — one row per posted JE; ``holder_history`` — receivable endorsement audit.

Design reference: ``PDC_DESIGN_FINAL_FA.md``; English notes: ``../../DEVELOPER.md``.
"""

import logging

import frappe
from frappe import validate_and_sanitize_search_inputs
from frappe.model.document import Document
from frappe.utils import cint, cstr, getdate, now_datetime

from erpnext_extensions.cheque_management.pdc_allocation import (
	ALLOCATION_MODE_ADVANCE,
	apply_pdc_allocation_row_defaults_from_parent,
	autofill_pdc_allocations_from_parent_reference,
	pdc_allocation_effective_milestone_workflow_state,
	sanitize_pdc_allocation_child_rows,
	sync_pdc_allocation_summary_amounts,
	sync_single_pdc_allocation_on_reduced_cheque_amount,
	validate_pdc_allocation_rows,
	validate_pdc_allocation_workflow_milestone,
	validate_post_dated_cheque_allocation_mode_immutability,
)
from erpnext_extensions.cheque_management.pdc_allocation import (
	is_pdc_allocation_draft_only as _is_pdc_allocation_draft_only,
)
from erpnext_extensions.cheque_management.pdc_allocation import (
	is_pdc_allocation_effective as _is_pdc_allocation_effective,
)
from erpnext_extensions.cheque_management.pdc_payable_purchase_invoice_je_refs import (
	payable_purchase_invoice_settlement_slices,
)
from erpnext_extensions.cheque_management.pdc_receivable_accounting import (
	receivable_intermediary_account_for_bank_clear,
)
from erpnext_extensions.cheque_management.pdc_receivable_sales_invoice_je_refs import (
	receivable_sales_invoice_settlement_slices,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	PDC_ACCOUNTING_JOURNAL_ENTRY,
	PDC_ACCOUNTING_NO_DOCUMENT,
	PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK,
	PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY,
	PDC_VALIDATION_ISSUED_PAYABLE_ONLY,
	PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY,
	WORKFLOW_BOUNCED,
	WORKFLOW_CANCELLED,
	WORKFLOW_CLEARED,
	WORKFLOW_DRAFT,
	WORKFLOW_ENDORSED,
	WORKFLOW_ISSUED,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	WORKFLOW_REGISTERED,
	WORKFLOW_REPLACED,
	WORKFLOW_RETURNED,
	WORKFLOW_SENT_TO_BANK,
	WORKFLOW_UNDER_LEGAL_ACTION,
	get_pdc_accounting_decision,
	get_pdc_workflow_transition_validation_error,
	is_workflow_previous_empty,
	normalize_workflow_state_value,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	CHEQUE_STATUS_RETURNED_FROM_PAYEE,
	CHEQUE_STATUS_RETURNED_TO_CUSTOMER,
	map_workflow_state_to_cheque_status,
)
from erpnext_extensions.cheque_management.utils.descriptions import (
	PDCDescriptionContext,
	render_pdc_je_text,
)

_pdc_accounting_logger = logging.getLogger("erpnext_extensions.cheque_management")


def _log_debug_journal_entry_payload(doc, from_state: str, to_state: str, payload: dict | None) -> None:
	"""Optional debug logging for JE payloads (duplicate-party investigations)."""
	if not payload:
		return
	rows = payload.get("accounts") or []
	party_lines = [
		(i, r.get("account"), r.get("party_type"), r.get("party"))
		for i, r in enumerate(rows)
		if r.get("party_type") or r.get("party")
	]
	_pdc_accounting_logger.info(
		"[PDC_ACCOUNTING_TRACE] JE payload | pdc=%s | cheque_direction=%s | from=%s | to=%s | "
		"party_on_lines=%s | accounts=%s",
		getattr(doc, "name", None),
		getattr(doc, "cheque_direction", None),
		from_state,
		to_state,
		party_lines,
		[r.get("account") for r in rows],
	)


def _strip_link_name_or_none(value) -> str | None:
	"""Return stripped Link / Dynamic Link value, or ``None`` if empty."""
	if not value:
		return None
	s = str(value).strip()
	return s or None


# Holder History child row reason when workflow moves to Endorsed (Receivable).
PDC_HOLDER_HISTORY_REASON_ENDORSEMENT = "Endorsement — transfer to new holder"

# Journal Entry remarks — Receivable Draft → Registered
PDC_JE_REMARK_REGISTER_RECEIVABLE_CHEQUE = "Register receivable cheque"
# Journal Entry remarks — Receivable Registered → Sent to Bank
PDC_JE_REMARK_SEND_RECEIVABLE_CHEQUE_TO_BANK = "Send receivable cheque to bank"
# Journal Entry remarks — Receivable Sent to Bank → Registered (Return from Bank)
PDC_JE_REMARK_RETURN_RECEIVABLE_FROM_BANK = "Return receivable cheque from bank to cashbox"
# Journal Entry remarks — Receivable Registered → Assigned to Bank for Debt Purchase
PDC_JE_REMARK_ASSIGN_RECEIVABLE_DEBT_PURCHASE = "Assign receivable cheque for debt purchase"
# Journal Entry remarks — Receivable Assigned DP → Returned
PDC_JE_REMARK_RETURN_DEBT_PURCHASE_RECEIVABLE = "Return debt purchase assigned cheque to party"
# Journal Entry remarks — Receivable Sent to Bank → Bounced
PDC_JE_REMARK_RECEIVABLE_CHEQUE_BOUNCED = "Receivable cheque bounced"
# Journal Entry remarks — Receivable Registered → Returned
PDC_JE_REMARK_RETURN_RECEIVABLE_CHEQUE_TO_PARTY = "Return receivable cheque to party"
# Journal Entry remarks — Receivable Registered → Endorsed
PDC_JE_REMARK_ENDORSE_RECEIVABLE_CHEQUE = "Endorse receivable cheque"
# Journal Entry remarks — Payable Draft → Registered (supplier / PI settlement)
PDC_JE_REMARK_REGISTER_PAYABLE_CHEQUE = "Register payable cheque (supplier settlement)"
# Legacy label kept for older remarks / docs; new postings use REGISTER variant above.
PDC_JE_REMARK_ISSUE_PAYABLE_CHEQUE = "Issue payable cheque"
# Journal Entry remarks — Payable Issued → Returned
PDC_JE_REMARK_RETURNED_PAYABLE_CHEQUE_FROM_PAYEE = "Returned payable cheque from payee"
# Journal Entry remarks — Payable Issued → Cancelled
PDC_JE_REMARK_CANCEL_ISSUED_PAYABLE_CHEQUE = "Cancel issued payable cheque"
# Journal Entry remarks — replacement transitions (see TODO(accounting) in builder)
PDC_JE_REMARK_REPLACE_RECEIVABLE_AFTER_BOUNCE = "Replace receivable cheque after bank bounce"
PDC_JE_REMARK_REPLACE_RECEIVABLE_AFTER_RETURN = "Replace receivable cheque after return"
PDC_JE_REMARK_REPLACE_ISSUED_PAYABLE_CHEQUE = "Replace issued payable cheque"
PDC_JE_REMARK_REPLACE_RETURNED_PAYABLE_CHEQUE = "Replace returned payable cheque"
PDC_JE_REMARK_CLEAR_PAYABLE_CHEQUE = "Clear payable cheque"
# Journal Entry remarks — Payable Registered → Cancelled (reverse register settlement)
PDC_JE_REMARK_CANCEL_REGISTERED_PAYABLE_CHEQUE = (
	"Cancel registered payable cheque (reverse supplier settlement)"
)

# Journal Entry remarks — Receivable → Cleared (Dr bank, Cr intermediary; no party)
PDC_JE_REMARK_CLEAR_RECEIVABLE_REGISTERED = (
	"Receivable PDC cleared at bank vs cheques in hand (no party on clear)."
)
PDC_JE_REMARK_CLEAR_RECEIVABLE_CLEARING = (
	"Receivable PDC cleared at bank vs cheques in clearing (no party on clear)."
)
PDC_JE_REMARK_CLEAR_RECEIVABLE_LEGAL = (
	"Receivable PDC cleared at bank after legal follow-up vs protested/clearing/in-hand (no party on clear)."
)
#
# NOTE: This app does not use Payment Entry for PDC lifecycle.


def _resolve_holder_party_type_and_party(doc) -> tuple[str | None, str | None]:
	"""Holder for endorsement / display: ``holder_party*`` if set, else drawer ``party*``."""
	if not doc:
		return None, None
	if isinstance(doc, dict):
		ht = doc.get("holder_party_type") or doc.get("party_type")
		hp = doc.get("holder_party") or doc.get("party")
	else:
		ht = doc.holder_party_type or doc.party_type
		hp = doc.holder_party or doc.party
	return _strip_link_name_or_none(ht), _strip_link_name_or_none(hp)


def get_accounting_action(doc, previous_workflow_state: str | None) -> str:
	"""Return which accounting artefact applies for the transition into the current save.

	PDC lifecycle is **Journal Entry only** — expect ``journal_entry`` or ``no_document`` only.

	Combines ``doc.cheque_direction``, ``previous_workflow_state`` (workflow before this
	change), and ``doc.workflow_state`` (target). Delegates to
	:func:`erpnext_extensions.cheque_management.pdc_workflow_state_machine.get_pdc_accounting_decision`.
	States are normalized with :func:`normalize_workflow_state_value` (blank/``None``
	→ **Draft**).

	When the state machine has no explicit rule for the edge (``None`` from
	``get_pdc_accounting_decision``), returns ``no_document`` — same default as
	undefined Payable/Receivable policy.

	Returns:
		One of ``journal_entry`` or ``no_document`` (see ``PDC_ACCOUNTING_*`` in
		``pdc_workflow_state_machine.py``).
	"""
	cheque_direction = getattr(doc, "cheque_direction", None) or ""
	to_state = normalize_workflow_state_value(getattr(doc, "workflow_state", None))
	from_state = normalize_workflow_state_value(previous_workflow_state)
	decision = get_pdc_accounting_decision(cheque_direction, from_state, to_state)
	# Task 5: Advance-mode recognition may post on operational-only edges when configured (Payable issue stage).
	if (
		(getattr(doc, "allocation_mode", None) or "").strip() == ALLOCATION_MODE_ADVANCE
		and (cheque_direction or "").strip() == CHEQUE_DIRECTION_PAYABLE
		and (getattr(doc, "effective_stage_for_advance_recognition", None) or "register").strip().lower()
		== "issue"
		and from_state == WORKFLOW_REGISTERED
		and to_state == WORKFLOW_ISSUED
	):
		decision = PDC_ACCOUNTING_JOURNAL_ENTRY
	# Enforce lifecycle rule: selector can only yield journal_entry or no_document.
	return (
		PDC_ACCOUNTING_JOURNAL_ENTRY
		if decision == PDC_ACCOUNTING_JOURNAL_ENTRY
		else PDC_ACCOUNTING_NO_DOCUMENT
	)


def _get_party_account_or_company_default(party_type, party, company, account_kind="receivable"):
	"""Get party account; fallback to company default. account_kind: receivable or payable."""
	# ERPNext's party account helper is primarily designed for Customer/Supplier.
	# For Employee/Shareholder we intentionally fallback to company defaults.
	if party_type in ("Employee", "Shareholder"):
		if account_kind == "receivable":
			return frappe.get_cached_value("Company", company, "default_receivable_account")
		return frappe.get_cached_value("Company", company, "default_payable_account")
	try:
		from erpnext.accounts.party import get_party_account

		account = get_party_account(party_type, party, company)
		if account:
			return account
	except Exception:
		pass
	# Fallback: company default
	if account_kind == "receivable":
		return frappe.get_cached_value("Company", company, "default_receivable_account")
	return frappe.get_cached_value("Company", company, "default_payable_account")


def _get_cheques_in_hand_account_for_company(company):
	"""PDC Settings: Cheques in Hand account for company, or None if not configured."""
	if not company:
		return None
	settings_name = frappe.db.get_value("PDC Settings", {"company": company}, "name") or company
	if not settings_name or not frappe.db.exists("PDC Settings", settings_name):
		return None
	return frappe.db.get_value("PDC Settings", settings_name, "default_cheques_in_hand_account")


def _get_pdc_settings_for_company(company: str):
	"""Fetch ``PDC Settings`` doc for a given company (by name or company field)."""
	if not company:
		return None
	name = frappe.db.get_value("PDC Settings", {"company": company}, "name") or company
	if not name or not frappe.db.exists("PDC Settings", name):
		return None
	return frappe.get_doc("PDC Settings", name)


def _company_default_advance_paid_account(company: str | None) -> str | None:
	"""Company default account for supplier advances (ERPNext Company.default_advance_paid_account)."""
	co = (company or "").strip()
	if not co:
		return None
	val = frappe.db.get_value("Company", co, "default_advance_paid_account")
	s = (val or "").strip()
	return s or None


def _company_default_advance_received_account(company: str | None) -> str | None:
	"""Company default account for customer advances (ERPNext Company.default_advance_received_account)."""
	co = (company or "").strip()
	if not co:
		return None
	val = frappe.db.get_value("Company", co, "default_advance_received_account")
	s = (val or "").strip()
	return s or None


def _resolve_advance_account_override(doc) -> str | None:
	"""Return doc-level override for advance/prepayment account (if any)."""
	return _strip_link_name_or_none(getattr(doc, "advance_account", None) if doc else None)


def _validate_advance_account_override(doc) -> None:
	"""Validate advance_account when user sets it (tolerant, company-aware).

	We intentionally validate only basic Account invariants + root_type alignment hints:
	- same company (unless account has no company)
	- not disabled
	- not a group
	- root_type alignment:
	  - Receivable advance typically uses a Liability (advance received)
	  - Payable advance typically uses an Asset (advance paid)

	We keep this tolerant: if root_type is empty/unknown, we do not block.
	"""
	acc = _resolve_advance_account_override(doc)
	if not acc:
		return

	company = (getattr(doc, "company", None) or "").strip()
	row = frappe.db.get_value(
		"Account",
		acc,
		["company", "disabled", "is_group", "root_type"],
		as_dict=True,
	)
	if not row:
		frappe.throw(
			frappe._("Advance / Prepayment Account {0} does not exist.").format(acc),
			title=frappe._("Account"),
		)

	if cint(row.get("disabled", 0)):
		frappe.throw(
			frappe._("Advance / Prepayment Account {0} is disabled.").format(acc),
			title=frappe._("Account"),
		)

	if cint(row.get("is_group", 0)):
		frappe.throw(
			frappe._("Advance / Prepayment Account {0} must not be a group account.").format(acc),
			title=frappe._("Account"),
		)

	acc_company = (row.get("company") or "").strip()
	if company and acc_company and acc_company != company:
		frappe.throw(
			frappe._(
				"Advance / Prepayment Account must belong to the same Company as this Post Dated Cheque."
			),
			title=frappe._("Account"),
		)

	root_type = (row.get("root_type") or "").strip()
	if not root_type:
		return

	direction = (getattr(doc, "cheque_direction", None) or "").strip()
	# Only validate alignment for advance mode; non-advance flows ignore this field.
	if (getattr(doc, "allocation_mode", None) or "").strip() != ALLOCATION_MODE_ADVANCE:
		return

	if direction == CHEQUE_DIRECTION_RECEIVABLE and root_type not in ("Liability",):
		frappe.throw(
			frappe._(
				"Advance / Prepayment Account root type should typically be Liability for receivable advances (advance received)."
			),
			title=frappe._("Account"),
		)
	if direction == CHEQUE_DIRECTION_PAYABLE and root_type not in ("Asset",):
		frappe.throw(
			frappe._(
				"Advance / Prepayment Account root type should typically be Asset for payable advances (advance paid)."
			),
			title=frappe._("Account"),
		)


def _pdc_company_policy_flags(company: str | None) -> dict[str, int]:
	"""Runtime flags from PDC Settings (defaults match DocType defaults when no row exists)."""
	st = _get_pdc_settings_for_company(company) if company else None
	if not st:
		return {"allow_endorsement": 1, "require_sayad_registration": 0}
	return {
		"allow_endorsement": cint(st.get("allow_endorsement", 1)),
		"require_sayad_registration": cint(st.get("require_sayad_registration", 0)),
	}


def resolve_pdc_accounts_for_journal(doc, settings=None):
	"""Resolve GL accounts for PDC Journal Entry lines from **PDC Settings** with document fallbacks.

	Uses these settings fields when set:

	* ``default_cheques_in_hand_account``
	* ``default_cheques_in_clearing_account``
	* ``default_payable_cheque_account``
	* ``default_protested_account``
	* ``default_endorsement_account`` (receivable endorsement debit when no per-PDC override)

	Fallbacks (when the setting is empty):

	* **Cheques in Hand** → ``doc.account_paid_to`` (Receivable: operational Cheques in Hand side).
	* **Endorsement debit** → ``doc.endorsement_settlement_account`` if set (takes precedence over setting);
	  else holder receivable when endorsing to a different party than ``party``.
	* Other roles have no DocType counterpart and remain unset unless configured in **PDC Settings**.

	:param doc: :class:`~frappe.model.document.Document` **Post Dated Cheque** (or any object with
		``company``, ``account_paid_to``, etc.).
	:param settings: optional **PDC Settings** document; if omitted, loaded from ``doc.company``.

	Returns:
		Dict with keys: ``cheques_in_hand``, ``cheques_in_clearing``, ``payable_cheque``,
		``protested``, ``endorsement_account`` — each value is an Account name or ``None``.
	"""
	if settings is None and doc is not None:
		company = getattr(doc, "company", None)
		settings = _get_pdc_settings_for_company(company) if company else None

	def _s(field: str) -> str | None:
		if not settings:
			return None
		return _strip_link_name_or_none(settings.get(field))

	def _payable_pool_account() -> str | None:
		"""Resolve payable cheque pool account with required priority.

		Priority:
		1) ``doc.account_paid_from`` when explicitly set on the PDC (payable only)
		2) ``settings.default_payable_cheque_account``
		"""
		if (
			doc is not None
			and (getattr(doc, "cheque_direction", None) or "").strip() == CHEQUE_DIRECTION_PAYABLE
		):
			explicit = _strip_link_name_or_none(getattr(doc, "account_paid_from", None))
			if explicit:
				return explicit
		return _s("default_payable_cheque_account")

	out = {
		"cheques_in_hand": _s("default_cheques_in_hand_account")
		or _strip_link_name_or_none(getattr(doc, "account_paid_to", None) if doc else None),
		"cheques_in_clearing": _strip_link_name_or_none(
			getattr(doc, "cheques_in_clearing_account", None) if doc else None
		)
		or _s("default_cheques_in_clearing_account"),
		"payable_cheque": _payable_pool_account(),
		"protested": _s("default_protested_account"),
		"endorsement_account": _s("default_endorsement_account"),
		"debt_purchase_in_collection": _s("default_debt_purchase_in_collection_account"),
	}
	return out


def _pdc_party_dims_for_doc(doc) -> dict[str, str]:
	"""Drawer/supplier party dimensions for single-party cheque events."""
	pt = _strip_link_name_or_none(getattr(doc, "party_type", None))
	p = _strip_link_name_or_none(getattr(doc, "party", None))
	if not pt or not p:
		return {}
	return {"party_type": pt, "party": p}


def _pdc_account_type(account: str | None) -> str | None:
	"""Return ERPNext Account.account_type, or None when unavailable / non-string."""
	acc = _strip_link_name_or_none(account)
	if not acc:
		return None
	try:
		at = frappe.get_cached_value("Account", acc, "account_type")
	except Exception:
		return None
	return at if isinstance(at, str) and at else None


def _pdc_account_allows_party(account: str | None) -> bool:
	"""Whether ERPNext Account Type is Receivable/Payable (strict COA typing).

	PDC intermediary accounts (CIH / Clearing / Protested / DPIC / Pool) may still carry
	Party for subledger circulation even when Account Type is blank — see
	:func:`_pdc_accounts_with_doc_party`.
	"""
	return _pdc_account_type(account) in ("Receivable", "Payable")


def _pdc_accounts_with_doc_party(
	accounts: list[dict],
	doc,
	*,
	bank_gl: str | None = None,
) -> list[dict]:
	"""Apply Finance Party policy: doc party on all lines except actual Bank rows.

	Mirrors ``doc.party_type`` / ``doc.party`` onto every JE line except:

	* the clear bank GL (``bank_gl``), and
	* accounts whose Account Type is **Bank**.

	Cash is **not** treated as Bank. Intermediary cheque accounts (CIH, Clearing, Pool,
	Protested, DPIC, …) keep Party. Endorsement skips this helper in ``_return_je``.
	"""
	dims = _pdc_party_dims_for_doc(doc)
	if not dims:
		return list(accounts)
	bank = _strip_link_name_or_none(bank_gl)
	out: list[dict] = []
	for row in accounts or []:
		entry = dict(row)
		acc = _strip_link_name_or_none(entry.get("account"))
		at = _pdc_account_type(acc)
		should_strip = bool(bank and acc == bank) or at == "Bank"
		if should_strip:
			entry.pop("party_type", None)
			entry.pop("party", None)
		else:
			entry.update(dims)
		out.append(entry)
	return out


def build_pdc_journal_entry_data(doc, from_state: str, to_state: str, posting_date=None):
	"""Prepare Journal Entry data dict for supported PDC workflow transitions.

	This builder is the **primary** vehicle for journal-centric lifecycle posting.
	Finance Party policy (via ``_pdc_accounts_with_doc_party``): drawer/supplier Party on
	all non-Bank lines for standard PDC transitions; Bank rows stay Party-free.
	**Endorsement** skips Party mirroring and keeps holder-only rules.
	**Cleared** keeps Party on the intermediary/pool line and strips it from the Bank GL.

	Does **not** insert any Journal Entry; only returns a structured payload:

	* ``voucher_type`` — ``\"Bank Entry\"`` for bank-facing clear transitions (``→ Cleared``),
	  otherwise ``\"Journal Entry\"``
	* ``posting_date`` — supplied value or today's date
	* ``remarks`` — user-facing description
	* ``accounts`` — list of debit/credit rows

	Replacement transitions (Receivable/Payable) use working defaults with ``TODO(accounting)``
	comments in-code; see design doc §9.1.

	Payable **Purchase Invoice** linkage on party/AP rows (register-settlement and symmetric reversals) comes from
	``pdc_payable_purchase_invoice_je_refs`` when allocation rows resolve to PI (direct or via PR).
	"""
	if not posting_date:
		posting_date = getdate()

	if doc.cheque_direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
		return None

	decision = get_pdc_accounting_decision(doc.cheque_direction, from_state, to_state)
	# Task 5: Advance-mode recognition may require posting on edges that are operational-only in the
	# direct-settlement lifecycle (e.g. Payable Registered → Issued when effective stage is `issue`).
	if (getattr(doc, "allocation_mode", None) or "").strip() == ALLOCATION_MODE_ADVANCE:
		eff = (getattr(doc, "effective_stage_for_advance_recognition", None) or "register").strip().lower()
		if (
			(doc.cheque_direction == CHEQUE_DIRECTION_PAYABLE)
			and eff == "issue"
			and from_state == WORKFLOW_REGISTERED
			and to_state == WORKFLOW_ISSUED
		):
			decision = "journal_entry"
	if decision != "journal_entry":
		# PE and no_document transitions must not go through the JE builder (avoids silent wrong accounts).
		return None

	settings = _get_pdc_settings_for_company(doc.company)
	acc = resolve_pdc_accounts_for_journal(doc, settings)
	ctx = PDCDescriptionContext.from_doc(doc, from_state=from_state, to_state=to_state)

	def _payable_party_issue_debit_lines(debit_account: str) -> list[dict]:
		slices = payable_purchase_invoice_settlement_slices(doc)
		if slices:
			return [
				{
					"account": debit_account,
					"debit_in_account_currency": amt,
					"party_type": doc.party_type,
					"party": doc.party,
					"reference_type": "Purchase Invoice",
					"reference_name": pnm,
				}
				for pnm, amt in slices
			]
		return [
			{
				"account": debit_account,
				"debit_in_account_currency": doc.cheque_amount,
				"party_type": doc.party_type,
				"party": doc.party,
			}
		]

	def _payable_party_reverse_credit_lines(credit_account: str) -> list[dict]:
		slices = payable_purchase_invoice_settlement_slices(doc)
		if slices:
			return [
				{
					"account": credit_account,
					"credit_in_account_currency": amt,
					"party_type": doc.party_type,
					"party": doc.party,
					"reference_type": "Purchase Invoice",
					"reference_name": pnm,
				}
				for pnm, amt in slices
			]
		return [
			{
				"account": credit_account,
				"credit_in_account_currency": doc.cheque_amount,
				"party_type": doc.party_type,
				"party": doc.party,
			}
		]

	def _base(remark: str) -> dict:
		voucher_type = "Bank Entry" if to_state == WORKFLOW_CLEARED else "Journal Entry"
		return {
			"voucher_type": voucher_type,
			"posting_date": posting_date,
			"remarks": remark,
			"accounts": [],
		}

	je: dict | None = None

	def _validate_receivable_party_integrity(payload: dict) -> None:
		"""Receivable Party policy: drawer Party on all non-Bank lines for standard edges.

		- Register / Return / Send to Bank / Bounce / DP Assignment / DP Bounce: every line
		  carries drawer Party.
		- Cleared: Bank line must not carry Party; non-bank intermediary must.
		- Endorsement: holder-only rules (unchanged; mirroring skipped in ``_return_je``).
		- Replacement edges: not redesigned here — leave builder + skip-mirror behavior.
		"""
		if doc.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		edge = (from_state, to_state)
		rows = payload.get("accounts") or []
		dims = _pdc_party_dims_for_doc(doc)
		exp_pt, exp_p = dims.get("party_type"), dims.get("party")

		def _row_has_doc_party(r: dict) -> bool:
			return (
				_strip_link_name_or_none(r.get("party_type")) == exp_pt
				and _strip_link_name_or_none(r.get("party")) == exp_p
			)

		def _row_has_any_party(r: dict) -> bool:
			return bool(r.get("party_type") or r.get("party"))

		if edge == (WORKFLOW_REGISTERED, WORKFLOW_ENDORSED):
			holder_pt = _strip_link_name_or_none(
				getattr(doc, "holder_party_type", None)
			) or _strip_link_name_or_none(getattr(doc, "party_type", None))
			holder_p = _strip_link_name_or_none(
				getattr(doc, "holder_party", None)
			) or _strip_link_name_or_none(getattr(doc, "party", None))
			for r in rows:
				pt, p = r.get("party_type"), r.get("party")
				if not pt and not p:
					continue
				if not holder_pt or not holder_p:
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Endorsement Journal Entry cannot use Party on lines without holder_party_type and holder_party on the PDC."
						),
						title=frappe._("PDC accounting integrity"),
					)
				if _strip_link_name_or_none(pt) != holder_pt or _strip_link_name_or_none(p) != holder_p:
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Endorsement may only post Party dimensions for the endorsed holder ({0} / {1}), not the drawer or any other party."
						).format(holder_pt, holder_p),
						title=frappe._("PDC accounting integrity"),
					)
			return

		# Debt Purchase bounce: account shape + Party on both rows (no invoice refs).
		if edge == (WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED):
			protested = (acc.get("protested") or "").strip() or None
			dpic = (acc.get("debt_purchase_in_collection") or "").strip() or None
			if not protested:
				frappe.throw(
					frappe._(
						"Default Protested Account is required in PDC Settings to bounce a Debt Purchase cheque. "
						"Configure Default Protested Account before Bounce Cheque."
					),
					title=frappe._("Missing Protested Account"),
				)
			if not dims:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			saw_prot_debit = False
			saw_dpic_credit = False
			for r in rows:
				if r.get("reference_type") or r.get("reference_name"):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Debt Purchase Bounce Journal Entry "
							"must not carry invoice references on any line ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				if not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Every Journal Entry line must carry the drawer Party for transition {0} → {1}."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				acc_name = _strip_link_name_or_none(r.get("account"))
				if float(r.get("debit_in_account_currency") or 0) > 0:
					if acc_name != protested:
						frappe.throw(
							frappe._(
								"Receivable PDC accounting integrity: Debt Purchase Bounce must debit "
								"Default Protested Account ({0}), not {1}."
							).format(protested, acc_name or "—"),
							title=frappe._("PDC accounting integrity"),
						)
					saw_prot_debit = True
				if float(r.get("credit_in_account_currency") or 0) > 0:
					if not dpic or acc_name != dpic:
						frappe.throw(
							frappe._(
								"Receivable PDC accounting integrity: Debt Purchase Bounce must credit "
								"Debt Purchase In Collection ({0}), not {1}."
							).format(dpic or "—", acc_name or "—"),
							title=frappe._("PDC accounting integrity"),
						)
					saw_dpic_credit = True
			if not saw_prot_debit or not saw_dpic_credit:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Debt Purchase Bounce requires "
						"Dr Protested and Cr Debt Purchase In Collection ({0} → {1})."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			return

		# Debt Purchase assignment: pool reclass + Party on both; no invoice refs.
		if edge == (WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE):
			if not dims:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			for r in rows:
				if r.get("reference_type") or r.get("reference_name"):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Debt Purchase Assignment Journal Entry "
							"must not carry invoice references on any line ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				if not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Every Journal Entry line must carry the drawer Party for transition {0} → {1}."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
			return

		if to_state == WORKFLOW_CLEARED and from_state in (
			WORKFLOW_REGISTERED,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_UNDER_LEGAL_ACTION,
		):
			bank_gl = _pdc_bank_gl_account(doc)
			if not dims:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			for r in rows:
				acc_name = _strip_link_name_or_none(r.get("account"))
				has_party = _row_has_any_party(r)
				is_bank = bool(bank_gl and acc_name == bank_gl) or _pdc_account_type(acc_name) == "Bank"
				if is_bank:
					if has_party:
						frappe.throw(
							frappe._(
								"Receivable PDC accounting integrity: Bank line must not carry Party on clear ({0} → {1})."
							).format(from_state, to_state),
							title=frappe._("PDC accounting integrity"),
						)
				elif not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Non-bank clear line must carry drawer Party ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
			return

		party_on_both_edges = {
			(WORKFLOW_DRAFT, WORKFLOW_REGISTERED),
			(WORKFLOW_REGISTERED, WORKFLOW_RETURNED),
			(WORKFLOW_REGISTERED, WORKFLOW_SENT_TO_BANK),
			(WORKFLOW_SENT_TO_BANK, WORKFLOW_REGISTERED),
			(WORKFLOW_SENT_TO_BANK, WORKFLOW_BOUNCED),
		}
		if edge in party_on_both_edges:
			if not dims:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			for r in rows:
				if _pdc_account_type(r.get("account")) == "Bank":
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Bank lines must not carry Party ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				if not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Every Journal Entry line must carry the drawer Party for transition {0} → {1}."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
			return

		# Replacement / other edges: require Party somewhere when present on settlement-style edges;
		# do not redesign Replacement Party matrix in 3.7.2.
		party_settlement_edges = {
			(WORKFLOW_RETURNED, WORKFLOW_REPLACED),
		}
		if edge in party_settlement_edges:
			if not dims:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			saw_party = False
			for r in rows:
				if not _row_has_any_party(r):
					continue
				if not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Party lines must carry the drawer Party for transition {0} → {1}."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				if _pdc_account_type(r.get("account")) == "Bank":
					frappe.throw(
						frappe._(
							"Receivable PDC accounting integrity: Bank lines must not carry Party ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				saw_party = True
			if not saw_party:
				frappe.throw(
					frappe._(
						"Receivable PDC accounting integrity: Party must be present on Journal Entry lines for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			return

		# Remaining edges (e.g. Bounced → Replaced): Party must not appear unless redesigned later.
		has_party = any(_row_has_any_party(r) for r in rows)
		if has_party:
			frappe.throw(
				frappe._(
					"Receivable PDC accounting integrity: Party must NOT be present on Journal Entry lines for transition {0} → {1}."
				).format(from_state, to_state),
				title=frappe._("PDC accounting integrity"),
			)

	def _validate_payable_party_integrity(payload: dict) -> None:
		"""Payable Party policy: supplier Party on all non-Bank lines for standard edges."""
		if doc.cheque_direction != CHEQUE_DIRECTION_PAYABLE:
			return
		edge = (from_state, to_state)
		rows = payload.get("accounts") or []
		dims = _pdc_party_dims_for_doc(doc)
		exp_pt, exp_p = dims.get("party_type"), dims.get("party")

		def _row_has_doc_party(r: dict) -> bool:
			return (
				_strip_link_name_or_none(r.get("party_type")) == exp_pt
				and _strip_link_name_or_none(r.get("party")) == exp_p
			)

		def _row_has_any_party(r: dict) -> bool:
			return bool(r.get("party_type") or r.get("party"))

		if edge == (WORKFLOW_ISSUED, WORKFLOW_CLEARED):
			bank_gl = _pdc_bank_gl_account(doc)
			expected_pool = acc.get("payable_cheque")
			expected_bank = bank_gl
			if not expected_pool or not expected_bank:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Cannot validate Cleared payload without Payable Cheque account and Bank GL."
					),
					title=frappe._("PDC accounting integrity"),
				)
			if not dims:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			if len(rows) != 2:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Cleared Journal Entry must have exactly 2 lines (Dr Payable Cheque account, Cr Bank)."
					),
					title=frappe._("PDC accounting integrity"),
				)
			debit_rows = [
				r
				for r in rows
				if float(r.get("debit_in_account_currency") or 0)
				and not float(r.get("credit_in_account_currency") or 0)
			]
			credit_rows = [
				r
				for r in rows
				if float(r.get("credit_in_account_currency") or 0)
				and not float(r.get("debit_in_account_currency") or 0)
			]
			if len(debit_rows) != 1 or len(credit_rows) != 1:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Cleared Journal Entry must be one debit line and one credit line (no mixed debit/credit lines)."
					),
					title=frappe._("PDC accounting integrity"),
				)
			dr = debit_rows[0]
			cr = credit_rows[0]
			if (
				_strip_link_name_or_none(dr.get("account")) != expected_pool
				or _strip_link_name_or_none(cr.get("account")) != expected_bank
			):
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Cleared must only use Dr {0} (Payable Cheque account) and Cr {1} (Bank)."
					).format(expected_pool, expected_bank),
					title=frappe._("PDC accounting integrity"),
				)
			for r in rows:
				acc_name = _strip_link_name_or_none(r.get("account"))
				has_party = _row_has_any_party(r)
				is_bank = bool(bank_gl and acc_name == bank_gl) or _pdc_account_type(acc_name) == "Bank"
				if is_bank:
					if has_party:
						frappe.throw(
							frappe._(
								"Payable PDC accounting integrity: Bank line must not carry Party on clear ({0} → {1})."
							).format(from_state, to_state),
							title=frappe._("PDC accounting integrity"),
						)
				elif not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Payable PDC accounting integrity: Payable cheque pool line must carry supplier Party on clear ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
			return

		party_on_both_edges = {
			(WORKFLOW_DRAFT, WORKFLOW_REGISTERED),
			(WORKFLOW_REGISTERED, WORKFLOW_CANCELLED),
			(WORKFLOW_ISSUED, WORKFLOW_RETURNED),
			(WORKFLOW_ISSUED, WORKFLOW_CANCELLED),
			# Advance-mode recognition when effective stage is `issue`.
			(WORKFLOW_REGISTERED, WORKFLOW_ISSUED),
		}
		if edge in party_on_both_edges:
			if not dims:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			for r in rows:
				if _pdc_account_type(r.get("account")) == "Bank":
					frappe.throw(
						frappe._(
							"Payable PDC accounting integrity: Bank lines must not carry Party ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				if not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Payable PDC accounting integrity: Every Journal Entry line must carry supplier Party for transition {0} → {1}."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
			return

		# Replacement edges (not redesigned in 3.7.2): Party required somewhere when present.
		party_settlement_edges = {
			(WORKFLOW_ISSUED, WORKFLOW_REPLACED),
			(WORKFLOW_RETURNED, WORKFLOW_REPLACED),
		}
		if edge in party_settlement_edges:
			if not dims:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Party Type and Party are required on the PDC for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)
			saw_party = False
			for r in rows:
				if not _row_has_any_party(r):
					continue
				if not _row_has_doc_party(r):
					frappe.throw(
						frappe._(
							"Payable PDC accounting integrity: Party lines must carry supplier Party for transition {0} → {1}."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				if _pdc_account_type(r.get("account")) == "Bank":
					frappe.throw(
						frappe._(
							"Payable PDC accounting integrity: Bank lines must not carry Party ({0} → {1})."
						).format(from_state, to_state),
						title=frappe._("PDC accounting integrity"),
					)
				saw_party = True
			if not saw_party:
				frappe.throw(
					frappe._(
						"Payable PDC accounting integrity: Party must be present on Journal Entry lines for transition {0} → {1}."
					).format(from_state, to_state),
					title=frappe._("PDC accounting integrity"),
				)

	def _return_je(payload: dict) -> dict:
		edge = (from_state, to_state)
		# Skip Party mirroring where builders own placement (Endorsement) or Replacement is
		# intentionally deferred from the 3.7.2 Party restore scope.
		skip_party_mirror = False
		if doc.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE and edge in (
			(WORKFLOW_REGISTERED, WORKFLOW_ENDORSED),
			(WORKFLOW_BOUNCED, WORKFLOW_REPLACED),
			(WORKFLOW_RETURNED, WORKFLOW_REPLACED),
		):
			skip_party_mirror = True
		elif doc.cheque_direction == CHEQUE_DIRECTION_PAYABLE and edge in (
			(WORKFLOW_ISSUED, WORKFLOW_REPLACED),
			(WORKFLOW_RETURNED, WORKFLOW_REPLACED),
		):
			skip_party_mirror = True
		if not skip_party_mirror:
			bank_gl = _pdc_bank_gl_account(doc) if to_state == WORKFLOW_CLEARED else None
			payload = dict(payload)
			payload["accounts"] = _pdc_accounts_with_doc_party(
				payload.get("accounts") or [], doc, bank_gl=bank_gl
			)
		_validate_receivable_party_integrity(payload)
		_validate_payable_party_integrity(payload)
		_log_debug_journal_entry_payload(doc, from_state, to_state, payload)
		return payload

	# --- Receivable transitions ---
	if doc.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
		# Draft -> Registered: Dr Cheques in Hand (``account_paid_to``), Cr party AR (``account_paid_from`` or party receivable).
		if from_state == WORKFLOW_DRAFT and to_state == WORKFLOW_REGISTERED:
			# Task 5: Advance Mode recognition (Sales Order / receivable) at Register stage only.
			if (getattr(doc, "allocation_mode", None) or "").strip() == ALLOCATION_MODE_ADVANCE:
				if cint(getattr(doc, "recognition_je_posted", 0)):
					return None
				eff = (
					(getattr(doc, "effective_stage_for_advance_recognition", None) or "register")
					.strip()
					.lower()
				)
				if eff != "register":
					return None
				debit_account = (
					_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
				)
				credit_account = _resolve_advance_account_override(
					doc
				) or _company_default_advance_received_account(getattr(doc, "company", None))
				if not debit_account or not credit_account:
					return None
				remark = render_pdc_je_text(
					getattr(settings, "je_remark_register_receivable_template", None) if settings else None,
					fallback_text=frappe._(PDC_JE_REMARK_REGISTER_RECEIVABLE_CHEQUE),
					context=ctx,
					append_cheque_no_suffix=True,
				)
				je = _base(remark)
				je["accounts"] = [
					{
						"account": debit_account,
						"debit_in_account_currency": doc.cheque_amount,
					},
					{
						"account": credit_account,
						"credit_in_account_currency": doc.cheque_amount,
						"party_type": doc.party_type,
						"party": doc.party,
					},
				]
				je["set_recognition_je_posted"] = 1
				return _return_je(je)
			debit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			credit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_from", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "receivable")
			if not debit_account or not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_register_receivable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_REGISTER_RECEIVABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			slices = receivable_sales_invoice_settlement_slices(doc)
			if slices:
				je["accounts"] = [
					{
						"account": debit_account,
						"debit_in_account_currency": doc.cheque_amount,
					},
				]
				for sinv, amt in slices:
					je["accounts"].append(
						{
							"account": credit_account,
							"credit_in_account_currency": amt,
							"party_type": doc.party_type,
							"party": doc.party,
							"reference_type": "Sales Invoice",
							"reference_name": sinv,
						}
					)
			else:
				je["accounts"] = [
					{
						"account": debit_account,
						"debit_in_account_currency": doc.cheque_amount,
					},
					{
						"account": credit_account,
						"credit_in_account_currency": doc.cheque_amount,
						"party_type": doc.party_type,
						"party": doc.party,
					},
				]
			return _return_je(je)

		# Registered -> Sent to Bank: Dr Clearing, Cr Cheques in Hand (``account_paid_to`` / resolver).
		if from_state == WORKFLOW_REGISTERED and to_state == WORKFLOW_SENT_TO_BANK:
			if not acc["cheques_in_clearing"]:
				return None
			credit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			if not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_send_receivable_to_bank_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_SEND_RECEIVABLE_CHEQUE_TO_BANK),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": acc["cheques_in_clearing"],
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": credit_account,
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Sent to Bank -> Registered (Return from Bank): reverse of Send — Dr CIH, Cr Clearing.
		# Independent JE; does not require or link to a prior Send JE (opening-balance safe).
		if from_state == WORKFLOW_SENT_TO_BANK and to_state == WORKFLOW_REGISTERED:
			if not acc["cheques_in_clearing"]:
				return None
			debit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			if not debit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_return_receivable_from_bank_template", None)
				if settings
				else None,
				fallback_text=frappe._(PDC_JE_REMARK_RETURN_RECEIVABLE_FROM_BANK),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": debit_account,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": acc["cheques_in_clearing"],
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Registered -> Assigned to Bank for Debt Purchase: Dr DP in collection, Cr Cheques in Hand.
		if from_state == WORKFLOW_REGISTERED and to_state == WORKFLOW_ASSIGNED_DEBT_PURCHASE:
			if not acc.get("debt_purchase_in_collection"):
				return None
			credit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			if not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_assign_receivable_debt_purchase_template", None)
				if settings
				else None,
				fallback_text=frappe._(PDC_JE_REMARK_ASSIGN_RECEIVABLE_DEBT_PURCHASE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": acc["debt_purchase_in_collection"],
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": credit_account,
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Assigned DP -> Bounced: Dr Protested / Cr DPIC (dishonour; not returned to cashier).
		# No cheques_in_hand fallback — Protested must be configured in PDC Settings.
		if from_state == WORKFLOW_ASSIGNED_DEBT_PURCHASE and to_state == WORKFLOW_BOUNCED:
			dpic = acc.get("debt_purchase_in_collection")
			protested = acc.get("protested")
			if not dpic:
				frappe.throw(
					frappe._(
						"Default Debt Purchase In Collection Account is required in PDC Settings "
						"to bounce a Debt Purchase cheque."
					),
					title=frappe._("Missing Debt Purchase In Collection Account"),
				)
			if not protested:
				frappe.throw(
					frappe._(
						"Default Protested Account is required in PDC Settings to bounce a Debt Purchase cheque. "
						"Configure Default Protested Account before Bounce Cheque. "
						"Cheques in Hand is not used for this transition."
					),
					title=frappe._("Missing Protested Account"),
				)
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_receivable_bounced_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_RECEIVABLE_CHEQUE_BOUNCED),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": protested,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": dpic,
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Registered / Sent to Bank / Under Legal Action -> Cleared: Dr Bank GL (no Party), Cr intermediary (Party via _return_je).
		if to_state == WORKFLOW_CLEARED and from_state in (
			WORKFLOW_REGISTERED,
			WORKFLOW_SENT_TO_BANK,
			WORKFLOW_UNDER_LEGAL_ACTION,
		):
			bank_gl = _pdc_bank_gl_account(doc)
			if not bank_gl:
				return None
			_pdc_validate_clearing_bank_ledger_account(doc, bank_gl)
			credit_account = receivable_intermediary_account_for_bank_clear(doc, from_state, acc)
			if not credit_account:
				return None
			if from_state == WORKFLOW_REGISTERED:
				remark_base = PDC_JE_REMARK_CLEAR_RECEIVABLE_REGISTERED
			elif from_state == WORKFLOW_SENT_TO_BANK:
				remark_base = PDC_JE_REMARK_CLEAR_RECEIVABLE_CLEARING
			else:
				remark_base = PDC_JE_REMARK_CLEAR_RECEIVABLE_LEGAL
			field = (
				"je_remark_clear_receivable_registered_template"
				if from_state == WORKFLOW_REGISTERED
				else "je_remark_clear_receivable_clearing_template"
				if from_state == WORKFLOW_SENT_TO_BANK
				else "je_remark_clear_receivable_legal_template"
			)
			remark = render_pdc_je_text(
				getattr(settings, field, None) if settings else None,
				fallback_text=frappe._(remark_base),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": bank_gl,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": credit_account,
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Sent to Bank -> Bounced: Cr Clearing; Dr protested (preferred) or Cheques in Hand.
		if from_state == WORKFLOW_SENT_TO_BANK and to_state == WORKFLOW_BOUNCED:
			if not acc["cheques_in_clearing"]:
				return None
			debit_account = acc["protested"] or acc["cheques_in_hand"]
			if not debit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_receivable_bounced_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_RECEIVABLE_CHEQUE_BOUNCED),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": debit_account,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": acc["cheques_in_clearing"],
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Bounced -> Replaced (Receivable): Dr Cheques in Hand (replacement instrument), Cr protested pool.
		# TODO(accounting): Confirm with finance — offsets dishonoured balance; may need clearing/protest split or link to ``replaces_cheque``.
		if from_state == WORKFLOW_BOUNCED and to_state == WORKFLOW_REPLACED:
			if not acc["protested"]:
				return None
			debit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			if not debit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_replace_receivable_after_bounce_template", None)
				if settings
				else None,
				fallback_text=frappe._(PDC_JE_REMARK_REPLACE_RECEIVABLE_AFTER_BOUNCE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": debit_account,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": acc["protested"],
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

		# Returned -> Replaced (Receivable): Dr Cheques in Hand, Cr party AR (inverse of Registered->Returned).
		# TODO(accounting): Tie to ``replaces_cheque`` / prior return JE — confirm amounts and timing.
		if from_state == WORKFLOW_RETURNED and to_state == WORKFLOW_REPLACED:
			debit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			credit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_from", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "receivable")
			if not debit_account or not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_replace_receivable_after_return_template", None)
				if settings
				else None,
				fallback_text=frappe._(PDC_JE_REMARK_REPLACE_RECEIVABLE_AFTER_RETURN),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": debit_account,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": credit_account,
					"credit_in_account_currency": doc.cheque_amount,
					"party_type": doc.party_type,
					"party": doc.party,
				},
			]
			return _return_je(je)

		# Registered -> Returned: Dr party AR (``account_paid_from`` / party receivable), Cr Cheques in Hand (``account_paid_to`` / resolver).
		if from_state == WORKFLOW_REGISTERED and to_state == WORKFLOW_RETURNED:
			debit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_from", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "receivable")
			credit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			if not debit_account or not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_return_receivable_to_party_template", None)
				if settings
				else None,
				fallback_text=frappe._(PDC_JE_REMARK_RETURN_RECEIVABLE_CHEQUE_TO_PARTY),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			slices = receivable_sales_invoice_settlement_slices(doc)
			if slices:
				je["accounts"] = []
				for sinv, amt in slices:
					je["accounts"].append(
						{
							"account": debit_account,
							"debit_in_account_currency": amt,
							"party_type": doc.party_type,
							"party": doc.party,
							"reference_type": "Sales Invoice",
							"reference_name": sinv,
						}
					)
				je["accounts"].append(
					{
						"account": credit_account,
						"credit_in_account_currency": doc.cheque_amount,
					}
				)
			else:
				je["accounts"] = [
					{
						"account": debit_account,
						"debit_in_account_currency": doc.cheque_amount,
						"party_type": doc.party_type,
						"party": doc.party,
					},
					{
						"account": credit_account,
						"credit_in_account_currency": doc.cheque_amount,
					},
				]
			return _return_je(je)

		# Registered -> Endorsed: Dr settlement GL (preferred) or endorsed holder AR — Cr Cheques in Hand.
		# No bank / PE; drawer (party) must not appear on lines (registration already credited AR).
		if from_state == WORKFLOW_REGISTERED and to_state == WORKFLOW_ENDORSED:
			holder_party_type = _strip_link_name_or_none(
				getattr(doc, "holder_party_type", None)
			) or _strip_link_name_or_none(getattr(doc, "party_type", None))
			holder_party = _strip_link_name_or_none(
				getattr(doc, "holder_party", None)
			) or _strip_link_name_or_none(getattr(doc, "party", None))
			if not holder_party_type or not holder_party:
				return None
			credit_account = (
				_strip_link_name_or_none(getattr(doc, "account_paid_to", None)) or acc["cheques_in_hand"]
			)
			if not credit_account:
				return None
			doc_settlement = _strip_link_name_or_none(getattr(doc, "endorsement_settlement_account", None))
			debit_account = doc_settlement or acc["endorsement_account"]
			debit_row: dict
			if debit_account:
				debit_row = {
					"account": debit_account,
					"debit_in_account_currency": doc.cheque_amount,
				}
			else:
				orig_pt = _strip_link_name_or_none(getattr(doc, "party_type", None))
				orig_p = _strip_link_name_or_none(getattr(doc, "party", None))
				if orig_pt == holder_party_type and orig_p == holder_party:
					return None
				holder_account = _get_party_account_or_company_default(
					holder_party_type, holder_party, doc.company, "receivable"
				)
				if not holder_account:
					return None
				debit_row = {
					"account": holder_account,
					"debit_in_account_currency": doc.cheque_amount,
					"party_type": holder_party_type,
					"party": holder_party,
				}
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_endorse_receivable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_ENDORSE_RECEIVABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			credit_row: dict = {
				"account": credit_account,
				"credit_in_account_currency": doc.cheque_amount,
			}
			# Cheques in Hand may be Receivable on COA; JE validate_party requires party (holder, not drawer).
			cih_at = frappe.get_cached_value("Account", credit_account, "account_type")
			if cih_at in ("Receivable", "Payable"):
				credit_row["party_type"] = holder_party_type
				credit_row["party"] = holder_party
			je = _base(remark)
			je["accounts"] = [debit_row, credit_row]
			return _return_je(je)

	# --- Payable transitions ---
	if doc.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
		# Draft -> Registered: Dr party payable (PI refs on AP rows), Cr notes payable pool — supplier settlement.
		if from_state == WORKFLOW_DRAFT and to_state == WORKFLOW_REGISTERED:
			# Task 5: Advance Mode recognition (Purchase Order / payable) at Register or Issue depending on config.
			if (getattr(doc, "allocation_mode", None) or "").strip() == ALLOCATION_MODE_ADVANCE:
				if cint(getattr(doc, "recognition_je_posted", 0)):
					return None
				eff = (
					(getattr(doc, "effective_stage_for_advance_recognition", None) or "register")
					.strip()
					.lower()
				)
				# If recognition is configured at `issue`, do not post at register.
				if eff == "issue":
					return None
				debit_account = _resolve_advance_account_override(
					doc
				) or _company_default_advance_paid_account(getattr(doc, "company", None))
				credit_account = acc.get("payable_cheque")
				if not debit_account or not credit_account:
					return None
				remark = render_pdc_je_text(
					getattr(settings, "je_remark_register_payable_template", None) if settings else None,
					fallback_text=frappe._(PDC_JE_REMARK_REGISTER_PAYABLE_CHEQUE),
					context=ctx,
					append_cheque_no_suffix=True,
				)
				je = _base(remark)
				je["accounts"] = [
					{
						"account": debit_account,
						"debit_in_account_currency": doc.cheque_amount,
						"party_type": doc.party_type,
						"party": doc.party,
					},
					{
						"account": credit_account,
						"credit_in_account_currency": doc.cheque_amount,
					},
				]
				je["set_recognition_je_posted"] = 1
				return _return_je(je)
			if not acc["payable_cheque"]:
				return None
			debit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_to", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "payable")
			if not debit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_register_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_REGISTER_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			party_debits = _payable_party_issue_debit_lines(debit_account)
			je["accounts"] = party_debits + [
				{
					"account": acc["payable_cheque"],
					"credit_in_account_currency": doc.cheque_amount,
				}
			]
			return _return_je(je)

		# Registered -> Issued: Task 5 advance-mode recognition when configured at `issue`.
		if from_state == WORKFLOW_REGISTERED and to_state == WORKFLOW_ISSUED:
			if (getattr(doc, "allocation_mode", None) or "").strip() != ALLOCATION_MODE_ADVANCE:
				return None
			if cint(getattr(doc, "recognition_je_posted", 0)):
				return None
			eff = (
				(getattr(doc, "effective_stage_for_advance_recognition", None) or "register").strip().lower()
			)
			if eff != "issue":
				return None
			debit_account = _resolve_advance_account_override(doc) or _company_default_advance_paid_account(
				getattr(doc, "company", None)
			)
			credit_account = acc.get("payable_cheque")
			if not debit_account or not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_register_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_REGISTER_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": debit_account,
					"debit_in_account_currency": doc.cheque_amount,
					"party_type": doc.party_type,
					"party": doc.party,
				},
				{
					"account": credit_account,
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			je["set_recognition_je_posted"] = 1
			return _return_je(je)

		# Registered -> Cancelled: Dr notes payable pool, Cr party payable — reverse Draft→Registered settlement.
		if from_state == WORKFLOW_REGISTERED and to_state == WORKFLOW_CANCELLED:
			if not acc["payable_cheque"]:
				return None
			credit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_to", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "payable")
			if not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_cancel_registered_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_CANCEL_REGISTERED_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			party_credits = _payable_party_reverse_credit_lines(credit_account)
			je["accounts"] = [
				{
					"account": acc["payable_cheque"],
					"debit_in_account_currency": doc.cheque_amount,
				},
				*party_credits,
			]
			return _return_je(je)

		# Issued -> Returned: Dr notes payable pool, Cr party payable (``account_paid_to`` / settlement).
		if from_state == WORKFLOW_ISSUED and to_state == WORKFLOW_RETURNED:
			if not acc["payable_cheque"]:
				return None
			credit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_to", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "payable")
			if not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_returned_payable_from_payee_template", None)
				if settings
				else None,
				fallback_text=frappe._(PDC_JE_REMARK_RETURNED_PAYABLE_CHEQUE_FROM_PAYEE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			party_credits = _payable_party_reverse_credit_lines(credit_account)
			je["accounts"] = [
				{
					"account": acc["payable_cheque"],
					"debit_in_account_currency": doc.cheque_amount,
				},
				*party_credits,
			]
			return _return_je(je)

		# Issued -> Replaced: same shape as Issued -> Returned (reverse notes-payable pool to party).
		# TODO(accounting): May require paired JE for the new cheque / link via ``replaces_cheque`` — policy TBD.
		if from_state == WORKFLOW_ISSUED and to_state == WORKFLOW_REPLACED:
			if not acc["payable_cheque"]:
				return None
			credit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_to", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "payable")
			if not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_replace_issued_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_REPLACE_ISSUED_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			party_credits = _payable_party_reverse_credit_lines(credit_account)
			je["accounts"] = [
				{
					"account": acc["payable_cheque"],
					"debit_in_account_currency": doc.cheque_amount,
				},
				*party_credits,
			]
			return _return_je(je)

		# Returned -> Replaced: same shape as Draft -> Registered (book new instrument to pool).
		# TODO(accounting): Confirm netting with prior return JE and replacement numbering.
		if from_state == WORKFLOW_RETURNED and to_state == WORKFLOW_REPLACED:
			if not acc["payable_cheque"]:
				return None
			debit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_to", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "payable")
			if not debit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_replace_returned_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_REPLACE_RETURNED_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			party_debits = _payable_party_issue_debit_lines(debit_account)
			je["accounts"] = party_debits + [
				{
					"account": acc["payable_cheque"],
					"credit_in_account_currency": doc.cheque_amount,
				}
			]
			return _return_je(je)

		# Issued -> Cancelled: Dr notes payable pool, Cr party payable (same shape as return).
		if from_state == WORKFLOW_ISSUED and to_state == WORKFLOW_CANCELLED:
			if not acc["payable_cheque"]:
				return None
			credit_account = _strip_link_name_or_none(
				getattr(doc, "account_paid_to", None)
			) or _get_party_account_or_company_default(doc.party_type, doc.party, doc.company, "payable")
			if not credit_account:
				return None
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_cancel_issued_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_CANCEL_ISSUED_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			party_credits = _payable_party_reverse_credit_lines(credit_account)
			je["accounts"] = [
				{
					"account": acc["payable_cheque"],
					"debit_in_account_currency": doc.cheque_amount,
				},
				*party_credits,
			]
			return _return_je(je)

		# Issued -> Cleared: Dr notes payable pool, Cr Bank GL (party on pool debit via _return_je).
		if from_state == WORKFLOW_ISSUED and to_state == WORKFLOW_CLEARED:
			if not getattr(doc, "cheque_amount", None):
				return None
			if not acc["payable_cheque"]:
				return None
			bank_gl = _pdc_bank_gl_account(doc)
			if not bank_gl:
				return None
			_pdc_validate_clearing_bank_ledger_account(doc, bank_gl)
			debit_pool = acc["payable_cheque"]
			remark = render_pdc_je_text(
				getattr(settings, "je_remark_clear_payable_template", None) if settings else None,
				fallback_text=frappe._(PDC_JE_REMARK_CLEAR_PAYABLE_CHEQUE),
				context=ctx,
				append_cheque_no_suffix=True,
			)
			je = _base(remark)
			je["accounts"] = [
				{
					"account": debit_pool,
					"debit_in_account_currency": doc.cheque_amount,
				},
				{
					"account": bank_gl,
					"credit_in_account_currency": doc.cheque_amount,
				},
			]
			return _return_je(je)

	return None


# Alias: same payload shape (voucher_type, posting_date, remarks, accounts).
build_pdc_journal_entry_payload = build_pdc_journal_entry_data


def _pdc_bank_gl_account(doc) -> str | None:
	"""Company bank GL from PDC ``bank_account`` → **Bank Account** ``account``.

	The linked **Account** must be the real **Bank** ledger for this company (validated at clear via
	:func:`_pdc_validate_clearing_bank_ledger_account` when the account row exists in the database)
	so bank ledger and bank reconciliation stay aligned with the voucher.
	"""
	ba = _strip_link_name_or_none(getattr(doc, "bank_account", None))
	if not ba:
		return None
	return _strip_link_name_or_none(frappe.db.get_value("Bank Account", ba, "account"))


def _pdc_validate_clearing_bank_ledger_account(doc, bank_gl: str | None) -> None:
	"""Cleared cheques must post the bank leg to the company **Bank** GL from the PDC Bank Account.

	Receivable **→ Cleared** debits this account; Payable **Issued → Cleared** credits it.
	Requires **Account.account_type == Bank** and (when both are known) **Account.company** = PDC company.
	Company Bank Account linkage (``is_company_account``, Bank Account.company) is enforced separately
	in :meth:`PostDatedCheque._validate_bank_account_for_cleared_workflow_state`.

	When the **Account** doc is not in the local DB (e.g. isolated unit tests), validation is skipped.

	Cheque intermediary accounts (clearing / in-hand / protested / payable pool) **may** be
	Receivable/Payable in Chart of Accounts; party on those lines is applied in the JE payload.
	"""
	if not bank_gl:
		return
	if not frappe.db.exists("Account", bank_gl):
		return
	doc_company = (getattr(doc, "company", None) or "").strip()
	acc_company = (frappe.get_cached_value("Account", bank_gl, "company") or "").strip()
	if doc_company and acc_company and acc_company != doc_company:
		frappe.throw(
			frappe._(
				"Cheque clearing bank ledger **{0}** belongs to company «{1}», but this Post Dated Cheque is for "
				"«{2}». Use a **Bank Account** for the same company so the Journal Entry hits the correct bank ledger."
			).format(bank_gl, acc_company, doc_company),
			title=frappe._("Wrong company for clearing bank account"),
		)
	at = frappe.get_cached_value("Account", bank_gl, "account_type")
	if at in ("Receivable", "Payable"):
		frappe.throw(
			frappe._(
				"Cheque clearing bank leg cannot use a Receivable or Payable account (**{0}**, type «{1}»). "
				"Link a **Bank Account** whose GL is account type **Bank**."
			).format(bank_gl, at or ""),
			title=frappe._("Invalid bank ledger for clearing"),
		)
	if at != "Bank":
		frappe.throw(
			frappe._(
				"Cheque clearing must use a **Bank** ledger account (the GL linked from **Bank Account** on this PDC); "
				"account **{0}** has type «{1}». Update the bank account or chart of accounts so reconciliation sees the correct bank book."
			).format(bank_gl, at or ""),
			title=frappe._("Invalid bank ledger for clearing"),
		)


@frappe.whitelist()
@validate_and_sanitize_search_inputs
def pdc_cheque_leaf_link_query(
	doctype,
	txt,
	searchfield,
	start,
	page_len,
	filters,
	as_dict=False,
	**kwargs,
):
	"""Link search for **Cheque Leaf** on Post Dated Cheque (Payable): readable dropdown line.

	Returns ``(name, description)`` rows where **description** is
	``cheque_number | status | bank_account | cheque_book`` (stable sort by cheque_number).
	Filters: **company**, **bank_account**; **status** is always **Available** (not taken from client).
	"""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	filters = filters or {}

	company = cstr(filters.get("company", "")).strip()
	bank_account = cstr(filters.get("bank_account", "")).strip()
	if not company or not bank_account:
		return []

	txt = cstr(txt or "").strip()
	start = cint(start)
	page_len = cint(page_len) or 10

	like = f"%{txt}%" if txt else None
	where_extra = ""
	values: dict = {
		"company": company,
		"bank_account": bank_account,
		"start": start,
		"page_len": page_len,
	}
	if like is not None:
		where_extra = """ AND (
			cl.name LIKE %(like)s
			OR cl.cheque_number LIKE %(like)s
			OR cl.cheque_book LIKE %(like)s
			OR cl.bank_account LIKE %(like)s
			OR cl.status LIKE %(like)s
		)"""
		values["like"] = like

	# Second column becomes Link field **description** in desk autosuggest.
	return frappe.db.sql(
		f"""
		SELECT
			cl.name,
			CONCAT_WS(
				' | ',
				NULLIF(TRIM(IFNULL(cl.cheque_number, '')), ''),
				NULLIF(TRIM(IFNULL(cl.status, '')), ''),
				NULLIF(TRIM(IFNULL(cl.bank_account, '')), ''),
				NULLIF(TRIM(IFNULL(cl.cheque_book, '')), '')
			) AS description
		FROM `tabCheque Leaf` cl
		WHERE
			cl.company = %(company)s
			AND cl.bank_account = %(bank_account)s
			AND cl.status = 'Available'
			AND IFNULL(cl.linked_guarantee_document, '') = ''
			AND IFNULL(cl.linked_post_dated_cheque, '') = ''
			AND IFNULL(cl.reserved_by_pdc, '') = ''
			{where_extra}
		ORDER BY cl.cheque_number ASC
		LIMIT %(start)s, %(page_len)s
		""",
		values,
		as_list=1,
	)


@frappe.whitelist()
def get_default_party_accounts(party_type, party, company, cheque_direction):
	"""Return default Account Paid From / Account Paid To for the given party and direction."""
	if not company or not cheque_direction:
		return {}
	out = {}
	if cheque_direction == "Receivable":
		# Account Paid To always tracks Cheques in Hand from PDC Settings (no party required).
		ch = _get_cheques_in_hand_account_for_company(company)
		if ch:
			out["account_paid_to"] = ch
	if party_type and party and company:
		if cheque_direction == "Receivable":
			out["account_paid_from"] = _get_party_account_or_company_default(
				party_type, party, company, "receivable"
			)
		elif cheque_direction == "Payable":
			out["account_paid_to"] = _get_party_account_or_company_default(
				party_type, party, company, "payable"
			)
	return out


class PostDatedCheque(Document):
	"""Submittable PDC: receivable or payable post-dated cheque with workflow-driven GL posting.

	Journal-centric lifecycle (design): party settlement timing — **Receivable** at **Registered**,
	**Payable** at **Issued**; **Cleared** = JE bank movement only; allocation to invoices/advances is
	separate from workflow transitions. See module docstring for full rules.
	"""

	def before_insert(self):
		"""Set defaults for new PDC."""
		if not self.workflow_state:
			self.workflow_state = "Draft"
		self._set_default_party_type_for_payable_if_missing()
		# Initial holder = party (Received From / Paid To)
		if not self.holder_party and self.party:
			self.holder_party_type = self.party_type
			self.holder_party = self.party
		self._autofill_accounts_from_pdc_settings_if_missing()

	def before_validate(self):
		"""Runs before ``validate`` on **save** and **submit** (not on ``update_after_submit`` — see ``before_update_after_submit``)."""
		self._set_default_bank_account_for_receivable()
		self._set_default_party_type_for_payable_if_missing()
		if self.is_new():
			self._autofill_accounts_from_pdc_settings_if_missing()

	def before_save(self):
		"""Persist ``cheque_status`` derived from ``workflow_state`` (see ``_sync_cheque_status_from_workflow_state``)."""
		self._sync_cheque_status_from_workflow_state()
		self._sync_is_at_bank_from_workflow_state()

	def before_submit(self):
		"""Frappe does not run ``before_save`` on submit — only ``validate`` then ``before_submit``.

		Re-sync so ``cheque_status`` matches ``workflow_state`` immediately before the document is
		submitted (``validate`` already syncs; this is an explicit last pass).
		"""
		self._sync_cheque_status_from_workflow_state()
		self._sync_is_at_bank_from_workflow_state()

	def before_update_after_submit(self):
		"""Run the same validations as draft saves when the document is already submitted.

		ERPNext workflow actions call :func:`~frappe.model.workflow.apply_workflow`, which ends in
		``doc.save()`` with ``_action == "update_after_submit"``. For that path Frappe runs
		``before_update_after_submit`` / ``on_update_after_submit`` only — not ``before_save`` or
		``on_update``. Without this hook, ``workflow_state`` can change while ``cheque_status``,
		transition checks, **holder history on endorsement**, and accounting never run.
		"""
		self._set_default_bank_account_for_receivable()
		self.validate()

	def on_update(self):
		"""Keep ``replaces_cheque`` / ``replaced_by`` in sync with the counterparty PDC.

		Runs after insert and after save (Frappe ``run_post_save_methods`` → ``on_update``).
		"""
		self._sync_replacement_bidirectional_links()
		self._pdc_post_save_accounting_sequence()

	def on_update_after_submit(self):
		"""Submitted saves skip :meth:`on_update`; keep replacement mirroring and accounting in sync."""
		self._sync_replacement_bidirectional_links()
		self._pdc_post_save_accounting_sequence()

	def validate(self):
		"""Validate PDC data and enforce immutability after submit."""
		validate_post_dated_cheque_allocation_mode_immutability(self)
		from erpnext_extensions.cheque_management.pdc_lifecycle_events import (
			validate_lifecycle_events_immutable,
		)
		from erpnext_extensions.cheque_management.pdc_workflow_rollback import (
			validate_workflow_rollback_logs_immutable,
		)

		validate_workflow_rollback_logs_immutable(self)
		validate_lifecycle_events_immutable(self)
		self._normalize_cheque_purpose()
		self._validate_cheque_direction_mutability()
		self._validate_payable_bank_account_mutability()
		self._validate_receivable_bank_account_mutability()
		self._reset_party_if_party_type_changed()
		self._set_default_party_accounts()
		self._validate_receivable_cheques_in_hand_account_required()
		self._validate_allocations()
		self._validate_advance_scope_structural()
		_validate_advance_account_override(self)
		self._validate_party()
		self._apply_cheque_leaf_cleanup_when_not_payable_draft()
		self._validate_duplicate_cheque_no()
		self._validate_cheque_leaf_integration()
		self._validate_drawer_bank()
		self._validate_replaces_cheque()
		self._validate_replacement_bidirectional_conflicts()
		self._validate_replacement_no_cycle()
		self._pdc_pre_save_workflow_sequence()
		self._validate_allocation_status_awareness()
		self._validate_replacement_links_when_replaced()
		self._validate_returned_workflow_state()
		self._validate_important_dates_not_in_future()
		self._validate_handover_date_vs_received_date()
		self._validate_receivable_sent_to_bank_vs_received_date()
		self._validate_receivable_cleared_and_bounced_vs_sent_to_bank()
		self._validate_returned_date_vs_received_date()
		self._validate_payable_cleared_vs_handover_date()
		self._validate_party_immutable_after_submit()
		self._validate_sayad_registration_per_settings()
		self._apply_cheque_leaf_reservation_draft()

	def _normalize_cheque_purpose(self) -> None:
		"""Trim cheque_purpose; whitespace-only values become empty (optional field)."""
		raw = getattr(self, "cheque_purpose", None)
		if raw is None:
			return
		normalized = cstr(raw).strip()
		self.cheque_purpose = normalized or None

	def _validate_payable_bank_account_mutability(self) -> None:
		"""Payable: bank_account is immutable once the cheque is Registered-or-later.

		Registered-or-later is approximated as workflow_state not equal to Draft (previous or current),
		which matches the v1 lifecycle where Draft is the only pre-registration state.
		"""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return

		if (getattr(self, "cheque_direction", None) or "").strip() != CHEQUE_DIRECTION_PAYABLE:
			return

		prev_bank = (getattr(before, "bank_account", None) or "").strip()
		cur_bank = (getattr(self, "bank_account", None) or "").strip()
		if prev_bank == cur_bank:
			return

		prev_ws = (getattr(before, "workflow_state", None) or "").strip()
		cur_ws = (getattr(self, "workflow_state", None) or "").strip()
		registered_or_later = (prev_ws and prev_ws != WORKFLOW_DRAFT) or (cur_ws and cur_ws != WORKFLOW_DRAFT)
		if registered_or_later:
			frappe.throw(
				frappe._("Bank Account cannot be changed after a payable cheque is registered."),
				title=frappe._("Bank Account"),
			)

	def _validate_receivable_bank_account_mutability(self) -> None:
		"""Receivable: bank_account is immutable once sent-to-bank-or-later."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return

		if (getattr(self, "cheque_direction", None) or "").strip() != CHEQUE_DIRECTION_RECEIVABLE:
			return

		prev_bank = (getattr(before, "bank_account", None) or "").strip()
		cur_bank = (getattr(self, "bank_account", None) or "").strip()
		if prev_bank == cur_bank:
			return

		prev_ws_raw = (getattr(before, "workflow_state", None) or "").strip()
		cur_ws_raw = (getattr(self, "workflow_state", None) or "").strip()
		prev_ws = normalize_workflow_state_value(prev_ws_raw) if prev_ws_raw else ""
		cur_ws = normalize_workflow_state_value(cur_ws_raw) if cur_ws_raw else ""
		prev_status = (getattr(before, "cheque_status", None) or "").strip()
		cur_status = (getattr(self, "cheque_status", None) or "").strip()

		ws_locked = {
			"Sent to Bank",
			"In Clearing",
			"Cleared",
			"Bounced",
			"Returned",
			"Cancelled",
			"Replaced",
		}
		status_locked = {"In Clearing", "Cleared", "Bounced", "Returned"}

		sent_to_bank_date_set = bool(
			getattr(before, "sent_to_bank_date", None) or getattr(self, "sent_to_bank_date", None)
		)
		in_locked_ws = (prev_ws in ws_locked) or (cur_ws in ws_locked)
		in_locked_status = (prev_status in status_locked) or (cur_status in status_locked)

		# Registered is NOT sent-to-bank-or-later from sent_to_bank_date alone (it may be prefilled early).
		# If workflow/cheque_status already indicates clearing lifecycle, those signals still lock.
		sent_to_bank_signal = (
			sent_to_bank_date_set and (prev_ws != WORKFLOW_REGISTERED) and (cur_ws != WORKFLOW_REGISTERED)
		)

		if not (sent_to_bank_signal or in_locked_ws or in_locked_status):
			return

		# Allow setting bank_account for the first time in sent-to-bank-or-later stages.
		# Only block changing an already-set bank_account.
		if not prev_bank and cur_bank:
			return

		# Block clearing/changing once bank_account was already set in sent-to-bank-or-later stages.
		if prev_bank and (not cur_bank or cur_bank != prev_bank):
			frappe.throw(
				frappe._("Bank Account cannot be changed after a receivable cheque is sent to bank."),
				title=frappe._("Bank Account"),
			)

	def _validate_cheque_direction_mutability(self) -> None:
		"""Cheque direction is mutable only within allowed windows.

		Rules:
		- New unsaved docs: editable
		- If previously saved as Payable: direction cannot change
		- If previously saved as Receivable: direction cannot change once sent to bank / clearing
		"""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return

		prev_dir = (getattr(before, "cheque_direction", None) or "").strip()
		cur_dir = (getattr(self, "cheque_direction", None) or "").strip()
		if prev_dir == cur_dir:
			return

		# Payable is locked after first save.
		if prev_dir == CHEQUE_DIRECTION_PAYABLE:
			frappe.throw(
				frappe._("Cheque Direction cannot be changed after a payable cheque is saved."),
				title=frappe._("Cheque Direction"),
			)

		# Receivable: lock after sent to bank / clearing / cleared.
		if prev_dir == CHEQUE_DIRECTION_RECEIVABLE:
			prev_ws = (getattr(before, "workflow_state", None) or "").strip()
			cur_ws = (getattr(self, "workflow_state", None) or "").strip()
			receivable_locked_states = {"Sent to Bank", "In Clearing", "Cleared"}
			sent_to_bank = bool(
				getattr(before, "sent_to_bank_date", None) or getattr(self, "sent_to_bank_date", None)
			)
			in_locked_state = bool(
				(prev_ws in receivable_locked_states) or (cur_ws in receivable_locked_states)
			)
			if sent_to_bank or in_locked_state:
				frappe.throw(
					frappe._("Cheque Direction cannot be changed after a receivable cheque is sent to bank."),
					title=frappe._("Cheque Direction"),
				)

	def _apply_cheque_leaf_cleanup_when_not_payable_draft(self) -> None:
		"""Draft UX: if switching away from Payable or clearing cheque_leaf, release any reserved leaf."""
		if (self.docstatus or 0) != 0:
			return
		before = None if self.is_new() else self.get_doc_before_save()
		prev_leaf = ((getattr(before, "cheque_leaf", None) if before else None) or "").strip()
		cur_leaf = (getattr(self, "cheque_leaf", None) or "").strip()

		# If direction is not Payable, force-clear cheque_leaf in draft.
		if (self.cheque_direction or "").strip() != "Payable" and cur_leaf:
			cur_leaf = ""
			self.cheque_leaf = ""

		# If leaf changed/cleared, release old reservation owned by this PDC.
		if prev_leaf and prev_leaf != cur_leaf and self.name:
			_pdc_release_leaf_if_reserved_by_pdc(prev_leaf, self.name)

	def on_submit(self):
		self._apply_cheque_leaf_on_submit()

	def before_cancel(self):
		"""Block direct cancel; use Rollback Workflow State (see pdc_direct_cancel_policy)."""
		from erpnext_extensions.cheque_management.pdc_direct_cancel_policy import (
			validate_pdc_direct_cancel_allowed,
		)

		validate_pdc_direct_cancel_allowed()

	def on_cancel(self):
		self._apply_cheque_leaf_on_cancel()

	def on_trash(self):
		"""Draft Payable delete: release owned temporary Cheque Leaf reservation."""
		self._release_cheque_leaf_reservation_on_trash()

	def _release_cheque_leaf_reservation_on_trash(self) -> None:
		"""Release this Draft PDC's owned Reserved leaf before the document row is removed.

		Registered→Draft rollback keeps the leaf Reserved while the Draft still exists.
		Deleting that Draft must return the leaf to Available. Submitted/cancelled docs
		do not use this path (cancel already handles leaf via ``on_cancel``).
		"""
		if cint(getattr(self, "docstatus", 0) or 0) != 0:
			return
		if (getattr(self, "cheque_direction", None) or "").strip() != CHEQUE_DIRECTION_PAYABLE:
			return
		leaf = (getattr(self, "cheque_leaf", None) or "").strip()
		if not leaf or not self.name:
			return
		_pdc_assert_and_release_leaf_on_draft_trash(leaf, self)

	def _validate_important_dates_not_in_future(self) -> None:
		"""Important Dates must not be in the future (backend authority)."""
		from frappe.utils import getdate, nowdate

		today = getdate(nowdate())
		date_fields = [
			("received_date", "Received / Issued Date"),
			("cleared_date", "Cleared Date"),
			("returned_date", "Returned Date"),
			("handover_date", "Handover / Endorsement Date"),
			("bounced_date", "Bounced Date"),
			("returned_from_bank_date", "Returned from Bank Date"),
		]

		for fieldname, label in date_fields:
			val = getattr(self, fieldname, None)
			if not val:
				continue
			try:
				dt = getdate(val)
			except Exception:
				# If it's not parseable, existing framework validations will catch it.
				continue
			if dt and dt > today:
				frappe.throw(
					frappe._("{0} cannot be in the future.").format(frappe._(label)),
					title=frappe._("Invalid Date"),
				)

	def _validate_advance_scope_structural(self) -> None:
		"""Advance-mode `advance_scope` structural validation (v1).

		Rules:
		- If `allocation_mode != "advance"`: `advance_scope` must not affect behavior.
		- If `allocation_mode == "advance"`:
		  - `advance_scope` is required (compat default: missing => order_based).
		  - `order_based`:
		    - allocation rows must exist
		    - allocation rows must reference only Purchase Order / Sales Order
		  - `general`:
		    - allocation rows must be empty
		  - If `advance_pool_dim_set` exists, v1 rejects any non-empty value other than:
		    - null, "", "{}", or {} (empty JSON)

		Preserves existing allocation validations by running after `_validate_allocations()`,
		which sanitizes and validates allocation rows.
		"""
		if (getattr(self, "allocation_mode", None) or "").strip() != ALLOCATION_MODE_ADVANCE:
			return

		scope = (getattr(self, "advance_scope", None) or "").strip() or "order_based"
		if scope not in ("order_based", "general"):
			frappe.throw(
				frappe._("Advance Scope must be order_based or general."), title=frappe._("PDC Advance")
			)

		# v1: dims must be empty if the field exists.
		if hasattr(self, "advance_pool_dim_set"):
			raw = getattr(self, "advance_pool_dim_set", None)
			allowed = True
			if raw is None:
				allowed = True
			elif isinstance(raw, dict):
				allowed = len(raw.keys()) == 0
			else:
				s = str(raw).strip()
				allowed = s in ("", "{}", "null")
			if not allowed:
				frappe.throw(
					frappe._(
						"Advance pool dimensions are not supported in v1. Leave Advance Pool Dim Set empty."
					),
					title=frappe._("PDC Advance"),
				)

		alloc_rows = list(getattr(self, "allocations", None) or [])
		if scope == "general":
			if alloc_rows:
				frappe.throw(
					frappe._("General advance PDC must not have any allocation rows."),
					title=frappe._("PDC Advance"),
				)
			return

		# order_based
		if not alloc_rows:
			frappe.throw(
				frappe._("Order-based advance PDC requires at least one allocation row."),
				title=frappe._("PDC Advance"),
			)

		allowed_refs = {"Purchase Order", "Sales Order"}
		for r in alloc_rows:
			mode = (getattr(r, "allocation_mode", None) or "").strip() or ALLOCATION_MODE_ADVANCE
			ref_dt = (getattr(r, "reference_doctype", None) or "").strip()
			ref_nm = (getattr(r, "reference_name", None) or "").strip()
			if mode != ALLOCATION_MODE_ADVANCE:
				frappe.throw(
					frappe._("Order-based advance PDC allocation rows must have allocation_mode = advance."),
					title=frappe._("PDC Advance"),
				)
			if ref_dt not in allowed_refs or not ref_nm:
				frappe.throw(
					frappe._(
						"Order-based advance PDC allocation rows must reference a Purchase Order or Sales Order."
					),
					title=frappe._("PDC Advance"),
				)

	def _validate_handover_date_vs_received_date(self) -> None:
		"""``handover_date`` must be on or after ``received_date`` when both are set (Payable + Receivable)."""
		received = getattr(self, "received_date", None)
		handover = getattr(self, "handover_date", None)
		if not received or not handover:
			return
		if getdate(handover) < getdate(received):
			frappe.throw(
				frappe._(
					"Handover / Endorsement Date cannot be earlier than Received / Issued Date.\n"
					"A cheque cannot be handed over before it is issued or recorded."
				),
				title=frappe._("Invalid Date Sequence"),
			)

	def _validate_receivable_sent_to_bank_vs_received_date(self) -> None:
		"""Receivable: ``sent_to_bank_date`` must be on or after ``received_date`` when both are set.

		Does **not** compare ``sent_to_bank_date`` to ``returned_from_bank_date``. That check is
		transition-scoped on Registered → Sent to Bank only
		(:meth:`_validate_receivable_resend_sent_vs_returned_from_bank`).
		"""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		sent = getattr(self, "sent_to_bank_date", None)
		received = getattr(self, "received_date", None)
		if not sent or not received:
			return
		if getdate(sent) < getdate(received):
			frappe.throw(
				frappe._(
					"Sent to Bank Date cannot be earlier than Received / Issued Date.\n"
					"A receivable cheque cannot be sent for collection before it was received or recorded."
				),
				title=frappe._("Invalid Date Sequence"),
			)

	def _validate_receivable_resend_sent_vs_returned_from_bank(self) -> None:
		"""Receivable re-send only: Registered → Sent to Bank requires ``sent_to_bank_date`` ≥ ``returned_from_bank_date``.

		Runs only on that workflow edge when a prior Return from Bank date exists. Ordinary saves while
		already Sent to Bank / Registered, and Return from Bank (Sent to Bank → Registered), must not
		compare these dates — a Return date later than the previous Send date is valid and expected.
		Opening-balance Return with no ``sent_to_bank_date`` is unaffected (this method never runs on Return).
		"""
		if (self.cheque_direction or "").strip() != CHEQUE_DIRECTION_RECEIVABLE:
			return
		curr = normalize_workflow_state_value(self.workflow_state)
		if curr != WORKFLOW_SENT_TO_BANK:
			return
		prev = normalize_workflow_state_value(self._get_previous_workflow_state_raw())
		if prev != WORKFLOW_REGISTERED:
			return
		returned_from_bank = getattr(self, "returned_from_bank_date", None)
		sent = getattr(self, "sent_to_bank_date", None)
		if not returned_from_bank or not sent:
			return
		if getdate(sent) < getdate(returned_from_bank):
			frappe.throw(
				frappe._(
					"Sent to Bank Date cannot be earlier than Returned from Bank Date.\n"
					"When re-sending a cheque after Return from Bank, enter a new Sent to Bank Date "
					"on or after the Returned from Bank Date."
				),
				title=frappe._("Invalid Date Sequence"),
			)

	def _validate_receivable_cleared_and_bounced_vs_sent_to_bank(self) -> None:
		"""Receivable: clearing or bank bounce cannot precede bank submission when both sides are set."""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		sent = getattr(self, "sent_to_bank_date", None)
		if not sent:
			return
		cleared = getattr(self, "cleared_date", None)
		if cleared and getdate(cleared) < getdate(sent):
			frappe.throw(
				frappe._(
					"Cleared Date cannot be earlier than Sent to Bank Date.\n"
					"Bank settlement cannot occur before the cheque was sent to the bank."
				),
				title=frappe._("Invalid Date Sequence"),
			)
		bounced = getattr(self, "bounced_date", None)
		if bounced and getdate(bounced) < getdate(sent):
			frappe.throw(
				frappe._(
					"Bounced Date cannot be earlier than Sent to Bank Date.\n"
					"A bank rejection cannot be recorded before the cheque was sent to the bank."
				),
				title=frappe._("Invalid Date Sequence"),
			)

	def _validate_returned_date_vs_received_date(self) -> None:
		"""``returned_date`` must be on or after ``received_date`` when both are set (Payable + Receivable)."""
		ret = getattr(self, "returned_date", None)
		received = getattr(self, "received_date", None)
		if not ret or not received:
			return
		if getdate(ret) < getdate(received):
			frappe.throw(
				frappe._(
					"Returned Date cannot be earlier than Received / Issued Date.\n"
					"A business return cannot be recorded before the cheque was received or issued."
				),
				title=frappe._("Invalid Date Sequence"),
			)

	def _validate_payable_cleared_vs_handover_date(self) -> None:
		"""Payable: ``cleared_date`` must be on or after ``handover_date`` when both are set."""
		if self.cheque_direction != CHEQUE_DIRECTION_PAYABLE:
			return
		cleared = getattr(self, "cleared_date", None)
		handover = getattr(self, "handover_date", None)
		if not cleared or not handover:
			return
		if getdate(cleared) < getdate(handover):
			frappe.throw(
				frappe._(
					"Cleared Date cannot be earlier than Handover / Endorsement Date.\n"
					"Bank withdrawal or settlement cannot occur before the cheque was physically handed over."
				),
				title=frappe._("Invalid Date Sequence"),
			)

	def _set_default_party_type_for_payable_if_missing(self) -> None:
		"""Payable cheques default to Supplier (user can change afterwards)."""
		if (self.cheque_direction or "").strip() != CHEQUE_DIRECTION_PAYABLE:
			return
		if not (self.party_type or "").strip():
			self.party_type = "Supplier"

	def _autofill_accounts_from_pdc_settings_if_missing(self) -> None:
		"""Auto-fill document accounts from **PDC Settings** (backend) without overwriting user values.

		- Receivable ``account_paid_to`` defaults from ``PDC Settings.default_cheques_in_hand_account``.
		- ``cheques_in_clearing_account`` defaults from ``PDC Settings.default_cheques_in_clearing_account``.
		"""
		company = (getattr(self, "company", None) or "").strip()
		if not company:
			return
		settings = _get_pdc_settings_for_company(company)
		if not settings:
			return
		if self.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE and not _strip_link_name_or_none(
			getattr(self, "account_paid_to", None)
		):
			ch = _strip_link_name_or_none(settings.get("default_cheques_in_hand_account"))
			if ch:
				self.account_paid_to = ch
		if not _strip_link_name_or_none(getattr(self, "cheques_in_clearing_account", None)):
			clr = _strip_link_name_or_none(settings.get("default_cheques_in_clearing_account"))
			if clr:
				self.cheques_in_clearing_account = clr
		# Payable: allow storing pool/default payable cheque account on account_paid_from if user didn't set it.
		if self.cheque_direction == CHEQUE_DIRECTION_PAYABLE and not _strip_link_name_or_none(
			getattr(self, "account_paid_from", None)
		):
			pool = _strip_link_name_or_none(settings.get("default_payable_cheque_account"))
			if pool:
				self.account_paid_from = pool

	def _validate_receivable_cheques_in_hand_account_required(self) -> None:
		"""Receivable PDC must always have Cheques in Hand account resolved onto the document."""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		if _strip_link_name_or_none(getattr(self, "account_paid_to", None)):
			return
		frappe.throw(
			frappe._(
				"Cheques in Hand Account is required for Receivable cheques. Set Account Paid To or configure Default Cheques in Hand Account in PDC Settings."
			),
			title=frappe._("Cheques in Hand account required"),
		)

	def _validate_allocation_status_awareness(self) -> None:
		"""See :func:`~erpnext_extensions.cheque_management.pdc_allocation.validate_pdc_allocation_workflow_milestone`."""
		validate_pdc_allocation_workflow_milestone(self)

	def _validate_allocations(self) -> None:
		"""Autofill from parent SI/PI link, drop empty rows, sync summary totals, then row rules."""
		autofill_pdc_allocations_from_parent_reference(self)
		sanitize_pdc_allocation_child_rows(self)
		apply_pdc_allocation_row_defaults_from_parent(self)
		# Draft single-row: clamp stale allocation when cheque_amount was reduced (v5.3.1).
		# Runs before summary validation so API/import paths match Desk UX; multi-row unchanged.
		sync_single_pdc_allocation_on_reduced_cheque_amount(self)
		sync_pdc_allocation_summary_amounts(self)
		# Ensure settlement-capacity helpers can exclude this cheque consistently even when invoked
		# indirectly during workflow transitions (before_update_after_submit -> validate()).
		#
		# Unit tests may run without bound frappe.local; keep this defensive.
		try:
			flags = getattr(frappe, "flags", None)
		except RuntimeError:
			flags = None
		if flags is None:
			validate_pdc_allocation_rows(self)
			return
		try:
			prev_excl = getattr(flags, "pdc_settlement_exclude_pdc", None)
		except RuntimeError:
			# frappe.flags is a LocalProxy and may be unbound in bare unit tests.
			validate_pdc_allocation_rows(self)
			return
		try:
			flags.pdc_settlement_exclude_pdc = self.name
			validate_pdc_allocation_rows(self)
		finally:
			flags.pdc_settlement_exclude_pdc = prev_excl

	def get_allocation_effective_from_workflow_state(self) -> str | None:
		"""First workflow state at which allocations are effective; see ``pdc_allocation`` module."""
		return pdc_allocation_effective_milestone_workflow_state(self.cheque_direction)

	def is_allocation_effective(self) -> bool:
		"""See :func:`~erpnext_extensions.cheque_management.pdc_allocation.is_pdc_allocation_effective`."""
		return _is_pdc_allocation_effective(self.cheque_direction, self.workflow_state)

	def is_allocation_draft_only(self) -> bool:
		"""See :func:`~erpnext_extensions.cheque_management.pdc_allocation.is_pdc_allocation_draft_only`."""
		return _is_pdc_allocation_draft_only(self.cheque_direction, self.workflow_state)

	def _sync_allocation_summary_amounts(self) -> None:
		"""See :func:`~erpnext_extensions.cheque_management.pdc_allocation.sync_pdc_allocation_summary_amounts`."""
		sync_pdc_allocation_summary_amounts(self)

	def _pdc_pre_save_workflow_sequence(self) -> None:
		"""Run **before** ``db_update`` on every successful validation (draft, submit, update_after_submit).

		Order (must stay stable for accounting and status):

		1. **Detect** prior ``workflow_state`` (Frappe snapshot — :meth:`_capture_previous_workflow_for_accounting`).
		2. **Validate** transition and workflow-shaped rules (state machine, bounce, endorsement, …).
		3. **Update** ``cheque_status`` from ``workflow_state`` and assert consistency.
		4. If moving to **Cleared** with **journal_entry** policy, require a buildable payload
		   (:meth:`_validate_clearing_accounting_payload`) so ``db_update`` cannot leave Cleared without a postable JE.

		Does **not** insert vouchers; that runs in :meth:`_pdc_post_save_accounting_sequence`.
		"""
		self._capture_previous_workflow_for_accounting()
		self._validate_workflow_transition()
		self._validate_received_date_required_for_receivable_registered()
		self._validate_received_date_required_for_payable_registered()
		self._validate_received_date_required_for_payable_issued()
		self._validate_endorsement_allowed_per_settings()
		self._validate_advance_recognition_effective_stage_supported()
		self._validate_bounced_workflow_state()
		self._validate_endorsed_workflow_state()
		# Endorsement audit + normalized holder (after transition and holder rules pass; runs on update_after_submit too).
		self._sync_holder_fields_for_endorsement()
		self._append_holder_history_on_endorsement()
		self._validate_issued_workflow_state()
		self._validate_sent_to_bank_workflow_state()
		self._validate_receivable_resend_sent_vs_returned_from_bank()
		self._validate_bank_account_for_workflow_state()
		self._validate_bank_account_for_cleared_workflow_state()
		self._validate_bank_gl_account_for_cleared_workflow_state()
		self._validate_receivable_bank_account_is_company_account()
		self._validate_payable_bank_account_is_company_account()
		self._sync_cheque_status_from_workflow_state()
		self._sync_is_at_bank_from_workflow_state()
		self._validate_cheque_status_matches_workflow_state()
		self._validate_clearing_accounting_payload()

	def _sync_is_at_bank_from_workflow_state(self):
		"""Set `is_at_bank` for operational monitoring (Receivable: Sent to Bank / In Clearing)."""
		try:
			direction = (self.cheque_direction or "").strip()
			ws = (self.workflow_state or "").strip()
			if direction != "Receivable":
				self.is_at_bank = 0
				return
			self.is_at_bank = 1 if ws in ("Sent to Bank", "In Clearing") else 0
		except Exception:
			# Never block lifecycle; this is a derived helper field only.
			self.is_at_bank = 0

	def _validate_advance_recognition_effective_stage_supported(self) -> None:
		"""Task 5 guardrail: do not allow silent non-working effective stage selections.

		- Payable advance supports `register` and `issue`
		- Receivable advance supports `register` only (no Issued edge in receivable workflow by design)
		"""
		if (getattr(self, "allocation_mode", None) or "").strip() != ALLOCATION_MODE_ADVANCE:
			return
		eff = (getattr(self, "effective_stage_for_advance_recognition", None) or "register").strip().lower()
		if eff not in ("register", "issue"):
			frappe.throw(
				frappe._("Effective Stage for Advance Recognition must be 'register' or 'issue'."),
				title=frappe._("Invalid Advance Recognition Stage"),
			)
		if (
			getattr(self, "cheque_direction", None) or ""
		).strip() == CHEQUE_DIRECTION_RECEIVABLE and eff == "issue":
			frappe.throw(
				frappe._(
					"Advance Recognition stage 'issue' is not supported for Receivable cheques. "
					"Receivable advance recognition posts only at Register (Draft → Registered)."
				),
				title=frappe._("Unsupported Advance Recognition Stage"),
			)

	def _reset_party_if_party_type_changed(self):
		"""If party_type changes, party may need re-selection.

		Do not blindly clear `party` on saved drafts switching direction: if the current `party`
		is already valid for the new `party_type`, keep it.
		"""
		before = self.get_doc_before_save()
		if not before:
			return
		if before.party_type != self.party_type:
			# Keep party if it exists as a record of the new type; else clear.
			pt = (self.party_type or "").strip()
			p = (self.party or "").strip()
			if not pt or not p:
				self.party = None
				return
			try:
				if frappe.db.exists(pt, p):
					return
			except Exception:
				# If party_type is invalid as a DocType, treat as mismatch.
				pass
			self.party = None

	def _validate_drawer_bank(self):
		"""Drawer bank is required for receivable cheques."""
		if self.cheque_direction == "Receivable" and not self.drawer_bank_name:
			frappe.throw(frappe._("Drawer Bank Name is required for Receivable cheques."))

	def _get_previous_workflow_state_raw(self):
		"""``workflow_state`` before this save: Frappe snapshot first, else DB for edge cases (e.g. import)."""
		before = self.get_doc_before_save()
		if before is not None:
			return before.get("workflow_state")
		if self.name and frappe.db.exists("Post Dated Cheque", self.name):
			return frappe.db.get_value("Post Dated Cheque", self.name, "workflow_state")
		return None

	def _get_previous_workflow_state_for_accounting(self) -> str | None:
		"""Prior ``workflow_state`` for transition / accounting (must match Frappe save cycle).

		Uses :meth:`~frappe.model.document.Document.get_value_before_save` → the document snapshot
		loaded in ``check_if_latest`` / ``load_doc_before_save``. That snapshot stays on the doc through
		``on_update`` / ``on_update_after_submit`` **before** any nested reload.

		Never uses the database row here: after ``db_update``, the row can already hold the **new**
		workflow state, so ``get_value`` would mis-report the previous step and break accounting
		(e.g. Registered→Sent to Bank misread as Draft→Sent to Bank).

		On brand-new insert there is no snapshot → ``None`` (normalized to **Draft** in policy helpers).
		"""
		prev = self.get_value_before_save("workflow_state")
		if prev is not None:
			return prev
		before = self.get_doc_before_save()
		if before is not None:
			return before.get("workflow_state")
		return None

	def _capture_previous_workflow_for_accounting(self):
		"""Step 1 (pre-save): store snapshot from :meth:`_get_previous_workflow_state_for_accounting` for logs/cache."""
		from erpnext_extensions.cheque_management.pdc_lifecycle_events import (
			snapshot_pdc_operational_fields,
		)

		self._pdc_previous_workflow_for_accounting = self._get_previous_workflow_state_for_accounting()
		before = self.get_doc_before_save()
		self._pdc_lifecycle_pre_event_snapshot = snapshot_pdc_operational_fields(before or self)
		_pdc_accounting_logger.debug(
			"Captured previous_workflow_state for accounting: %r (doc %s)",
			self._pdc_previous_workflow_for_accounting,
			self.name or "(new)",
		)

	def _pdc_post_save_accounting_sequence(self) -> None:
		"""After successful ``db_update`` when ``workflow_state`` changed (``on_update`` / ``on_update_after_submit``).

		PDC lifecycle is **Journal Entry only** — vouchers are recorded in ``journal_references``.

		Preconditions: steps 1–3 already ran in :meth:`validate` via :meth:`_pdc_pre_save_workflow_sequence`.

		4. Re-read prior ``workflow_state`` from the same Frappe snapshot used before save.
		5. If policy is ``journal_entry``, create **at most one** JE per transition
		   (``cheque_name|cheque_direction|from_state|to_state`` on ``journal_references``; legacy suffix
		   ``direction|from|to`` still recognized — skip if voucher already linked;
		   see ``pdc_accounting_idempotency``).

		Skips when ``flags.skip_pdc_accounting_orchestration`` is set (nested saves from posting services).

		Skips when ``frappe.flags.in_cheque_opening_import`` is set (Cheque Opening Import workflow
		walk only — not ``is_opening_import`` on the document, so post-import transitions still post).
		"""
		if getattr(self.flags, "skip_pdc_accounting_orchestration", False):
			return
		if getattr(frappe.flags, "in_cheque_opening_import", False):
			return
		if not self.name:
			return
		prev_raw = getattr(self, "_pdc_previous_workflow_for_accounting", None)
		if prev_raw is None:
			prev_raw = self._get_previous_workflow_state_for_accounting()
		if prev_raw is None:
			prev_raw = self._get_previous_workflow_state_raw()
		prev_norm = normalize_workflow_state_value(prev_raw)
		curr_norm = normalize_workflow_state_value(self.workflow_state)
		if prev_norm != curr_norm:
			frappe.logger("erpnext_extensions.cheque_management").info(
				"PDC workflow transition | name=%s | cheque_direction=%s | previous_workflow_state=%r | workflow_state=%r",
				self.name,
				getattr(self, "cheque_direction", None),
				prev_raw,
				self.workflow_state,
			)
		if prev_norm == curr_norm:
			return
		_pdc_accounting_logger.debug("Detected transition: %s → %s", prev_norm, curr_norm)
		action = get_accounting_action(self, prev_raw)
		_pdc_accounting_logger.info(
			"[PDC_ACCOUNTING_TRACE] post_save | pdc=%s | previous_workflow_state=%r | workflow_state=%r | "
			"cheque_direction=%s | accounting_action=%s",
			self.name,
			prev_raw,
			self.workflow_state,
			getattr(self, "cheque_direction", None),
			"none" if action == PDC_ACCOUNTING_NO_DOCUMENT else action,
		)
		_pdc_accounting_logger.debug(
			"Accounting action: %s",
			"none" if action == PDC_ACCOUNTING_NO_DOCUMENT else action,
		)
		pre_event_snapshot = getattr(self, "_pdc_lifecycle_pre_event_snapshot", None)
		if action == PDC_ACCOUNTING_NO_DOCUMENT:
			self._capture_lifecycle_event_after_transition(
				prev_raw, prev_norm, curr_norm, action, pre_event_snapshot
			)
			return

		# Posting dates must follow business event dates (not today/workflow timestamp).
		# - Receivable Draft→Registered: use received_date
		# - Any → Cleared: use cleared_date
		# - Any → Returned: use returned_date only
		# - Any → Bounced: use bounced_date only (bank rejection after Sent to Bank)
		# - Receivable → Sent to Bank: use sent_to_bank_date only (bank handover for collection)
		# - Receivable Sent to Bank → Registered (Return from Bank): use returned_from_bank_date only
		# - Payable Draft→Registered: use received_date (register / settlement event)
		# - Receivable → Endorsed: use handover_date only (endorsement / transfer)
		# Fallback only for other transitions that are not business-dated yet.
		posting_date = None
		if curr_norm == WORKFLOW_CLEARED:
			posting_date = getattr(self, "cleared_date", None)
		elif curr_norm == WORKFLOW_RETURNED:
			posting_date = getattr(self, "returned_date", None)
		elif curr_norm == WORKFLOW_BOUNCED:
			posting_date = getattr(self, "bounced_date", None)
		elif (
			curr_norm == WORKFLOW_SENT_TO_BANK
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			posting_date = getattr(self, "sent_to_bank_date", None)
		elif (
			curr_norm == WORKFLOW_REGISTERED
			and prev_norm == WORKFLOW_SENT_TO_BANK
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			posting_date = getattr(self, "returned_from_bank_date", None)
		elif (
			curr_norm == WORKFLOW_ENDORSED
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			posting_date = getattr(self, "handover_date", None)
		elif (
			curr_norm == WORKFLOW_REGISTERED
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			posting_date = getattr(self, "received_date", None)
		elif (
			curr_norm == WORKFLOW_REGISTERED
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_PAYABLE
			and prev_norm == WORKFLOW_DRAFT
		):
			posting_date = getattr(self, "received_date", None)
		elif (
			curr_norm == WORKFLOW_ISSUED and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_PAYABLE
		):
			# Operational Issued transition does not post a JE; handover is still mandatory for validation.
			posting_date = getattr(self, "handover_date", None)
		if curr_norm == WORKFLOW_RETURNED:
			if not posting_date:
				frappe.throw(
					frappe._(
						"Returned Date is mandatory when Workflow State is Returned. "
						"Returned is a business return, not a bank bounce — use Workflow State Bounced for bank rejection."
					),
					title=frappe._("Missing Returned Date"),
				)
		elif curr_norm == WORKFLOW_BOUNCED:
			if not posting_date:
				frappe.throw(
					frappe._(
						"Bounced Date is mandatory when Workflow State is Bounced. "
						"Enter the date of bank rejection (dishonour after Sent to Bank). "
						"This is not a business return — use Returned for return to party before completion."
					),
					title=frappe._("Missing Bounced Date"),
				)
		elif (
			curr_norm == WORKFLOW_SENT_TO_BANK
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			if not posting_date:
				frappe.throw(
					frappe._(
						"Sent to Bank Date is mandatory when Workflow State is Sent to Bank (Receivable). "
						"Enter the date the cheque was delivered to the bank for collection — do not use workflow time or today."
					),
					title=frappe._("Missing Sent to Bank Date"),
				)
		elif (
			curr_norm == WORKFLOW_REGISTERED
			and prev_norm == WORKFLOW_SENT_TO_BANK
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			if not posting_date:
				frappe.throw(
					frappe._(
						"Returned from Bank Date is mandatory when returning a receivable cheque "
						"from Sent to Bank to Registered (Return from Bank). "
						"Enter the date the cheque was returned to the cashbox — do not auto-fill or use today."
					),
					title=frappe._("Missing Returned from Bank Date"),
				)
		elif (
			curr_norm == WORKFLOW_ISSUED and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_PAYABLE
		):
			if not posting_date:
				frappe.throw(
					frappe._(
						"Handover / Endorsement Date is mandatory when Workflow State is Issued (Payable). "
						"Enter the date the cheque was physically handed over to the payee — not Received / Issued Date or today."
					),
					title=frappe._("Missing Handover Date"),
				)
		elif (
			curr_norm == WORKFLOW_ENDORSED
			and (self.cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		):
			if not posting_date:
				frappe.throw(
					frappe._(
						"Handover / Endorsement Date is mandatory when Workflow State is Endorsed. "
						"Enter the date of endorsement or transfer — not Received / Issued Date."
					),
					title=frappe._("Missing Handover Date"),
				)
		else:
			# Fallback for transitions that don't have a dedicated business date yet.
			# Prefer user-provided/meaningful dates on the doc; only then fall back to today.
			# Do not use handover_date, returned_date, bounced_date, or sent_to_bank_date here —
			# only their workflows use them.
			posting_date = (
				posting_date
				or getattr(self, "cheque_due_date", None)
				or getattr(self, "received_date", None)
				or getattr(self, "cleared_date", None)
				or getdate()
			)
		ch_dir = (self.cheque_direction or "").strip()

		from erpnext_extensions.cheque_management.pdc_journal_entry_service import (
			get_existing_journal_entry_for_transition,
			post_pdc_transition_journal_entry,
		)

		created = False
		if action == PDC_ACCOUNTING_JOURNAL_ENTRY:
			existing_je = get_existing_journal_entry_for_transition(
				self.name, ch_dir, prev_raw, self.workflow_state
			)
			if existing_je:
				_pdc_accounting_logger.debug(
					"PDC accounting: Journal Entry already exists for %s → %s (%s), skip duplicate",
					prev_norm,
					curr_norm,
					existing_je,
				)
			else:
				_pdc_accounting_logger.debug(
					"Calling post_pdc_transition_journal_entry for %s → %s",
					prev_norm,
					curr_norm,
				)
				created = bool(
					post_pdc_transition_journal_entry(
						self, prev_raw, self.workflow_state, posting_date=posting_date
					)
				)
				if not created:
					_pdc_accounting_logger.debug(
						"post_pdc_transition_journal_entry returned no JE (missing payload/accounts or build skipped)"
					)

		if created:
			self.reload()

		self._capture_lifecycle_event_after_transition(
			prev_raw, prev_norm, curr_norm, action, pre_event_snapshot
		)

	def _capture_lifecycle_event_after_transition(
		self,
		prev_raw,
		prev_norm: str,
		curr_norm: str,
		action: str | None,
		pre_event_snapshot: str | None,
	) -> None:
		from erpnext_extensions.cheque_management.pdc_lifecycle_events import (
			capture_pdc_lifecycle_event,
		)

		workflow_action = (
			getattr(self, "workflow_action_name", None)
			or getattr(self, "_action", None)
			or ""
		)
		capture_pdc_lifecycle_event(
			self,
			prev_norm,
			curr_norm,
			action,
			snapshot_json=pre_event_snapshot,
			action=str(workflow_action or ""),
		)

	def _validate_sayad_registration_per_settings(self) -> None:
		"""Enforce Sayad policy per-company (PDC Settings).

		Rules:
		- If PDC Settings.require_sayad_registration = 1: ``sayad_code`` is required.
		- If enabled: ``sayad_registered`` is required only at lifecycle checkpoints:
		  - Receivable: before becoming **Registered**
		  - Payable: before becoming **Registered**
		"""
		if not _pdc_company_policy_flags(getattr(self, "company", None))["require_sayad_registration"]:
			return

		code = (getattr(self, "sayad_code", None) or "").strip()
		if not code:
			frappe.throw(
				frappe._(
					"Sayad Code is required because Require Sayad Registration is enabled in PDC Settings."
				),
				title=frappe._("Sayad registration required"),
			)

		prev = normalize_workflow_state_value(self._get_previous_workflow_state_raw())
		curr = normalize_workflow_state_value(getattr(self, "workflow_state", None))
		if prev == curr:
			return

		if self.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE and curr == WORKFLOW_REGISTERED:
			if not cint(getattr(self, "sayad_registered", 0)):
				frappe.throw(
					frappe._(
						"Sayad Registered must be checked before registering a receivable cheque because Require Sayad Registration is enabled in PDC Settings."
					),
					title=frappe._("Sayad registration required"),
				)

		if self.cheque_direction == CHEQUE_DIRECTION_PAYABLE and curr == WORKFLOW_REGISTERED:
			if not cint(getattr(self, "sayad_registered", 0)):
				frappe.throw(
					frappe._(
						"Sayad Registered must be checked before registering a payable cheque because Require Sayad Registration is enabled in PDC Settings."
					),
					title=frappe._("Sayad registration required"),
				)

	def _validate_endorsement_allowed_per_settings(self) -> None:
		"""Block **Registered → Endorsed** when **Allow Endorsement** is disabled in PDC Settings."""
		if not self._transitioning_to_endorsed():
			return
		if _pdc_company_policy_flags(getattr(self, "company", None))["allow_endorsement"]:
			return
		frappe.throw(
			frappe._(
				"Transition to Endorsed is not allowed: Allow Endorsement is disabled in PDC Settings for this company."
			),
			title=frappe._("Endorsement not allowed"),
		)

	def _validate_workflow_transition(self):
		"""Enforce allowed ``workflow_state`` transitions via ``pdc_workflow_state_machine``.

		Uses DocType field ``cheque_direction`` as cheque type (**Receivable** / **Payable**).
		Terminal states (**Cleared** / **Cancelled** / **Replaced**) are locked even if
		direction is not set yet (see :func:`get_pdc_workflow_transition_validation_error`).
		Bounced / Endorsed / Issued / Sent to Bank rules are also enforced in
		:meth:`_validate_bounced_workflow_state`, :meth:`_validate_endorsed_workflow_state`,
		:meth:`_validate_issued_workflow_state`, and :meth:`_validate_sent_to_bank_workflow_state`.
		Accounting documents are created in :meth:`_pdc_post_save_accounting_sequence` (``on_update``), not here.
		"""
		if getattr(frappe.flags, "in_pdc_workflow_rollback", None):
			return
		prev_raw = self._get_previous_workflow_state_raw()
		cheque_type = self.cheque_direction if self.cheque_direction in ("Receivable", "Payable") else ""
		err = get_pdc_workflow_transition_validation_error(
			cheque_type,
			prev_raw,
			self.workflow_state,
		)
		if err:
			frappe.throw(frappe._(err), title=frappe._("Invalid Workflow State"))

	def _validate_received_date_required_for_receivable_registered(self) -> None:
		"""Receivable cheques must have ``received_date`` before becoming **Registered**."""
		if (self.cheque_direction or "").strip() != CHEQUE_DIRECTION_RECEIVABLE:
			return
		curr = normalize_workflow_state_value(self.workflow_state)
		if curr != WORKFLOW_REGISTERED:
			return
		prev = normalize_workflow_state_value(self._get_previous_workflow_state_raw())
		if prev == curr:
			return
		if not getattr(self, "received_date", None):
			frappe.throw(
				frappe._("Received Date is mandatory before registering a receivable cheque."),
				title=frappe._("Missing Received Date"),
			)

	def _validate_received_date_required_for_payable_registered(self) -> None:
		"""Payable cheques must have ``received_date`` before **Registered** (Draft → Registered settlement)."""
		if (self.cheque_direction or "").strip() != CHEQUE_DIRECTION_PAYABLE:
			return
		curr = normalize_workflow_state_value(self.workflow_state)
		if curr != WORKFLOW_REGISTERED:
			return
		prev = normalize_workflow_state_value(self._get_previous_workflow_state_raw())
		if prev == curr:
			return
		if prev != WORKFLOW_DRAFT:
			return
		if not getattr(self, "received_date", None):
			frappe.throw(
				frappe._(
					"Received / Issued Date is mandatory before registering a payable cheque (Draft → Registered): "
					"it is the posting date for supplier / Purchase Invoice settlement."
				),
				title=frappe._("Missing Received / Issued Date"),
			)

	def _validate_received_date_required_for_payable_issued(self) -> None:
		"""Payable cheques must have ``received_date`` (preparation / internal issue date) before **Issued**.

		Distinct from ``handover_date`` (physical delivery to payee), validated in
		:meth:`_validate_issued_workflow_state`.
		"""
		if (self.cheque_direction or "").strip() != CHEQUE_DIRECTION_PAYABLE:
			return
		curr = normalize_workflow_state_value(self.workflow_state)
		if curr != WORKFLOW_ISSUED:
			return
		prev = normalize_workflow_state_value(self._get_previous_workflow_state_raw())
		if prev == curr:
			return
		if not getattr(self, "received_date", None):
			frappe.throw(
				frappe._(
					"Received / Issued Date is mandatory before Issued (Payable): record when the cheque was prepared or internally issued. "
					"Physical handover to the payee is Handover / Endorsement Date — set that field separately."
				),
				title=frappe._("Missing Received / Issued Date"),
			)

	def _validate_bounced_workflow_state(self):
		"""**Bounced** = bank rejection after **Sent to Bank** or **Assigned to Bank for Debt Purchase**
		(not **Returned**, which is a business return).

		Requires ``bounced_date``. Transition rules match :data:`PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK`
		from :func:`get_pdc_workflow_transition_validation_error` (validated first in
		:meth:`_validate_workflow_transition`).
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_BOUNCED:
			return
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			frappe.throw(
				frappe._(PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK),
				title=frappe._("Invalid Bounced workflow state"),
			)
		prev_raw = self._get_previous_workflow_state_raw()
		if not is_workflow_previous_empty(prev_raw):
			prev = normalize_workflow_state_value(prev_raw)
			if prev not in (WORKFLOW_SENT_TO_BANK, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED):
				frappe.throw(
					frappe._(PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK),
					title=frappe._("Invalid Bounced workflow state"),
				)
		if not getattr(self, "bounced_date", None):
			frappe.throw(
				frappe._(
					"Bounced Date is mandatory when Workflow State is Bounced. "
					"Enter the bank rejection date — dishonour after Sent to Bank or "
					"Assigned to Bank for Debt Purchase. "
					"This is not a business return (use Returned and Returned Date for that)."
				),
				title=frappe._("Missing Bounced Date"),
			)

	def _transitioning_to_endorsed(self) -> bool:
		"""True when this save moves a Receivable PDC into ``workflow_state`` **Endorsed**."""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return False
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_ENDORSED:
			return False
		prev = normalize_workflow_state_value(self._get_previous_workflow_state_raw())
		return prev != WORKFLOW_ENDORSED

	def _validate_endorsed_workflow_state(self):
		"""**Endorsed** is only valid for Receivable cheques (not Payable).

		Message: :data:`PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY` — same rule as
		:func:`get_pdc_workflow_transition_validation_error` (see :meth:`_validate_workflow_transition`).
		For **Receivable** with **Endorsed**, ``holder_party_type`` and ``holder_party`` must both be set
		and must reference an existing document (canonical current holder after endorsement).
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_ENDORSED:
			return
		if self.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
			frappe.throw(
				frappe._(PDC_VALIDATION_ENDORSED_RECEIVABLE_ONLY),
				title=frappe._("Invalid Endorsed workflow state"),
			)
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		ht = _strip_link_name_or_none(self.holder_party_type)
		hp = _strip_link_name_or_none(self.holder_party)
		if not ht or not hp:
			frappe.throw(
				frappe._("Holder Party Type and Holder Party are required when Workflow State is Endorsed."),
				title=frappe._("Invalid Endorsed workflow state"),
			)
		if not frappe.db.exists(ht, hp):
			frappe.throw(
				frappe._("Invalid Holder Party: {0} {1} was not found.").format(ht, hp),
				title=frappe._("Invalid Endorsed workflow state"),
			)
		if not getattr(self, "handover_date", None):
			frappe.throw(
				frappe._(
					"Handover / Endorsement Date is mandatory when Workflow State is Endorsed. "
					"Enter the date the cheque was endorsed or transferred to the new party."
				),
				title=frappe._("Missing Handover Date"),
			)

	def _sync_holder_fields_for_endorsement(self):
		"""Keep ``holder_party_type`` / ``holder_party`` normalized while **Endorsed** (Receivable).

		Runs in ``before_save`` after validation so stripped values persist and Holder History matches.
		"""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_ENDORSED:
			return
		ht = _strip_link_name_or_none(self.holder_party_type)
		hp = _strip_link_name_or_none(self.holder_party)
		if not ht or not hp:
			return
		self.holder_party_type = ht
		self.holder_party = hp

	def _append_holder_history_on_endorsement(self):
		"""Append a **PDC Holder History** row when a Receivable PDC transitions into **Endorsed**.

		Runs from :meth:`_pdc_pre_save_workflow_sequence` (draft save and ``update_after_submit``), **after**
		transition validation, so history is not written for invalid moves.

		**Previous** holder is taken from the pre-save document (``holder_party*`` with fallback to ``party*``).
		**New** holder uses canonical ``holder_party*`` after :meth:`_sync_holder_fields_for_endorsement`.
		"""
		if not self._transitioning_to_endorsed():
			return
		new_ht = _strip_link_name_or_none(self.holder_party_type)
		new_hn = _strip_link_name_or_none(self.holder_party)
		if not new_ht or not new_hn:
			return
		before = self.get_doc_before_save()
		prev_ht, prev_hn = _resolve_holder_party_type_and_party(before)
		if prev_ht and not prev_hn:
			prev_ht = None
		self.append(
			"holder_history",
			{
				"date": now_datetime(),
				"previous_holder_type": prev_ht,
				"previous_holder": prev_hn if prev_ht else None,
				"new_holder_type": new_ht,
				"new_holder": new_hn,
				"reason": frappe._(PDC_HOLDER_HISTORY_REASON_ENDORSEMENT),
			},
		)

	def _validate_issued_workflow_state(self):
		"""**Issued** is only valid for Payable cheques (not Receivable).

		Requires ``handover_date`` (physical delivery to payee). ``received_date`` is validated separately
		as preparation / internal issue date (:meth:`_validate_received_date_required_for_payable_issued`).

		Message: :data:`PDC_VALIDATION_ISSUED_PAYABLE_ONLY` — same rule as
		:func:`get_pdc_workflow_transition_validation_error` (see :meth:`_validate_workflow_transition`).
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_ISSUED:
			return
		if self.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
			frappe.throw(
				frappe._(PDC_VALIDATION_ISSUED_PAYABLE_ONLY),
				title=frappe._("Invalid Issued workflow state"),
			)
		if not getattr(self, "handover_date", None):
			frappe.throw(
				frappe._(
					"Handover / Endorsement Date is mandatory when Workflow State is Issued (Payable): "
					"enter the date the cheque was physically given to the payee (not the preparation date in Received / Issued Date)."
				),
				title=frappe._("Missing Handover Date"),
			)

	def _validate_sent_to_bank_workflow_state(self):
		"""**Sent to Bank** is only valid for Receivable cheques (not Payable).

		Requires ``sent_to_bank_date`` (date handed to the bank for collection — not inferred from workflow time).

		Message: :data:`PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY` — same rule as
		:func:`get_pdc_workflow_transition_validation_error` (see :meth:`_validate_workflow_transition`).
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_SENT_TO_BANK:
			return
		if self.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
			frappe.throw(
				frappe._(PDC_VALIDATION_SENT_TO_BANK_RECEIVABLE_ONLY),
				title=frappe._("Invalid Sent to Bank workflow state"),
			)
		if not getattr(self, "sent_to_bank_date", None):
			frappe.throw(
				frappe._(
					"Sent to Bank Date is mandatory when Workflow State is Sent to Bank. "
					"Enter the date the receivable cheque was delivered or submitted to the bank for collection."
				),
				title=frappe._("Missing Sent to Bank Date"),
			)
		# Sent to Bank requires a clearing account (either per-document override or company defaults).
		acc = resolve_pdc_accounts_for_journal(self)
		if not acc.get("cheques_in_clearing"):
			frappe.throw(
				frappe._(
					"Cheques in Clearing Account is required when Workflow State is Sent to Bank. "
					"Set Post Dated Cheque → Cheques in Clearing Account or configure Default Cheques in Clearing Account in PDC Settings."
				),
				title=frappe._("Clearing account required"),
			)

	def _set_default_bank_account_for_receivable(self) -> None:
		"""If **Receivable** and **Bank Account** is empty, set company default bank (if any).

		Uses **Bank Account** rows with ``company`` = PDC company, ``is_company_account``,
		and ``is_default`` (ERPNext field — not ``is_default_account``). If several match,
		picks the first by name; if none match, leaves the field empty (no error).
		"""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		if _strip_link_name_or_none(self.bank_account):
			return
		if not self.company:
			return
		names = frappe.get_all(
			"Bank Account",
			filters={
				"company": self.company,
				"is_company_account": 1,
				"is_default": 1,
			},
			pluck="name",
			order_by="name asc",
			limit=1,
		)
		if names:
			self.bank_account = names[0]

	def _validate_bank_account_for_workflow_state(self):
		"""Require ``bank_account`` when the workflow stage needs a settlement bank.

		* **Payable:** Issued (Cleared is validated in :meth:`_validate_bank_account_for_cleared_workflow_state`)
		* **Receivable:** Sent to Bank (Cleared: same dedicated validator)
		"""
		if self.cheque_direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
			return
		ws = normalize_workflow_state_value(self.workflow_state)
		if self.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
			if ws == WORKFLOW_ISSUED and not self.bank_account:
				frappe.throw(
					frappe._("Bank Account is required for Payable cheques when Workflow State is Issued."),
					title=frappe._("Bank Account required"),
				)
		elif self.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
			if ws == WORKFLOW_SENT_TO_BANK and not self.bank_account:
				frappe.throw(
					frappe._(
						"Bank Account is required for Receivable cheques when Workflow State is Sent to Bank."
					),
					title=frappe._("Bank Account required"),
				)

	def _validate_bank_account_for_cleared_workflow_state(self) -> None:
		"""**Cleared:** settlement bank must be set and must be a **company** bank for this PDC company."""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_CLEARED:
			return
		if self.cheque_direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
			return
		msg = frappe._("Bank account is required and must be a company account for clearing")
		ba = _strip_link_name_or_none(self.bank_account)
		if not ba:
			frappe.throw(msg, title=frappe._("Bank account required for clearing"))
		row = frappe.db.get_value(
			"Bank Account",
			ba,
			["is_company_account", "company"],
			as_dict=True,
		)
		if not row or not cint(row.get("is_company_account")):
			frappe.throw(msg, title=frappe._("Bank account required for clearing"))
		ba_company = (row.get("company") or "").strip()
		doc_company = (self.company or "").strip()
		if ba_company != doc_company:
			frappe.throw(msg, title=frappe._("Bank account required for clearing"))

	def _validate_bank_gl_account_for_cleared_workflow_state(self) -> None:
		"""**Cleared:** require a resolvable, real **Bank** GL account from the selected **Bank Account**.

		This is enforced at the Document layer so the workflow cannot reach **Cleared** without a
		bank-facing Journal Entry target ledger.
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_CLEARED:
			return
		if self.cheque_direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
			return
		bank_gl = _pdc_bank_gl_account(self)
		if not bank_gl:
			frappe.throw(
				frappe._("The linked **Bank Account** must have a company **Account** (GL) in ERPNext."),
				title=frappe._("Bank account required for clearing"),
			)
		if not frappe.db.exists("Account", bank_gl):
			frappe.throw(
				frappe._(
					"Bank Account {0} resolves to GL account {1}, but that Account does not exist. "
					"Select a Bank Account linked to a real Bank ledger."
				).format(self.bank_account, bank_gl),
				title=frappe._("Invalid bank ledger for clearing"),
			)
		_pdc_validate_clearing_bank_ledger_account(self, bank_gl)

	def _validate_receivable_bank_account_is_company_account(self):
		"""Receivable PDCs must link a **Bank Account** that is a company account for this PDC's company.

		Mirrors the Desk link filter in ``post_dated_cheque.js``; blocks API / import / manual bypass.
		Cleared uses :meth:`_validate_bank_account_for_cleared_workflow_state` (single user-facing message).
		"""
		if self.cheque_direction != CHEQUE_DIRECTION_RECEIVABLE:
			return
		if normalize_workflow_state_value(self.workflow_state) == WORKFLOW_CLEARED:
			return
		ba = _strip_link_name_or_none(self.bank_account)
		if not ba:
			return
		row = frappe.db.get_value(
			"Bank Account",
			ba,
			["is_company_account", "company"],
			as_dict=True,
		)
		if not row:
			return
		if not cint(row.get("is_company_account")):
			frappe.throw(
				frappe._("For receivable cheques, bank account must be a company account"),
				title=frappe._("Invalid Bank Account"),
			)
		ba_company = (row.get("company") or "").strip()
		doc_company = (self.company or "").strip()
		if ba_company != doc_company:
			frappe.throw(
				frappe._("For receivable cheques, bank account must be a company account"),
				title=frappe._("Invalid Bank Account"),
			)

	def _validate_payable_bank_account_is_company_account(self):
		"""Payable PDCs must link a **Bank Account** that is a company account for this PDC's company.

		**Cleared** always runs :meth:`_validate_bank_account_for_cleared_workflow_state` (single message);
		this covers earlier stages when a bank_account is provided.
		"""
		if self.cheque_direction != CHEQUE_DIRECTION_PAYABLE:
			return
		if normalize_workflow_state_value(self.workflow_state) == WORKFLOW_CLEARED:
			return
		ba = _strip_link_name_or_none(self.bank_account)
		if not ba:
			return
		row = frappe.db.get_value(
			"Bank Account",
			ba,
			["is_company_account", "company"],
			as_dict=True,
		)
		if not row:
			return
		if not cint(row.get("is_company_account")):
			frappe.throw(
				frappe._("For payable cheques, bank account must be a company account"),
				title=frappe._("Invalid Bank Account"),
			)
		ba_company = (row.get("company") or "").strip()
		doc_company = (self.company or "").strip()
		if ba_company != doc_company:
			frappe.throw(
				frappe._("For payable cheques, bank account must be a company account"),
				title=frappe._("Invalid Bank Account"),
			)

	def _validate_returned_workflow_state(self):
		"""**Returned** is a business return, not a bank bounce (use **Bounced** for bank rejection).

		Requires ``return_reason``. Operational ``cheque_status``: Receivable → *Returned to Customer*;
		Payable → *Returned from Payee* (see :func:`map_workflow_state_to_cheque_status`). Runs after
		``cheque_status`` sync so labels can be checked.
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_RETURNED:
			return
		if self.cheque_direction not in (CHEQUE_DIRECTION_RECEIVABLE, CHEQUE_DIRECTION_PAYABLE):
			frappe.throw(
				frappe._("Workflow State Returned requires Cheque Direction Receivable or Payable."),
				title=frappe._("Invalid Returned workflow state"),
			)
		if not (self.return_reason or "").strip():
			frappe.throw(
				frappe._(
					"Return Reason is mandatory when Workflow State is Returned. "
					"Returned is a business return (not a bank bounce — use Workflow State Bounced for bank rejection)."
				),
				title=frappe._("Missing Return Reason"),
			)
		if not getattr(self, "returned_date", None):
			frappe.throw(
				frappe._(
					"Returned Date is mandatory when Workflow State is Returned. "
					"Returned is a business return, not a bank bounce — use Workflow State Bounced for bank rejection."
				),
				title=frappe._("Missing Returned Date"),
			)
		status = (self.cheque_status or "").strip()
		if self.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
			if status != CHEQUE_STATUS_RETURNED_TO_CUSTOMER:
				frappe.throw(
					frappe._(
						"For Receivable cheques, Workflow State Returned must show Cheque Status «{0}»."
					).format(CHEQUE_STATUS_RETURNED_TO_CUSTOMER),
					title=frappe._("Returned workflow state"),
				)
		elif self.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
			if status != CHEQUE_STATUS_RETURNED_FROM_PAYEE:
				frappe.throw(
					frappe._(
						"For Payable cheques, Workflow State Returned must show Cheque Status «{0}»."
					).format(CHEQUE_STATUS_RETURNED_FROM_PAYEE),
					title=frappe._("Returned workflow state"),
				)

	def _validate_replacement_links_when_replaced(self):
		"""Replacement chain when ``workflow_state`` is **Replaced**.

		At least one of ``replaces_cheque`` or ``replaced_by`` must be set; both empty is invalid.
		"""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_REPLACED:
			return
		has_replaces = bool((self.replaces_cheque or "").strip())
		has_replaced_by = bool((self.replaced_by or "").strip())
		if has_replaces or has_replaced_by:
			return
		frappe.throw(
			frappe._("When Workflow State is Replaced, set at least one of Replaces Cheque or Replaced By."),
			title=frappe._("Missing replacement link"),
		)

	def _sync_cheque_status_from_workflow_state(self):
		"""Set ``cheque_status`` from ``workflow_state`` using ``pdc_workflow_to_cheque_status``.

		Calls :func:`map_workflow_state_to_cheque_status` with ``cheque_direction`` and
		``workflow_state``. Skips when ``cheque_direction`` is not Receivable/Payable.
		"""
		if self.cheque_direction not in ("Receivable", "Payable"):
			return
		mapped = map_workflow_state_to_cheque_status(self.cheque_direction, self.workflow_state)
		if mapped is None:
			frappe.throw(
				frappe._(
					"There is no Cheque Status mapped for Workflow State {0} with {1} cheque. "
					"Set a valid Workflow State for this cheque direction."
				).format(self.workflow_state or "", self.cheque_direction),
				title=frappe._("Cheque Status out of sync"),
			)
		self.cheque_status = mapped

	def _validate_cheque_status_matches_workflow_state(self):
		"""Ensure ``cheque_status`` equals the mapping for current ``workflow_state`` (no manual drift)."""
		if self.cheque_direction not in ("Receivable", "Payable"):
			return
		expected = map_workflow_state_to_cheque_status(self.cheque_direction, self.workflow_state)
		if expected is None:
			frappe.throw(
				frappe._(
					"There is no Cheque Status mapped for Workflow State {0} with {1} cheque. "
					"Set a valid Workflow State for this cheque direction."
				).format(self.workflow_state or "", self.cheque_direction),
				title=frappe._("Cheque Status out of sync"),
			)
		actual = (self.cheque_status or "").strip()
		if actual != expected:
			frappe.throw(
				frappe._(
					"Cheque Status ({0}) does not match Workflow State ({1}) for a {2} cheque. "
					"Expected Cheque Status: {3}."
				).format(
					self.cheque_status or "",
					normalize_workflow_state_value(self.workflow_state),
					self.cheque_direction,
					expected,
				),
				title=frappe._("Cheque Status mismatch"),
			)

	def _validate_clearing_accounting_payload(self) -> None:
		"""If policy requires **Journal Entry** for **→Cleared**, ensure a JE payload can be built."""
		if normalize_workflow_state_value(self.workflow_state) != WORKFLOW_CLEARED:
			return
		if not getattr(self, "cleared_date", None):
			frappe.throw(
				frappe._("Cleared Date is mandatory when Workflow State is Cleared."),
				title=frappe._("Missing Cleared Date"),
			)
		prev_raw = self._get_previous_workflow_state_for_accounting()
		action = get_accounting_action(self, prev_raw)
		if action != PDC_ACCOUNTING_JOURNAL_ENTRY:
			return
		from_n = normalize_workflow_state_value(prev_raw)
		posting_date = getattr(self, "cleared_date", None)
		payload = build_pdc_journal_entry_data(self, from_n, WORKFLOW_CLEARED, posting_date=posting_date)
		if payload:
			return

		msgs: list[str] = []
		if self.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
			if not _strip_link_name_or_none(getattr(self, "bank_account", None)):
				msgs.append(frappe._("Set **Bank Account** on this PDC (required to clear at the bank)."))
			elif not _pdc_bank_gl_account(self):
				msgs.append(
					frappe._("The linked **Bank Account** must have a company **Account** (GL) in ERPNext.")
				)
			if not getattr(self, "cheque_amount", None):
				msgs.append(frappe._("Set **Cheque Amount**."))
			settings = _get_pdc_settings_for_company(getattr(self, "company", None))
			acc = resolve_pdc_accounts_for_journal(self, settings)
			if from_n == WORKFLOW_SENT_TO_BANK and not acc.get("cheques_in_clearing"):
				msgs.append(
					frappe._(
						"Set **Default Cheques in Clearing Account** in **PDC Settings** for this company "
						"(required to clear after **Sent to Bank**)."
					)
				)
			cred = receivable_intermediary_account_for_bank_clear(self, from_n, acc)
			if not cred:
				if from_n == WORKFLOW_REGISTERED:
					msgs.append(
						frappe._(
							"Configure **Cheques in Hand** (PDC Settings or **Account Paid To**) to build the clear Journal Entry."
						)
					)
				elif from_n == WORKFLOW_UNDER_LEGAL_ACTION:
					msgs.append(
						frappe._(
							"For **Under Legal Action** → **Cleared**, configure **Protested** and/or **Cheques in Clearing** "
							"in **PDC Settings**, or **Account Paid To** for cheques in hand."
						)
					)
		elif self.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
			if not _strip_link_name_or_none(getattr(self, "bank_account", None)):
				msgs.append(frappe._("Set **Bank Account** on this PDC."))
			elif not _pdc_bank_gl_account(self):
				msgs.append(
					frappe._("The linked **Bank Account** must have a company **Account** (GL) in ERPNext.")
				)
			settings = _get_pdc_settings_for_company(getattr(self, "company", None))
			acc = resolve_pdc_accounts_for_journal(self, settings)
			if not acc.get("payable_cheque"):
				msgs.append(
					frappe._("Set **Default Payable Cheque Account** in **PDC Settings** for this company.")
				)
			if not getattr(self, "cheque_amount", None):
				msgs.append(frappe._("Set **Cheque Amount**."))
			if not self.party_type or not self.party:
				msgs.append(frappe._("Set **Party Type** and **Party**."))

		detail = "\n".join(f"• {m}" for m in msgs) if msgs else ""
		summary = frappe._(
			"Cannot set Workflow to **Cleared** until a **{0}** can be built for transition {1} → Cleared."
		).format(frappe._("Journal Entry"), from_n)
		body = summary if not detail else f"{summary}\n{detail}"
		frappe.throw(
			body,
			title=frappe._("PDC clearing"),
		)

	def _validate_replaces_cheque(self):
		"""Optional replacement chain links: ``replaces_cheque`` / ``replaced_by`` → Post Dated Cheque."""
		for fieldname, label in (
			("replaces_cheque", frappe._("Replaces Cheque")),
			("replaced_by", frappe._("Replaced By")),
		):
			other = self.get(fieldname)
			if not other:
				continue
			if self.name and other == self.name:
				frappe.throw(
					frappe._("{0} cannot point to this same Post Dated Cheque.").format(label),
					title=frappe._("Invalid replacement link"),
				)
			other_company = frappe.db.get_value("Post Dated Cheque", other, "company")
			if other_company and self.company and other_company != self.company:
				frappe.throw(
					frappe._("{0} must belong to the same Company ({1}).").format(label, self.company),
					title=frappe._("Invalid replacement link"),
				)

	def _validate_replacement_bidirectional_conflicts(self):
		"""Do not overwrite a valid existing counterparty link (B replaces A must match A.replaced_by)."""
		rc = _strip_link_name_or_none(self.replaces_cheque)
		rb = _strip_link_name_or_none(self.replaced_by)
		if rc:
			other_rb = _strip_link_name_or_none(frappe.db.get_value("Post Dated Cheque", rc, "replaced_by"))
			if other_rb and (not self.name or other_rb != self.name):
				frappe.throw(
					frappe._(
						"Post Dated Cheque {0} is already marked as replaced by {1}. "
						"Clear that link first or choose another cheque to replace."
					).format(rc, other_rb),
					title=frappe._("Replacement link conflict"),
				)
		if rb:
			other_rc = _strip_link_name_or_none(
				frappe.db.get_value("Post Dated Cheque", rb, "replaces_cheque")
			)
			if other_rc and (not self.name or other_rc != self.name):
				frappe.throw(
					frappe._(
						"Post Dated Cheque {0} already replaces {1}. "
						"Clear Replaces Cheque on that document first or pick another replacement cheque."
					).format(rb, other_rc),
					title=frappe._("Replacement link conflict"),
				)

	def _validate_replacement_no_cycle(self):
		"""Block self-replacement loops and multi-hop cycles (e.g. A←B←C←A along ``replaces_cheque``)."""
		rc = _strip_link_name_or_none(self.replaces_cheque)
		rb = _strip_link_name_or_none(self.replaced_by)
		if rc:
			self._assert_no_replacement_cycle_along_replaces_chain(rc, self.name)
		if rb and self.name:
			self._assert_no_replacement_cycle_along_replaces_chain(self.name, rb)

	def _assert_no_replacement_cycle_along_replaces_chain(
		self,
		start: str,
		replacer_name: str | None,
		*,
		max_hops: int = 500,
	) -> None:
		"""Follow ``replaces_cheque`` from ``start`` (older chain). Fail if we revisit a node (cycle in
		data) or if ``replacer_name`` appears on that chain (would close a replacement loop).
		"""
		if not start:
			return
		visited: set[str] = set()
		cur = start
		hops = 0
		while cur:
			hops += 1
			if hops > max_hops:
				frappe.throw(
					frappe._("Replacement chain from {0} is too long; check for circular links.").format(
						start
					),
					title=frappe._("Circular replacement"),
				)
			if replacer_name and cur == replacer_name:
				frappe.throw(
					frappe._(
						"This replacement would create a circular chain: the replacer cannot appear in the "
						"replacement history of the cheque being replaced."
					),
					title=frappe._("Circular replacement"),
				)
			if cur in visited:
				frappe.throw(
					frappe._("A circular replacement chain exists involving Post Dated Cheque {0}.").format(
						cur
					),
					title=frappe._("Circular replacement"),
				)
			visited.add(cur)
			nxt = _strip_link_name_or_none(frappe.db.get_value("Post Dated Cheque", cur, "replaces_cheque"))
			if not nxt:
				break
			cur = nxt

	def _sync_replacement_bidirectional_links(self):
		"""Mirror replacement links on the other PDC and clear stale links when fields change.

		If **B** replaces **A** (``B.replaces_cheque = A``), set ``A.replaced_by = B``.
		If **A** is replaced by **B** (``A.replaced_by = B``), set ``B.replaces_cheque = A``.
		Uses ``frappe.db.set_value`` (no recursive full save). Clears the previous counterparty
		when a link is removed or repointed.
		"""
		if not self.name:
			return
		before = self.get_doc_before_save()
		prev_rc = _strip_link_name_or_none(before.get("replaces_cheque")) if before else None
		prev_rb = _strip_link_name_or_none(before.get("replaced_by")) if before else None
		cur_rc = _strip_link_name_or_none(self.replaces_cheque)
		cur_rb = _strip_link_name_or_none(self.replaced_by)

		def _clear_if_points_to_me(other_name: str, field: str) -> None:
			if not other_name or not frappe.db.exists("Post Dated Cheque", other_name):
				return
			current = _strip_link_name_or_none(frappe.db.get_value("Post Dated Cheque", other_name, field))
			if current == self.name:
				frappe.db.set_value("Post Dated Cheque", other_name, field, None)

		# Drop stale back-references when this document repoints or clears a link.
		if prev_rc and prev_rc != cur_rc:
			_clear_if_points_to_me(prev_rc, "replaced_by")
		if prev_rb and prev_rb != cur_rb:
			_clear_if_points_to_me(prev_rb, "replaces_cheque")

		# B.replaces_cheque = A  →  A.replaced_by = B
		if cur_rc and frappe.db.exists("Post Dated Cheque", cur_rc):
			existing = _strip_link_name_or_none(
				frappe.db.get_value("Post Dated Cheque", cur_rc, "replaced_by")
			)
			if existing in (None, self.name):
				frappe.db.set_value("Post Dated Cheque", cur_rc, "replaced_by", self.name)

		# A.replaced_by = B  →  B.replaces_cheque = A
		if cur_rb and frappe.db.exists("Post Dated Cheque", cur_rb):
			existing = _strip_link_name_or_none(
				frappe.db.get_value("Post Dated Cheque", cur_rb, "replaces_cheque")
			)
			if existing in (None, self.name):
				frappe.db.set_value("Post Dated Cheque", cur_rb, "replaces_cheque", self.name)

	def _set_default_party_accounts(self):
		"""Set Account Paid From/To from party default or company default if empty.

		Skipped when updating an **already submitted** document so workflow-only saves do not
		overwrite GL links (`account_paid_from` / `account_paid_to`) after submit. Initial
		**Submit** (docstatus 0 → 1) still runs this because ``_doc_before_save.docstatus`` is 0.
		"""
		if not self.company:
			return
		before = self.get_doc_before_save()
		prev_docstatus = int(getattr(before, "docstatus", 0) or 0) if before is not None else 0
		if self.docstatus == 1 and prev_docstatus == 1:
			return

		prev_direction = None
		if before:
			prev_direction = before.get("cheque_direction")

		# Receivable: Account Paid To = Cheques in Hand from PDC Settings (default / direction switch).
		if self.cheque_direction == "Receivable":
			ch = _get_cheques_in_hand_account_for_company(self.company)
			if ch and (not self.account_paid_to or prev_direction == "Payable"):
				self.account_paid_to = ch

		if not self.party_type or not self.party:
			return

		if self.cheque_direction == "Receivable" and not self.account_paid_from:
			self.account_paid_from = _get_party_account_or_company_default(
				self.party_type, self.party, self.company, "receivable"
			)

		if self.cheque_direction == "Payable":
			# After switching from Receivable, replace Cheques-in-Hand with party payable default.
			if not self.account_paid_to or prev_direction == "Receivable":
				self.account_paid_to = _get_party_account_or_company_default(
					self.party_type, self.party, self.company, "payable"
				)

	def _validate_party(self):
		if not self.party_type or not self.party:
			# Draft UX resilience: sometimes clients fill holder fields first; for Receivable we can safely
			# treat holder as the received-from party when it is a valid receivable party type.
			if (self.docstatus or 0) == 0 and (
				self.cheque_direction or ""
			).strip() == CHEQUE_DIRECTION_RECEIVABLE:
				ht = _strip_link_name_or_none(getattr(self, "holder_party_type", None))
				hp = _strip_link_name_or_none(getattr(self, "holder_party", None))
				if ht in {"Customer", "Employee", "Shareholder"} and hp:
					try:
						if frappe.db.exists(ht, hp):
							self.party_type = ht
							self.party = hp
					except Exception:
						pass
			if self.party_type and self.party:
				return
			frappe.throw(
				frappe._("Party Type and Party are required for {0} cheque.").format(
					self.cheque_direction or ""
				)
			)
		# Optional: party_type vs direction guidance (non-blocking)
		receivable_party_types = {"Customer", "Employee", "Shareholder"}
		payable_party_types = {"Supplier", "Employee", "Shareholder"}

		if self.cheque_direction == "Receivable" and self.party_type not in receivable_party_types:
			frappe.msgprint(
				frappe._("Receivable cheques typically use Party Type: Customer."),
				indicator="orange",
				alert=True,
			)
		if self.cheque_direction == "Payable" and self.party_type not in payable_party_types:
			frappe.msgprint(
				frappe._("Payable cheques typically use Party Type: Supplier."),
				indicator="orange",
				alert=True,
			)

	def _validate_duplicate_cheque_no(self):
		if not self.cheque_no or not self.company:
			return
		filters = {
			"cheque_no": self.cheque_no,
			"company": self.company,
			"name": ["!=", self.name or ""],
		}
		if frappe.db.exists("Post Dated Cheque", filters):
			frappe.throw(
				frappe._("Cheque Number {0} already exists for company {1}.").format(
					self.cheque_no, self.company
				)
			)

	def _validate_cheque_leaf_integration(self) -> None:
		"""Phase 3: optional Cheque Leaf integration for Payable PDCs only."""
		leaf = (getattr(self, "cheque_leaf", None) or "").strip()
		if self.cheque_direction != "Payable":
			if leaf:
				# Draft UX: clear inconsistent leaf rather than blocking saves.
				if (self.docstatus or 0) == 0:
					self.cheque_leaf = ""
					return
				frappe.throw(
					frappe._("Cheque Leaf can only be set for Payable (issued) cheques."),
					title=frappe._("Cheque Leaf"),
				)
			return

		# Payable: leaf is optional; manual cheque_no continues to be supported.
		if not leaf:
			return

		row = _pdc_get_cheque_leaf_row_for_update(leaf)
		if not row:
			frappe.throw(
				frappe._("Cheque Leaf {0} does not exist.").format(leaf), title=frappe._("Cheque Leaf")
			)

		if row.company != self.company or row.bank_account != self.bank_account:
			frappe.throw(
				frappe._(
					"Cheque Leaf must belong to the same Company and Bank Account as this Post Dated Cheque."
				),
				title=frappe._("Cheque Leaf"),
			)

		_pdc_assert_cheque_leaf_usable_by_pdc(row, self.name or "")

		# Enforce cheque_no match.
		if (row.cheque_number or "").strip() and (self.cheque_no or "").strip() != (
			row.cheque_number or ""
		).strip():
			frappe.throw(
				frappe._("Cheque Number must match the selected Cheque Leaf."),
				title=frappe._("Cheque Leaf"),
			)

	def _apply_cheque_leaf_reservation_draft(self) -> None:
		"""Draft save: reserve/release leaf as the user edits cheque_leaf (Payable only)."""
		# Only for draft saves (not update_after_submit).
		if (self.docstatus or 0) != 0:
			return

		if self.cheque_direction != "Payable":
			return

		before = None if self.is_new() else self.get_doc_before_save()
		prev_leaf = ((getattr(before, "cheque_leaf", None) if before else None) or "").strip()
		cur_leaf = (getattr(self, "cheque_leaf", None) or "").strip()

		# Release old leaf (if any) when changed/cleared.
		if prev_leaf and prev_leaf != cur_leaf and self.name:
			_pdc_release_leaf_if_reserved_by_pdc(prev_leaf, self.name)

		# Reserve current leaf.
		if cur_leaf and self.name:
			_pdc_reserve_leaf_for_pdc(cur_leaf, self)

	def _apply_cheque_leaf_on_submit(self) -> None:
		"""Submit: mark selected leaf as Used for this PDC (Payable only)."""
		leaf = (getattr(self, "cheque_leaf", None) or "").strip()
		if self.cheque_direction != "Payable" or not leaf:
			return
		_pdc_mark_leaf_used_for_pdc(leaf, self)

	def _apply_cheque_leaf_on_cancel(self) -> None:
		"""Cancel: Reserved -> Available; Used by this PDC -> Void (Payable only)."""
		leaf = (getattr(self, "cheque_leaf", None) or "").strip()
		if self.cheque_direction != "Payable" or not leaf or not self.name:
			return
		_pdc_cancel_leaf_for_pdc(leaf, self)

	def _validate_party_immutable_after_submit(self):
		"""Party (Received From / Paid To) must not change after submit."""
		if self.docstatus != 1:
			return
		before = self.get_doc_before_save()
		if not before:
			return
		if (before.party_type != self.party_type) or (before.party != self.party):
			frappe.throw(
				frappe._(
					"Cannot change Party Type or Party after submit. Cancel the document to make changes."
				)
			)

	def get_pdc_settings(self):
		"""Get PDC Settings for the company (by name or by company field)."""
		if not self.company:
			frappe.throw(frappe._("Company is required"))
		name = frappe.db.get_value("PDC Settings", {"company": self.company}, "name")
		if not name:
			name = self.company
		if not name or not frappe.db.exists("PDC Settings", name):
			frappe.throw(
				frappe._("PDC Settings not found for company {0}. Please create PDC Settings first.").format(
					self.company
				)
			)
		return frappe.get_doc("PDC Settings", name)

	def _has_register_entry(self):
		"""Check if Register JE already exists (Receive for receivable, Payable Issue for payable)."""
		for ref in self.journal_references or []:
			if ref.purpose in ("Receive", "Payable Issue"):
				return True
		if self.name:
			count = frappe.db.count(
				"PDC Journal Reference",
				{
					"parent": self.name,
					"parenttype": "Post Dated Cheque",
					"purpose": ["in", ["Receive", "Payable Issue"]],
				},
			)
			if count and count > 0:
				return True
		return False

	def _create_register_cheque_je(self, posting_date=None):
		"""
		Create Journal Entry when transitioning to Registered.
		Receivable: Dr Cheques in Hand, Cr Account Paid From (party receivable).
		Payable: Dr Account Paid To (party payable), Cr Cheques Payable.
		"""
		if self._has_register_entry():
			return None
		settings = self.get_pdc_settings()
		posting_date = (
			posting_date
			or (getattr(self, "received_date", None) if self.cheque_direction == "Receivable" else None)
			or getdate()
		)

		if self.cheque_direction == "Receivable":
			if not settings.get("default_cheques_in_hand_account"):
				frappe.throw(
					frappe._("Cheques in Hand Account is not set in PDC Settings for company {0}.").format(
						self.company
					)
				)
			if not self.account_paid_from:
				frappe.throw(
					frappe._(
						"Account Paid From is required for Receivable cheque. Set it or select Party first."
					)
				)
			je = frappe.new_doc("Journal Entry")
			je.posting_date = posting_date
			je.company = self.company
			je.voucher_type = "Journal Entry"
			je.cheque_no = self.cheque_no
			je.cheque_date = self.cheque_due_date
			je.user_remark = render_pdc_je_text(
				getattr(settings, "je_user_remark_register_receivable_template", None) if settings else None,
				fallback_text=frappe._("Cheque {0} received from party - PDC Register").format(
					self.cheque_no
				),
				context=PDCDescriptionContext.from_doc(self),
				append_cheque_no_suffix=False,
			)
			je.append(
				"accounts",
				{
					"account": settings.default_cheques_in_hand_account,
					"debit_in_account_currency": self.cheque_amount,
					"party_type": self.party_type,
					"party": self.party,
				},
			)
			je.append(
				"accounts",
				{
					"account": self.account_paid_from,
					"credit_in_account_currency": self.cheque_amount,
					"party_type": self.party_type,
					"party": self.party,
				},
			)
			je.flags.ignore_permissions = True
			je.save()
			je.submit()
			self.append(
				"journal_references",
				{
					"journal_entry": je.name,
					"purpose": "Receive",
					"posting_date": posting_date,
					"amount": self.cheque_amount,
				},
			)
			return je

		if self.cheque_direction == "Payable":
			if not settings.get("default_payable_cheque_account"):
				frappe.throw(
					frappe._(
						"Default Payable Cheque Account is not set in PDC Settings for company {0}."
					).format(self.company)
				)
			if not self.account_paid_to:
				frappe.throw(
					frappe._("Account Paid To is required for Payable cheque. Set it or select Party first.")
				)
			je = frappe.new_doc("Journal Entry")
			je.posting_date = posting_date
			je.company = self.company
			je.voucher_type = "Journal Entry"
			je.cheque_no = self.cheque_no
			je.cheque_date = self.cheque_due_date
			je.user_remark = render_pdc_je_text(
				getattr(settings, "je_user_remark_register_payable_template", None) if settings else None,
				fallback_text=frappe._("Cheque {0} issued to party - PDC Register").format(self.cheque_no),
				context=PDCDescriptionContext.from_doc(self),
				append_cheque_no_suffix=False,
			)
			je.append(
				"accounts",
				{
					"account": self.account_paid_to,
					"debit_in_account_currency": self.cheque_amount,
					"party_type": self.party_type,
					"party": self.party,
				},
			)
			je.append(
				"accounts",
				{
					# Payable pool account: explicit override on PDC must take priority over settings default.
					"account": resolve_pdc_accounts_for_journal(self, settings).get("payable_cheque"),
					"credit_in_account_currency": self.cheque_amount,
					"party_type": self.party_type,
					"party": self.party,
				},
			)
			je.flags.ignore_permissions = True
			je.save()
			je.submit()
			self.append(
				"journal_references",
				{
					"journal_entry": je.name,
					"purpose": "Payable Issue",
					"posting_date": posting_date,
					"amount": self.cheque_amount,
				},
			)
			return je

		return None


def _pdc_assert_cheque_leaf_usable_by_pdc(row, pdc_name: str) -> None:
	"""Ensure a Payable PDC may reference this Cheque Leaf (status + ownership).

	Allowed:
	* Available
	* Reserved with ``reserved_by_pdc`` = this PDC
	* Used with ``linked_post_dated_cheque`` = this PDC

	Rejected: Void leaf, or Reserved/Used by another PDC.
	"""
	status = (getattr(row, "status", None) or "").strip()
	pdc_name = (pdc_name or "").strip()

	if status == "Void":
		frappe.throw(
			frappe._("Cheque Leaf {0} is void/spoiled and cannot be used.").format(
				getattr(row, "name", None) or getattr(row, "cheque_number", "") or ""
			),
			title=frappe._("Cheque Leaf"),
		)
	if status == "Used for Guarantee" or (getattr(row, "linked_guarantee_document", None) or "").strip():
		frappe.throw(
			frappe._("This Cheque Leaf is allocated to a Guarantee Document and cannot be used for a Post Dated Cheque."),
			title=frappe._("Cheque Leaf"),
		)
	if status == "Available":
		return
	if status == "Reserved":
		if (getattr(row, "reserved_by_pdc", None) or "").strip() == pdc_name:
			return
		frappe.throw(
			frappe._("This Cheque Leaf is already reserved by another Post Dated Cheque."),
			title=frappe._("Cheque Leaf"),
		)
	if status == "Used":
		if (getattr(row, "linked_post_dated_cheque", None) or "").strip() == pdc_name:
			return
		frappe.throw(
			frappe._("This Cheque Leaf is already used by another Post Dated Cheque."),
			title=frappe._("Cheque Leaf"),
		)
	frappe.throw(
		frappe._("Cheque Leaf must be Available, Reserved by this PDC, or Used by this PDC."),
		title=frappe._("Cheque Leaf"),
	)


def _pdc_get_cheque_leaf_row_for_update(leaf_name: str):
	"""Fetch Cheque Leaf row and lock it for the current transaction."""
	if not leaf_name:
		return None
	sql = """
		select
			name, company, bank_account, cheque_number, status,
			reserved_by_pdc, reserved_on,
			linked_post_dated_cheque, used_on, voided_on,
			linked_guarantee_document, guarantee_allocated_on,
			guarantee_allocated_by, guarantee_released_on
		from `tabCheque Leaf`
		where name = %s
		for update
		"""
	try:
		rows = frappe.db.sql(sql, (leaf_name,), as_dict=True)
	except Exception:
		rows = frappe.db.sql(
			"""
			select
				name, company, bank_account, cheque_number, status,
				reserved_by_pdc, reserved_on,
				linked_post_dated_cheque, used_on, voided_on
			from `tabCheque Leaf`
			where name = %s
			for update
			""",
			(leaf_name,),
			as_dict=True,
		)
		for row in rows or []:
			row["linked_guarantee_document"] = None
			row["guarantee_allocated_on"] = None
			row["guarantee_allocated_by"] = None
			row["guarantee_released_on"] = None
	return rows[0] if rows else None


def _pdc_reserve_leaf_for_pdc(leaf_name: str, pdc: "PostDatedCheque") -> None:
	row = _pdc_get_cheque_leaf_row_for_update(leaf_name)
	if not row:
		frappe.throw(
			frappe._("Cheque Leaf {0} does not exist.").format(leaf_name), title=frappe._("Cheque Leaf")
		)

	if row.company != pdc.company or row.bank_account != pdc.bank_account:
		frappe.throw(
			frappe._(
				"Cheque Leaf must belong to the same Company and Bank Account as this Post Dated Cheque."
			),
			title=frappe._("Cheque Leaf"),
		)
	if row.status == "Reserved" and (row.reserved_by_pdc or "") != (pdc.name or ""):
		frappe.throw(
			frappe._("This Cheque Leaf is already reserved by another Post Dated Cheque."),
			title=frappe._("Cheque Leaf"),
		)
	if row.status in ("Used", "Void", "Used for Guarantee") or (
		getattr(row, "linked_guarantee_document", None) or ""
	).strip():
		frappe.throw(
			frappe._("Cannot reserve a {0} cheque leaf.").format(row.status), title=frappe._("Cheque Leaf")
		)
	if row.status == "Reserved" and (row.reserved_by_pdc or "") == (pdc.name or ""):
		return

	from frappe.utils import now_datetime

	frappe.db.set_value(
		"Cheque Leaf",
		leaf_name,
		{"status": "Reserved", "reserved_by_pdc": pdc.name, "reserved_on": now_datetime()},
		update_modified=False,
	)


def _pdc_release_leaf_if_reserved_by_pdc(leaf_name: str, pdc_name: str) -> None:
	row = _pdc_get_cheque_leaf_row_for_update(leaf_name)
	if not row or row.status != "Reserved":
		return
	if (row.reserved_by_pdc or "") != (pdc_name or ""):
		return
	if row.linked_post_dated_cheque or "":
		return
	if (getattr(row, "linked_guarantee_document", None) or "").strip():
		return

	frappe.db.set_value(
		"Cheque Leaf",
		leaf_name,
		{"status": "Available", "reserved_by_pdc": None, "reserved_on": None},
		update_modified=False,
	)


def _pdc_leaf_ownership_conflict_message(pdc, leaf_name: str, row) -> str:
	return frappe._(
		"Cannot delete Post Dated Cheque {0}: Cheque Leaf {1} cannot be safely released "
		"(status={2}, reserved_by_pdc={3}, linked_post_dated_cheque={4}, "
		"linked_guarantee_document={5})."
	).format(
		pdc.name,
		leaf_name,
		(getattr(row, "status", None) or "") if row else "",
		(getattr(row, "reserved_by_pdc", None) or "") if row else "",
		(getattr(row, "linked_post_dated_cheque", None) or "") if row else "",
		(getattr(row, "linked_guarantee_document", None) or "") if row else "",
	)


def _pdc_assert_and_release_leaf_on_draft_trash(leaf_name: str, pdc: "PostDatedCheque") -> None:
	"""Fail-closed release of a Draft PDC's owned temporary leaf reservation on trash.

	Releases only when the leaf is Reserved by this PDC with no operational/Guarantee
	ownership. Available leaves with no conflicting owner are allowed (normalize stale
	self-reservation fields). Any other state blocks deletion.
	"""
	row = _pdc_get_cheque_leaf_row_for_update(leaf_name)
	if not row:
		frappe.throw(
			frappe._("Cannot delete Post Dated Cheque {0}: Cheque Leaf {1} does not exist.").format(
				pdc.name, leaf_name
			),
			title=frappe._("Cheque Leaf"),
		)

	status = (row.status or "").strip()
	reserved_by = (row.reserved_by_pdc or "").strip()
	linked_pdc = (row.linked_post_dated_cheque or "").strip()
	linked_gd = (getattr(row, "linked_guarantee_document", None) or "").strip()

	if linked_gd or status == "Used for Guarantee":
		frappe.throw(_pdc_leaf_ownership_conflict_message(pdc, leaf_name, row), title=frappe._("Cheque Leaf"))
	if linked_pdc:
		frappe.throw(_pdc_leaf_ownership_conflict_message(pdc, leaf_name, row), title=frappe._("Cheque Leaf"))
	if status in ("Used", "Void", "Used for Guarantee"):
		frappe.throw(_pdc_leaf_ownership_conflict_message(pdc, leaf_name, row), title=frappe._("Cheque Leaf"))

	if status == "Reserved":
		if reserved_by != (pdc.name or ""):
			frappe.throw(_pdc_leaf_ownership_conflict_message(pdc, leaf_name, row), title=frappe._("Cheque Leaf"))
		_pdc_release_leaf_if_reserved_by_pdc(leaf_name, pdc.name)
		after = frappe.db.get_value(
			"Cheque Leaf",
			leaf_name,
			["status", "reserved_by_pdc", "reserved_on", "linked_post_dated_cheque", "used_on"],
			as_dict=True,
		)
		if not after or (after.status or "").strip() != "Available" or (after.reserved_by_pdc or "").strip():
			frappe.throw(
				_pdc_leaf_ownership_conflict_message(pdc, leaf_name, after or row),
				title=frappe._("Cheque Leaf"),
			)
		return

	if status == "Available":
		if reserved_by and reserved_by != (pdc.name or ""):
			frappe.throw(_pdc_leaf_ownership_conflict_message(pdc, leaf_name, row), title=frappe._("Cheque Leaf"))
		if reserved_by == (pdc.name or "") or row.reserved_on or row.used_on:
			frappe.db.set_value(
				"Cheque Leaf",
				leaf_name,
				{
					"status": "Available",
					"reserved_by_pdc": None,
					"reserved_on": None,
					"used_on": None,
				},
				update_modified=False,
			)
		return

	frappe.throw(_pdc_leaf_ownership_conflict_message(pdc, leaf_name, row), title=frappe._("Cheque Leaf"))


def _pdc_mark_leaf_used_for_pdc(leaf_name: str, pdc: "PostDatedCheque") -> None:
	row = _pdc_get_cheque_leaf_row_for_update(leaf_name)
	if not row:
		frappe.throw(
			frappe._("Cheque Leaf {0} does not exist.").format(leaf_name), title=frappe._("Cheque Leaf")
		)
	if row.status == "Used" and (row.linked_post_dated_cheque or "") == (pdc.name or ""):
		return
	if row.company != pdc.company or row.bank_account != pdc.bank_account:
		frappe.throw(
			frappe._(
				"Cheque Leaf must belong to the same Company and Bank Account as this Post Dated Cheque."
			),
			title=frappe._("Cheque Leaf"),
		)
	if row.status == "Reserved" and (row.reserved_by_pdc or "") not in ("", pdc.name or ""):
		frappe.throw(
			frappe._("This Cheque Leaf is reserved by another Post Dated Cheque."),
			title=frappe._("Cheque Leaf"),
		)
	if row.status not in ("Available", "Reserved"):
		frappe.throw(
			frappe._("Cheque Leaf must be Available or Reserved to be used."), title=frappe._("Cheque Leaf")
		)
	if (getattr(row, "linked_guarantee_document", None) or "").strip():
		frappe.throw(
			frappe._("This Cheque Leaf is allocated to a Guarantee Document and cannot be used for a Post Dated Cheque."),
			title=frappe._("Cheque Leaf"),
		)

	from frappe.utils import now_datetime

	frappe.db.set_value(
		"Cheque Leaf",
		leaf_name,
		{
			"status": "Used",
			"linked_post_dated_cheque": pdc.name,
			"used_on": now_datetime(),
			"reserved_by_pdc": None,
			"reserved_on": None,
		},
		update_modified=False,
	)


def _pdc_cancel_leaf_for_pdc(leaf_name: str, pdc: "PostDatedCheque") -> None:
	row = _pdc_get_cheque_leaf_row_for_update(leaf_name)
	if not row:
		return
	from frappe.utils import now_datetime

	if (
		row.status == "Reserved"
		and (row.reserved_by_pdc or "") == (pdc.name or "")
		and not (row.linked_post_dated_cheque or "")
	):
		frappe.db.set_value(
			"Cheque Leaf",
			leaf_name,
			{"status": "Available", "reserved_by_pdc": None, "reserved_on": None},
			update_modified=False,
		)
		return
	if row.status == "Used" and (row.linked_post_dated_cheque or "") == (pdc.name or ""):
		frappe.flags.skip_cheque_leaf_void_field_guard = True
		frappe.db.set_value(
			"Cheque Leaf",
			leaf_name,
			{
				"status": "Void",
				"voided_on": now_datetime(),
				"void_reason": frappe._("Voided because Post Dated Cheque {0} was cancelled.").format(
					pdc.name
				),
				"voided_by": frappe.session.user,
			},
			update_modified=False,
		)
		frappe.flags.skip_cheque_leaf_void_field_guard = False
		return


def on_pdc_update_after_submit(doc, method=None):
	"""Legacy ``doc_events`` hook; logic lives on :meth:`PostDatedCheque.on_update_after_submit`."""
	return
