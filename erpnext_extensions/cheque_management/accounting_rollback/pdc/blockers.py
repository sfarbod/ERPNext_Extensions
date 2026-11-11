# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Unlinked Journal Entry discovery and classification for PDC workflow rollback blockers.

Opening-import PDCs may have historical pre-baseline JEs (matched by cheque_no) that are
intentionally not linked in ``PDC Journal Reference``. Those must be ignored when outside the
requested undo scope. Ambiguous or conflicting candidates fail closed.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.utils import cint, flt, get_datetime

from erpnext_extensions.cheque_management.pdc_journal_entry_service import _purpose_for_transition
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_DRAFT,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	WORKFLOW_SENT_TO_BANK,
	normalize_workflow_state_value,
)

# Classification outcomes
CLASS_IGNORE = "ignore"
CLASS_BLOCK = "block"

# Accounting purpose families used for undo-scope / baseline matching
FAMILY_ISSUE = "issue"  # Payable Issue / Register
FAMILY_CLEAR = "clear"  # Payable Clear / Collected
FAMILY_RECEIVE = "receive"  # Receivable Receive
FAMILY_UNDER_COLLECTION = "under_collection"
FAMILY_RETURN_BOUNCE = "return_bounce"
FAMILY_CANCEL = "cancel"
FAMILY_OTHER = "other"
FAMILY_AMBIGUOUS = "ambiguous"

_PURPOSE_TO_FAMILY: dict[str, str] = {
	"Payable Issue": FAMILY_ISSUE,
	"Payable Clear": FAMILY_CLEAR,
	"Collected": FAMILY_CLEAR,
	"Receive": FAMILY_RECEIVE,
	"Under Collection": FAMILY_UNDER_COLLECTION,
	"Return from Bank": FAMILY_RETURN_BOUNCE,
	"Returned": FAMILY_RETURN_BOUNCE,
	"Cancel": FAMILY_CANCEL,
	"Endorsement": FAMILY_OTHER,
	"Debt Purchase Assignment": FAMILY_OTHER,
	"Debt Purchase Settlement": FAMILY_OTHER,
	"Return from Debt Purchase": FAMILY_OTHER,
}


def purpose_to_family(purpose: str | None) -> str:
	purpose = (purpose or "").strip()
	return _PURPOSE_TO_FAMILY.get(purpose, FAMILY_OTHER)


def undo_purpose_families(pdc, plan) -> set[str]:
	"""Purpose families for edges actually being undone by ``plan``."""
	direction = (getattr(pdc, "cheque_direction", None) or "").strip()
	families: set[str] = set()
	for step in getattr(plan, "steps", None) or []:
		purpose = (getattr(step, "purpose", None) or "").strip()
		if not purpose:
			purpose = _purpose_for_transition(
				direction, getattr(step, "from_state", None), getattr(step, "to_state", None)
			)
		families.add(purpose_to_family(purpose))
	return {f for f in families if f}


def pre_baseline_purpose_families(direction: str, baseline: str) -> set[str]:
	"""Expected historical accounting families that may exist at/before opening-import baseline."""
	direction = (direction or "").strip()
	baseline = normalize_workflow_state_value(baseline)
	if direction == CHEQUE_DIRECTION_PAYABLE:
		if baseline in (WORKFLOW_ISSUED, WORKFLOW_REGISTERED):
			# Issue/Register JE is the typical pre-/at-baseline book entry for payable imports.
			return {FAMILY_ISSUE}
		if baseline == WORKFLOW_DRAFT:
			return set()
		# Cleared baseline: clear accounting may also exist as opening books — not ignored on
		# Cleared→Issued (that rollback is unavailable). Keep empty for safety.
		return set()
	if direction == CHEQUE_DIRECTION_RECEIVABLE:
		if baseline == WORKFLOW_SENT_TO_BANK:
			return {FAMILY_RECEIVE, FAMILY_UNDER_COLLECTION}
		if baseline == WORKFLOW_REGISTERED:
			return {FAMILY_RECEIVE}
		return set()
	return set()


def _account_type(account: str | None) -> str:
	if not account:
		return ""
	return (frappe.db.get_value("Account", account, "account_type") or "").strip()


