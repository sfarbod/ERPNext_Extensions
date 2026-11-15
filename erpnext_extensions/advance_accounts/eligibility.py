# Copyright (c) 2026, ERPNext Extensions contributors
"""Eligible advance-account resolution (classification ∪ configured defaults)."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.advance_accounts.constants import (
	ADVANCE_PAID,
	ADVANCE_RECEIVED,
	CF_ADVANCE_ACCOUNT_TYPE,
	CLASSIFICATION_RULES,
	DIRECTION_PAID,
	DIRECTION_RECEIVED,
)


def direction_for_party_type(party_type: str | None) -> str | None:
	if party_type == "Customer":
		return DIRECTION_RECEIVED
	if party_type == "Supplier":
		return DIRECTION_PAID
	return None


def direction_for_payment_type(payment_type: str | None) -> str | None:
	if payment_type == "Receive":
		return DIRECTION_RECEIVED
	if payment_type == "Pay":
		return DIRECTION_PAID
	return None


def advance_type_for_direction(direction: str | None) -> str | None:
	if direction == DIRECTION_RECEIVED:
		return ADVANCE_RECEIVED
	if direction == DIRECTION_PAID:
		return ADVANCE_PAID
	return None


def company_default_field_for_direction(direction: str | None) -> str | None:
	if direction == DIRECTION_RECEIVED:
		return "default_advance_received_account"
	if direction == DIRECTION_PAID:
		return "default_advance_paid_account"
	return None


def _has_advance_account_type_column() -> bool:
	return bool(frappe.db.has_column("Account", CF_ADVANCE_ACCOUNT_TYPE))


def get_account_advance_type(account: str | None) -> str:
	if not account or not _has_advance_account_type_column():
		return ""
	return (frappe.get_cached_value("Account", account, CF_ADVANCE_ACCOUNT_TYPE) or "").strip()


def get_account_row(account: str | None) -> dict | None:
	if not account:
		return None
	fields = ["name", "company", "root_type", "account_type", "is_group", "disabled", "account_currency"]
	if _has_advance_account_type_column():
		fields.append(CF_ADVANCE_ACCOUNT_TYPE)
	row = frappe.db.get_value("Account", account, fields, as_dict=True)
	return row


def account_matches_classification_rules(row: dict, advance_type: str) -> bool:
	rules = CLASSIFICATION_RULES.get(advance_type)
	if not rules or not row:
		return False
	if cint(row.get("is_group")) or cint(row.get("disabled")):
		return False
	return (
		(row.get("root_type") or "").strip() == rules["root_type"]
		and (row.get("account_type") or "").strip() == rules["account_type"]
	)


def is_classified_advance_account(account: str | None, direction: str | None = None) -> bool:
	"""True when Account.advance_account_type matches direction (or any advance type)."""
	row = get_account_row(account)
	if not row:
		return False
	adv_type = (row.get(CF_ADVANCE_ACCOUNT_TYPE) or "").strip()
	if not adv_type:
		return False
	if direction:
		expected = advance_type_for_direction(direction)
		if adv_type != expected:
			return False
	return account_matches_classification_rules(row, adv_type)


def is_wrong_direction_advance(account: str | None, direction: str | None) -> bool:
	"""Classified as the opposite advance direction."""
	if not account or not direction:
		return False
	adv_type = get_account_advance_type(account)
	if not adv_type:
		return False
	expected = advance_type_for_direction(direction)
	return bool(expected and adv_type != expected)


def get_configured_default_advance_accounts(
	company: str | None, direction: str | None, party_type: str | None = None, party: str | None = None
) -> list[str]:
	"""Company default + party/group advance (compat when classification empty)."""
	out: list[str] = []
	co = (company or "").strip()
	if not co or not direction:
		return out

	field = company_default_field_for_direction(direction)
	if field:
		val = (frappe.get_cached_value("Company", co, field) or "").strip()
		if val:
			out.append(val)

	pt = (party_type or "").strip()
	p = (party or "").strip()
	if pt in ("Customer", "Supplier") and p:
		from erpnext.accounts.party import get_party_advance_account

		party_adv = (get_party_advance_account(pt, p, co) or "").strip()
		if party_adv and party_adv not in out:
			out.append(party_adv)

	return out


def get_classified_advance_accounts(company: str | None, direction: str | None) -> list[str]:
	co = (company or "").strip()
	adv_type = advance_type_for_direction(direction)
	if not co or not adv_type or not _has_advance_account_type_column():
		return []

	rules = CLASSIFICATION_RULES[adv_type]
	rows = frappe.get_all(
		"Account",
		filters={
			"company": co,
			CF_ADVANCE_ACCOUNT_TYPE: adv_type,
			"is_group": 0,
			"disabled": 0,
			"root_type": rules["root_type"],
			"account_type": rules["account_type"],
		},
		pluck="name",
	)
	return list(rows or [])


def get_eligible_advance_accounts(
	company: str | None,
	direction: str | None = None,
	party_type: str | None = None,
	party: str | None = None,
) -> list[str]:
	"""Union of classified accounts and configured defaults for discovery/compat."""
	if not direction:
		direction = direction_for_party_type(party_type)
	if not direction:
		return []

	seen: set[str] = set()
	ordered: list[str] = []
	for acc in get_classified_advance_accounts(company, direction) + get_configured_default_advance_accounts(
		company, direction, party_type=party_type, party=party
	):
		if acc and acc not in seen:
			seen.add(acc)
			ordered.append(acc)
	return ordered


def is_eligible_advance_account(
	account: str | None,
	company: str | None,
	direction: str | None,
	party_type: str | None = None,
	party: str | None = None,
) -> bool:
	if not account or not company or not direction:
		return False
	row = get_account_row(account)
	if not row:
		return False
	if (row.get("company") or "").strip() != (company or "").strip():
		return False
	if cint(row.get("is_group")) or cint(row.get("disabled")):
		return False

	# Classified correctly
	if is_classified_advance_account(account, direction):
		return True

	# Compat: configured default/party advance even if classification not yet seeded.
	# Some legacy CoAs omit account_type on advance ledgers; require correct root_type at minimum.
	configured = get_configured_default_advance_accounts(company, direction, party_type, party)
	if account in configured:
		expected_type = advance_type_for_direction(direction)
		if not expected_type:
			return False
		if account_matches_classification_rules(row, expected_type):
			return True
		rules = CLASSIFICATION_RULES[expected_type]
		return (row.get("root_type") or "").strip() == rules["root_type"]

	return False


def company_feature_enabled(company: str | None) -> bool:
	co = (company or "").strip()
	if not co:
		return False
	return bool(cint(frappe.get_cached_value("Company", co, "book_advance_payments_in_separate_party_account")))
