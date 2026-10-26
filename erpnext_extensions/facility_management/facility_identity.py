# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Facility loan-number identity: normalization and uniqueness guards.

Uniqueness scope: (company, bank, normalized facility_name).

Historical Production duplicates remain readable. Validation blocks:
- inserting a new Facility with a conflicting key
- changing company/bank/facility_name onto an existing conflict

Database UNIQUE index is deferred until Finance resolves historical duplicates
(see RELEASE notes). Concurrency uses a process file lock on the identity key.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt
from frappe.utils.synchronization import filelock

# Persian / Arabic-Indic digits → ASCII
_DIGIT_MAP = str.maketrans(
	{
		"۰": "0",
		"۱": "1",
		"۲": "2",
		"۳": "3",
		"۴": "4",
		"۵": "5",
		"۶": "6",
		"۷": "7",
		"۸": "8",
		"۹": "9",
		"٠": "0",
		"١": "1",
		"٢": "2",
		"٣": "3",
		"٤": "4",
		"٥": "5",
		"٦": "6",
		"٧": "7",
		"٨": "8",
		"٩": "9",
	}
)

# Visually equivalent separators → hyphen (conservative)
_SEP_RE = re.compile(r"[\u2010-\u2015\u2212\uFE58\uFE63\uFF0D_/\\|]+")
_WS_RE = re.compile(r"\s+")
# Longest digit/separator run (loan number embedded in descriptive facility_name)
_LOAN_TOKEN_RE = re.compile(r"[0-9][0-9.\-_/\\]*[0-9]|[0-9]{4,}")


def normalize_facility_number(raw: str | None) -> str:
	"""Deterministic comparison key for bank facility / loan identifiers in facility_name.

	When facility_name contains descriptive Persian/English text plus a loan number,
	the longest digit/separator token is used as the identity key so that
	``تسهیلات … 000004835-404-50-1001`` and ``000004835-404-50-1001`` collide.
	"""
	if raw is None:
		return ""
	s = unicodedata.normalize("NFKC", str(raw))
	s = s.translate(_DIGIT_MAP)
	s = s.strip()
	s = _WS_RE.sub(" ", s)
	s = _SEP_RE.sub("-", s)
	s = re.sub(r"\s*-\s*", "-", s)
	s = re.sub(r"-{2,}", "-", s)
	s = s.strip("- ")

	tokens = _LOAN_TOKEN_RE.findall(s)
	if tokens:
		# Prefer the longest token with the most digits
		def _token_rank(t: str) -> tuple[int, int]:
			digits = sum(1 for ch in t if ch.isdigit())
			return (digits, len(t))

		best = max(tokens, key=_token_rank)
		best = re.sub(r"-{2,}", "-", best.strip("-."))
		if sum(1 for ch in best if ch.isdigit()) >= 4:
			return best
	return s


def facility_identity_key(company: str | None, bank: str | None, facility_name: str | None) -> str:
	"""Composite uniqueness key. Empty components yield empty key (no uniqueness)."""
	company = (company or "").strip()
	bank = (bank or "").strip()
	number = normalize_facility_number(facility_name)
	if not company or not bank or not number:
		return ""
	return f"{company}\x1f{bank}\x1f{number}"


def _lock_name(key: str) -> str:
	# Keep lock filename short and filesystem-safe
	import hashlib

	digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
	return f"facility_identity_{digest}"


def find_conflicting_facility(
	*,
	company: str,
	bank: str,
	facility_name: str,
	exclude_name: str | None = None,
) -> dict[str, Any] | None:
	"""Return first conflicting Facility (any status), or None."""
	key = facility_identity_key(company, bank, facility_name)
	if not key:
		return None
	target_norm = normalize_facility_number(facility_name)
	rows = frappe.get_all(
		"Facility",
		filters={"company": company, "bank": bank},
		fields=["name", "facility_name", "status", "company", "bank"],
		limit=500,
	)
	for row in rows:
		if exclude_name and row.name == exclude_name:
			continue
		if normalize_facility_number(row.facility_name) == target_norm:
			return row
	return None


def assert_facility_number_unique(doc) -> None:
	"""Throw if another Facility shares company+bank+normalized facility_name."""
	company = (doc.get("company") or "").strip()
	bank = (doc.get("bank") or "").strip()
	facility_name = doc.get("facility_name")
	if not company or not bank or not (facility_name or "").strip():
		return

	key = facility_identity_key(company, bank, facility_name)
	if not key:
		return

	exclude = None if doc.is_new() else doc.name
	with filelock(_lock_name(key), timeout=30):
		conflict = find_conflicting_facility(
			company=company,
			bank=bank,
			facility_name=facility_name,
			exclude_name=exclude,
		)
		if not conflict:
			return
		frappe.throw(
			_(
				"Duplicate Facility Number\n\n"
				"Facility number {0}\n"
				"already exists for this Company and Bank.\n\n"
				"Existing Facility: {1}\n\n"
				"Creating another Facility with the same number is not permitted."
			).format(normalize_facility_number(facility_name), conflict.name),
			title=_("Duplicate Facility Number"),
		)


def receipt_je_owners(je_name: str) -> list[str]:
	if not je_name:
		return []
	return frappe.get_all(
		"Facility",
		filters={"receipt_journal_entry": je_name},
		pluck="name",
		order_by="name asc",
	)