def load_journal_entry_evidence(je_name: str) -> dict[str, Any]:
	"""Load JE header + lines for classification (read-only)."""
	je = frappe.db.get_value(
		"Journal Entry",
		je_name,
		["name", "company", "cheque_no", "user_remark", "remark", "posting_date", "creation", "docstatus"],
		as_dict=True,
	)
	if not je:
		return {"name": je_name, "missing": True}
	rows = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": je_name},
		fields=[
			"account",
			"party_type",
			"party",
			"debit",
			"credit",
			"debit_in_account_currency",
			"credit_in_account_currency",
			"reference_type",
			"reference_name",
		],
		order_by="idx asc",
	)
	total_debit = 0.0
	parties: set[str] = set()
	has_bank_credit = False
	has_bank_debit = False
	has_nonbank_credit = False
	has_nonbank_debit = False
	refs_pdc: set[str] = set()
	for r in rows:
		debit = flt(r.debit) or flt(r.debit_in_account_currency)
		credit = flt(r.credit) or flt(r.credit_in_account_currency)
		total_debit += debit
		if r.party:
			parties.add((r.party or "").strip())
		is_bank = _account_type(r.account) == "Bank"
		if credit and is_bank:
			has_bank_credit = True
		if debit and is_bank:
			has_bank_debit = True
		if credit and not is_bank:
			has_nonbank_credit = True
		if debit and not is_bank:
			has_nonbank_debit = True
		if (r.reference_type or "") == "Post Dated Cheque" and r.reference_name:
			refs_pdc.add(r.reference_name)
	return {
		"name": je.name,
		"missing": False,
		"company": je.company,
		"cheque_no": (je.cheque_no or "").strip(),
		"user_remark": je.user_remark or "",
		"remark": je.remark or "",
		"posting_date": je.posting_date,
		"creation": je.creation,
		"docstatus": je.docstatus,
		"total_debit": total_debit,
		"parties": parties,
		"has_bank_credit": has_bank_credit,
		"has_bank_debit": has_bank_debit,
		"has_nonbank_credit": has_nonbank_credit,
		"has_nonbank_debit": has_nonbank_debit,
		"reference_pdcs": refs_pdc,
		"rows": rows,
	}


def infer_accounting_family(pdc, evidence: dict[str, Any]) -> str:
	"""Infer purpose family from JE account lines (not remarks alone)."""
	if evidence.get("missing"):
		return FAMILY_AMBIGUOUS
	direction = (getattr(pdc, "cheque_direction", None) or "").strip()
	bank_cr = bool(evidence.get("has_bank_credit"))
	bank_dr = bool(evidence.get("has_bank_debit"))
	nonbank_cr = bool(evidence.get("has_nonbank_credit"))
	nonbank_dr = bool(evidence.get("has_nonbank_debit"))
	parties = evidence.get("parties") or set()

	# Clear / collected: settlement against bank
	if bank_cr and nonbank_dr:
		return FAMILY_CLEAR
	if bank_dr and nonbank_cr and direction == CHEQUE_DIRECTION_RECEIVABLE:
		# Unusual; treat as ambiguous rather than ignore
		return FAMILY_AMBIGUOUS

	if direction == CHEQUE_DIRECTION_PAYABLE:
		# Payable Issue: Dr party AP, Cr notes-payable (non-bank)
		if nonbank_dr and nonbank_cr and parties and not bank_cr and not bank_dr:
			return FAMILY_ISSUE
		return FAMILY_AMBIGUOUS

	if direction == CHEQUE_DIRECTION_RECEIVABLE:
		# Receive: typically Dr CIH, Cr party AR (no bank)
		if nonbank_dr and nonbank_cr and not bank_cr and not bank_dr:
			# Under Collection vs Receive both non-bank; remarks may distinguish but
			# either is a valid pre-baseline family for Sent to Bank imports.
			return FAMILY_RECEIVE
		# Under Collection often Dr clearing Cr CIH — still non-bank both sides
		if nonbank_dr and nonbank_cr:
			return FAMILY_UNDER_COLLECTION
		return FAMILY_AMBIGUOUS

	return FAMILY_AMBIGUOUS


