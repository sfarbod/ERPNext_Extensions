# Copyright (c) 2026, ERPNext Extensions contributors
"""Seed Account.advance_account_type from Company/Party configured advance accounts.

Safe rules:
- Only set classification when currently empty.
- Validate structural Root Type / Account Type before seeding.
- Skip (and log) accounts referenced in conflicting directions.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.advance_accounts.constants import (
	ADVANCE_PAID,
	ADVANCE_RECEIVED,
	CF_ADVANCE_ACCOUNT_TYPE,
	CLASSIFICATION_RULES,
)
from erpnext_extensions.advance_accounts.custom_fields import ensure_custom_fields


def _collect_candidates() -> dict[str, set[str]]:
	"""account -> set of desired advance types from configuration."""
	wanted: dict[str, set[str]] = {}

	def add(account: str | None, adv_type: str) -> None:
		acc = (account or "").strip()
		if not acc:
			return
		wanted.setdefault(acc, set()).add(adv_type)

	for co in frappe.get_all("Company", pluck="name"):
		add(frappe.db.get_value("Company", co, "default_advance_received_account"), ADVANCE_RECEIVED)
		add(frappe.db.get_value("Company", co, "default_advance_paid_account"), ADVANCE_PAID)

	for parenttype, adv_type in (
		("Customer", ADVANCE_RECEIVED),
		("Supplier", ADVANCE_PAID),
		("Customer Group", ADVANCE_RECEIVED),
		("Supplier Group", ADVANCE_PAID),
	):
		rows = frappe.get_all(
			"Party Account",
			filters={"parenttype": parenttype},
			fields=["advance_account"],
		)
		for r in rows:
			add(r.advance_account, adv_type)

	return wanted


def _structurally_valid(account: str, adv_type: str) -> bool:
	row = frappe.db.get_value(
		"Account",
		account,
		["root_type", "account_type", "is_group", "disabled", "account_currency", "company"],
		as_dict=True,
	)
	if not row or cint(row.is_group) or cint(row.disabled):
		return False
	rules = CLASSIFICATION_RULES[adv_type]
	if (row.root_type or "") != rules["root_type"]:
		return False
	# Legacy CoAs may omit account_type; seed will backfill it when empty.
	if (row.account_type or "") and (row.account_type or "") != rules["account_type"]:
		return False
	company_currency = frappe.get_cached_value("Company", row.company, "default_currency")
	acc_currency = (row.account_currency or "").strip() or company_currency
	if company_currency and acc_currency != company_currency:
		return False
	return True


def _ensure_account_type(account: str, adv_type: str) -> None:
	rules = CLASSIFICATION_RULES[adv_type]
	current = (frappe.db.get_value("Account", account, "account_type") or "").strip()
	if not current:
		frappe.db.set_value("Account", account, "account_type", rules["account_type"], update_modified=False)


def execute() -> None:
	ensure_custom_fields()
	if not frappe.db.has_column("Account", CF_ADVANCE_ACCOUNT_TYPE):
		return

	wanted = _collect_candidates()
	conflicts: list[str] = []
	seeded = 0
	skipped = 0

	for account, types in wanted.items():
		if len(types) > 1:
			conflicts.append(f"{account}: {sorted(types)}")
			skipped += 1
			continue

		adv_type = next(iter(types))
		current = (frappe.db.get_value("Account", account, CF_ADVANCE_ACCOUNT_TYPE) or "").strip()
		if current:
			if current != adv_type:
				conflicts.append(f"{account}: existing={current}, wanted={adv_type}")
			skipped += 1
			continue

		if not frappe.db.exists("Account", account):
			skipped += 1
			continue

		if not _structurally_valid(account, adv_type):
			conflicts.append(f"{account}: structurally invalid for {adv_type}")
			skipped += 1
			continue

		_ensure_account_type(account, adv_type)
		frappe.db.set_value("Account", account, CF_ADVANCE_ACCOUNT_TYPE, adv_type, update_modified=False)
		seeded += 1

	if conflicts:
		frappe.logger("advance_accounts").warning(
			"seed_advance_account_types_v5742 conflicts/skips: " + "; ".join(conflicts[:50])
		)

	frappe.logger("advance_accounts").info(
		f"seed_advance_account_types_v5742 seeded={seeded} skipped={skipped} conflicts={len(conflicts)}"
	)