def assert_exclusive_receipt_je(doc) -> None:
	"""Ensure receipt_journal_entry is not owned by another Facility.

	Shared JE links are always rejected.

	cheque_no / company contract checks apply when the receipt pointer is newly set
	or changed. Unrelated updates to historical Facilities that already hold a
	legacy link are not blocked solely for empty/mismatched cheque_no (Finance
	cleanup); new assignments must use assign_receipt_journal_entry.
	"""
	je_name = (doc.get("receipt_journal_entry") or "").strip()
	if not je_name:
		return

	if not frappe.db.exists("Journal Entry", je_name):
		frappe.throw(_("Receipt Journal Entry {0} does not exist.").format(je_name))

	je = frappe.db.get_value(
		"Journal Entry",
		je_name,
		["name", "docstatus", "company", "cheque_no", "voucher_type"],
		as_dict=True,
	)
	if not je:
		frappe.throw(_("Receipt Journal Entry {0} does not exist.").format(je_name))

	# Detect whether receipt_journal_entry is newly assigned / changed
	prior = None
	if doc.name and not doc.is_new():
		prior = (frappe.db.get_value("Facility", doc.name, "receipt_journal_entry") or "").strip() or None
	receipt_changed = doc.is_new() or prior != je_name

	# Exclusive ownership on new/changed assignment. Historical shared links remain
	# readable and editable for unrelated fields; scanner + SAFE_CLEAR remediate them.
	owners = [n for n in receipt_je_owners(je_name) if not doc.name or n != doc.name]
	if owners and receipt_changed:
		frappe.throw(
			_(
				"Receipt Journal Entry {0} is already linked to Facility {1}. "
				"A receipt Journal Entry may belong to only one Facility."
			).format(je_name, ", ".join(owners)),
			title=_("Shared Receipt Journal Entry"),
		)

	if not receipt_changed:
		return

	if doc.company and je.company and je.company != doc.company:
		frappe.throw(
			_("Receipt Journal Entry {0} belongs to company {1}, not {2}.").format(
				je_name, je.company, doc.company
			)
		)

	if doc.name and not doc.is_new():
		cheque = (je.cheque_no or "").strip()
		if not cheque:
			frappe.throw(
				_(
					"Receipt Journal Entry {0} has empty cheque_no and cannot be linked "
					"to Facility {1}."
				).format(je_name, doc.name),
				title=_("Invalid Receipt Journal Entry"),
			)
		if cheque != doc.name:
			frappe.throw(
				_(
					"Receipt Journal Entry {0} cheque_no is {1}, expected {2} "
					"(Facility document name)."
				).format(je_name, cheque, doc.name),
				title=_("Invalid Receipt Journal Entry"),
			)


def clear_inherited_receipt_state(doc) -> None:
	"""Strip receipt ownership / derived receipt state that must never be copied."""
	doc.receipt_journal_entry = None
	doc.received_amount = 0
	# Copied Active facilities would fail status rules without a receipt JE
	if (doc.get("status") or "") in ("Active", "Closed"):
		doc.status = "Draft"


def assign_receipt_journal_entry(
	facility_name: str,
	je_name: str,
	*,
	received_amount: float | None = None,
	status: str | None = None,
) -> None:
	"""Concurrency-safe exclusive assignment of a receipt JE to one Facility."""
	if not facility_name or not je_name:
		frappe.throw(_("Facility and Journal Entry are required for receipt assignment."))

	lock = f"facility_receipt_je_{je_name}"
	with filelock(lock, timeout=30):
		owners = [n for n in receipt_je_owners(je_name) if n != facility_name]
		if owners:
			frappe.throw(
				_(
					"Receipt Journal Entry {0} is already linked to Facility {1}. "
					"A receipt Journal Entry may belong to only one Facility."
				).format(je_name, ", ".join(owners)),
				title=_("Shared Receipt Journal Entry"),
			)

		je = frappe.db.get_value(
			"Journal Entry",
			je_name,
			["name", "docstatus", "company", "cheque_no"],
			as_dict=True,
		)
		if not je:
			frappe.throw(_("Journal Entry {0} does not exist.").format(je_name))
		if cint(je.docstatus) != 1:
			frappe.throw(_("Receipt Journal Entry {0} must be submitted.").format(je_name))

		fac = frappe.db.get_value(
			"Facility",
			facility_name,
			["name", "company", "principal_amount"],
			as_dict=True,
		)
		if not fac:
			frappe.throw(_("Facility {0} does not exist.").format(facility_name))
		if je.company and fac.company and je.company != fac.company:
			frappe.throw(
				_("Receipt Journal Entry {0} company {1} does not match Facility company {2}.").format(
					je_name, je.company, fac.company
				)
			)
		cheque = (je.cheque_no or "").strip()
		if cheque != facility_name:
			frappe.throw(
				_(
					"Receipt Journal Entry {0} cheque_no is {1}, expected {2}."
				).format(je_name, cheque or "(empty)", facility_name),
				title=_("Invalid Receipt Journal Entry"),
			)

		updates: dict[str, Any] = {"receipt_journal_entry": je_name}
		if received_amount is not None:
			updates["received_amount"] = flt(received_amount)
		if status is not None:
			updates["status"] = status
		frappe.db.set_value("Facility", facility_name, updates, update_modified=True)
