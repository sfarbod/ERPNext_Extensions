# Copyright (c) 2026, ERPNext Extensions contributors
"""Validate Account.advance_account_type against CoA rules."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.advance_accounts.constants import (
	ADVANCE_PAID,
	ADVANCE_RECEIVED,
	CF_ADVANCE_ACCOUNT_TYPE,
	CLASSIFICATION_RULES,
)


def validate_account_advance_type(doc, method=None) -> None:
	adv_type = (getattr(doc, CF_ADVANCE_ACCOUNT_TYPE, None) or "").strip()
	if not adv_type:
		return

	if adv_type not in (ADVANCE_RECEIVED, ADVANCE_PAID):
		frappe.throw(_("Invalid Advance Account Type: {0}").format(adv_type), title=_("Advance Account"))

	if cint(doc.is_group):
		frappe.throw(
			_("Advance Account Type cannot be set on a group account."),
			title=_("Advance Account"),
		)

	if cint(doc.disabled):
		frappe.throw(
			_("Advance Account Type cannot be set on a disabled account."),
			title=_("Advance Account"),
		)

	rules = CLASSIFICATION_RULES[adv_type]
	if (doc.root_type or "").strip() != rules["root_type"]:
		frappe.throw(
			_("{0} requires Root Type {1} (got {2}).").format(
				_(adv_type), rules["root_type"], doc.root_type or _("empty")
			),
			title=_("Advance Account"),
		)

	if (doc.account_type or "").strip() != rules["account_type"]:
		frappe.throw(
			_("{0} requires Account Type {1} (got {2}).").format(
				_(adv_type), rules["account_type"], doc.account_type or _("empty")
			),
			title=_("Advance Account"),
		)

	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	acc_currency = (doc.account_currency or "").strip() or company_currency
	if company_currency and acc_currency != company_currency:
		frappe.throw(
			_("{0} must use company currency {1} (account currency is {2}).").format(
				_(adv_type), company_currency, acc_currency
			),
			title=_("Advance Account"),
		)


def validate_company_default_advance_accounts(doc, method=None) -> None:
	"""Ensure Company default advance Links match classification rules when set."""
	from erpnext_extensions.advance_accounts.eligibility import (
		account_matches_classification_rules,
		get_account_advance_type,
		get_account_row,
	)

	checks = (
		("default_advance_received_account", ADVANCE_RECEIVED),
		("default_advance_paid_account", ADVANCE_PAID),
	)
	for field, expected_type in checks:
		acc = (doc.get(field) or "").strip()
		if not acc:
			continue
		row = get_account_row(acc)
		if not row:
			frappe.throw(_("Account {0} does not exist.").format(acc), title=_("Advance Account"))
		if (row.get("company") or "").strip() != (doc.name or "").strip():
			frappe.throw(
				_("Advance account {0} must belong to company {1}.").format(acc, doc.name),
				title=_("Advance Account"),
			)
		from erpnext_extensions.advance_accounts.constants import CLASSIFICATION_RULES

		rules = CLASSIFICATION_RULES[expected_type]
		root_ok = (row.get("root_type") or "").strip() == rules["root_type"]
		atype = (row.get("account_type") or "").strip()
		atype_ok = (not atype) or atype == rules["account_type"]
		if cint(row.get("is_group")) or cint(row.get("disabled")) or not root_ok or not atype_ok:
			frappe.throw(
				_(
					"{0} must be a ledger with the correct Root Type / Account Type for {1}."
				).format(frappe.bold(doc.meta.get_label(field)), _(expected_type)),
				title=_("Advance Account"),
			)
		# If classified, must match; empty classification is allowed (seed/compat)
		classified = get_account_advance_type(acc)
		if classified and classified != expected_type:
			frappe.throw(
				_("Account {0} is classified as {1}, not {2}.").format(acc, classified, expected_type),
				title=_("Advance Account"),
			)


def validate_party_advance_account(doc, method=None) -> None:
	"""Customer/Supplier Party Account.advance_account must match direction when set."""
	from erpnext_extensions.advance_accounts.eligibility import (
		account_matches_classification_rules,
		get_account_advance_type,
		get_account_row,
	)

	expected = ADVANCE_RECEIVED if doc.doctype == "Customer" else ADVANCE_PAID
	if doc.doctype == "Customer Group":
		expected = ADVANCE_RECEIVED
	elif doc.doctype == "Supplier Group":
		expected = ADVANCE_PAID

	for row in doc.get("accounts") or []:
		acc = (getattr(row, "advance_account", None) or "").strip()
		if not acc:
			continue
		acc_row = get_account_row(acc)
		if not acc_row:
			frappe.throw(_("Advance Account {0} does not exist.").format(acc), title=_("Advance Account"))
		row_company = (getattr(row, "company", None) or "").strip()
		if row_company and (acc_row.get("company") or "").strip() != row_company:
			frappe.throw(
				_("Advance Account {0} must belong to company {1}.").format(acc, row_company),
				title=_("Advance Account"),
			)
		if not account_matches_classification_rules(acc_row, expected):
			frappe.throw(
				_("Advance Account {0} is not valid for {1}.").format(acc, _(expected)),
				title=_("Advance Account"),
			)
		classified = get_account_advance_type(acc)
		if classified and classified != expected:
			frappe.throw(
				_("Advance Account {0} is classified as {1}, expected {2}.").format(
					acc, classified, expected
				),
				title=_("Advance Account"),
			)