def find_related_unlinked_journal_entries(pdc, known_jes: set[str]) -> list[str]:
	"""Discover submitted JEs related to the PDC that are not already known/linked.

	Discovery alone does **not** imply BLOCK — callers must classify each candidate.
	"""
	known = {k for k in (known_jes or set()) if k}
	cheque_no = (getattr(pdc, "cheque_no", None) or "")[:140]
	company = getattr(pdc, "company", None)
	pdc_name = getattr(pdc, "name", None) or ""
	if not company or not pdc_name:
		return []

	params: dict[str, Any] = {
		"company": company,
		"pat": f"%{pdc_name}%",
		"cheque_no": cheque_no or "__no_cheque_no__",
		"pdc_name": pdc_name,
	}
	known_sql = ""
	if known:
		known_sql = "AND je.name NOT IN %(known)s"
		params["known"] = tuple(known)

	rows = frappe.db.sql(
		f"""
		SELECT DISTINCT je.name
		FROM `tabJournal Entry` je
		WHERE je.docstatus = 1
		  AND je.company = %(company)s
		  {known_sql}
		  AND (
			IFNULL(je.user_remark, '') LIKE %(pat)s
			OR IFNULL(je.remark, '') LIKE %(pat)s
			OR ( %(cheque_no)s != '__no_cheque_no__' AND je.cheque_no = %(cheque_no)s )
			OR EXISTS (
				SELECT 1 FROM `tabJournal Entry Account` jea
				WHERE jea.parent = je.name
				  AND jea.reference_type = 'Post Dated Cheque'
				  AND jea.reference_name = %(pdc_name)s
			)
		  )
		ORDER BY je.creation ASC
		LIMIT 50
		""",
		params,
		as_list=True,
	)
	return [r[0] for r in rows if r and r[0]]


def _identity_strength(pdc, evidence: dict[str, Any]) -> tuple[bool, bool, str]:
	"""Return (has_strong_identity, has_supporting_identity, detail)."""
	pdc_name = getattr(pdc, "name", None) or ""
	cheque_no = (getattr(pdc, "cheque_no", None) or "").strip()
	remark_blob = f"{evidence.get('user_remark') or ''} {evidence.get('remark') or ''}"
	strong = False
	detail_parts: list[str] = []

	if cheque_no and (evidence.get("cheque_no") or "") == cheque_no:
		strong = True
		detail_parts.append("cheque_no")
	if pdc_name and pdc_name in remark_blob:
		strong = True
		detail_parts.append("remark_pdc_name")
	if pdc_name in (evidence.get("reference_pdcs") or set()):
		strong = True
		detail_parts.append("account_reference")

	# Supporting: party / amount
	supporting = False
	party = (getattr(pdc, "party", None) or "").strip()
	parties = evidence.get("parties") or set()
	if party and party in parties:
		supporting = True
		detail_parts.append("party")
	amount = flt(getattr(pdc, "cheque_amount", None))
	je_amt = flt(evidence.get("total_debit"))
	if amount and je_amt and abs(amount - je_amt) <= max(0.01, amount * 0.0001):
		supporting = True
		detail_parts.append("amount")

	return strong, supporting, ",".join(detail_parts) or "none"


def _party_amount_ok(pdc, evidence: dict[str, Any]) -> tuple[bool, str]:
	party = (getattr(pdc, "party", None) or "").strip()
	parties = evidence.get("parties") or set()
	if party and parties and party not in parties:
		return False, "party_mismatch"
	amount = flt(getattr(pdc, "cheque_amount", None))
	je_amt = flt(evidence.get("total_debit"))
	if amount and je_amt and abs(amount - je_amt) > max(1.0, amount * 0.001):
		return False, "amount_mismatch"
	return True, "party_amount_ok"


def _temporal_support(pdc, evidence: dict[str, Any]) -> bool:
	"""Supporting evidence: JE document creation is on/before PDC creation.

	Posting date alone is not sufficient (backdated postings are common).
	"""
	pdc_created = getattr(pdc, "creation", None)
	je_created = evidence.get("creation")
	if not pdc_created or not je_created:
		return False
	try:
		return get_datetime(je_created) <= get_datetime(pdc_created)
	except Exception:
		return False


def _created_after_pdc(pdc, evidence: dict[str, Any]) -> bool:
	pdc_created = getattr(pdc, "creation", None)
	je_created = evidence.get("creation")
	if not pdc_created or not je_created:
		return False
	try:
		return get_datetime(je_created) > get_datetime(pdc_created)
	except Exception:
		return False


def classify_unlinked_journal_entry(
	pdc,
	plan,
	je_name: str,
	*,
	evidence: dict[str, Any] | None = None,
) -> tuple[str, str]:
	"""Classify an unlinked related JE as IGNORE or BLOCK.

	Returns ``(CLASS_IGNORE|CLASS_BLOCK, reason_code)``. Uncertain → BLOCK (fail closed).
	"""
	evidence = evidence if evidence is not None else load_journal_entry_evidence(je_name)
	if evidence.get("missing"):
		return CLASS_BLOCK, "missing_journal_entry"

	is_opening = cint(getattr(pdc, "is_opening_import", 0))
	family = infer_accounting_family(pdc, evidence)
	undo_families = undo_purpose_families(pdc, plan)
	strong, supporting, id_detail = _identity_strength(pdc, evidence)
	party_ok, party_reason = _party_amount_ok(pdc, evidence)

	# Conflict with undo scope always blocks (imported or not)
	if family in undo_families:
		return CLASS_BLOCK, f"undo_scope_conflict:{family}"

	# Clear-like / bounce-like unlinked accounting outside known undo JE is always dangerous
	if family in (FAMILY_CLEAR, FAMILY_RETURN_BOUNCE, FAMILY_CANCEL) and family not in undo_families:
		# Still block: unexpected clear/return not linked
		return CLASS_BLOCK, f"unlinked_{family}_accounting"

	if family == FAMILY_AMBIGUOUS:
		return CLASS_BLOCK, "ambiguous_accounting_shape"

	if not party_ok:
		return CLASS_BLOCK, party_reason

	# Normal PDC: any discovered unlinked related JE blocks
	if not is_opening:
		return CLASS_BLOCK, "normal_pdc_unlinked_je"

	# Opening import — baseline required
	from erpnext_extensions.cheque_management.pdc_opening_import_baseline import (
		_workflow_rank,
		resolve_opening_import_baseline_state,
	)

	baseline = resolve_opening_import_baseline_state(pdc)
	if not baseline:
		return CLASS_BLOCK, "opening_import_baseline_unresolved"

	direction = (getattr(pdc, "cheque_direction", None) or "").strip()
	target = normalize_workflow_state_value(getattr(plan, "target_workflow_state", None))
	current = normalize_workflow_state_value(getattr(plan, "current_workflow_state", None))
	if _workflow_rank(direction, target) < _workflow_rank(direction, baseline):
		return CLASS_BLOCK, "rollback_before_baseline"
	if _workflow_rank(direction, current) < _workflow_rank(direction, baseline):
		return CLASS_BLOCK, "current_before_baseline"

	pre_families = pre_baseline_purpose_families(direction, baseline)
	if family not in pre_families:
		return CLASS_BLOCK, f"not_pre_baseline_family:{family}"

	# IGNORE requires strong identity + supporting evidence (party/amount and/or temporal)
	if not strong:
		return CLASS_BLOCK, f"insufficient_identity:{id_detail}"

	temporal = _temporal_support(pdc, evidence)
	if not supporting and not temporal:
		return CLASS_BLOCK, f"insufficient_supporting_evidence:{id_detail}"

	# Explicit post-import manual that names this PDC must never be ignored
	pdc_name = getattr(pdc, "name", None) or ""
	remark_blob = f"{evidence.get('user_remark') or ''} {evidence.get('remark') or ''}"
	if pdc_name and pdc_name in remark_blob and _created_after_pdc(pdc, evidence):
		return CLASS_BLOCK, "post_import_manual_remark"

	frappe.logger("pdc_workflow_rollback").info(
		"Ignoring pre-baseline unlinked JE %s for PDC %s (family=%s identity=%s)",
		je_name,
		pdc_name,
		family,
		id_detail,
	)
	return CLASS_IGNORE, f"pre_baseline_{family}:{id_detail}"


def validate_unlinked_journal_entry_candidates(pdc, plan, known_jes: set[str]) -> list[dict[str, Any]]:
	"""Classify all related unlinked JEs; raise if any BLOCK. Returns IGNORE diagnostics."""
	candidates = find_related_unlinked_journal_entries(pdc, known_jes)
	ignored: list[dict[str, Any]] = []
	for je_name in candidates:
		verdict, reason = classify_unlinked_journal_entry(pdc, plan, je_name)
		if verdict == CLASS_IGNORE:
			ignored.append({"journal_entry": je_name, "classification": CLASS_IGNORE, "reason": reason})
			continue
		# BLOCK
		frappe.logger("pdc_workflow_rollback").warning(
			"Rollback blocked by unlinked JE %s for PDC %s reason=%s",
			je_name,
			getattr(pdc, "name", None),
			reason,
		)
		raise ValidationError(
			_(
				"Rollback is blocked: Journal Entry {0} could not be treated as safe historical "
				"opening-import accounting (reason: {1}). Cancel or relink conflicting entries "
				"before rollback."
			).format(je_name, reason)
		)
	return ignored
