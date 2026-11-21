# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Resolve Labor/Overhead control accounts from existing classifiers — not ad-hoc hardcodes."""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.fg_cost_allocation.document_breakdown import (
	LABOR_ACCOUNT_CONTROL_PREFIX,
	OVERHEAD_ACCOUNT_CONTROL_PREFIX,
	classify_account_control,
)


@dataclass(frozen=True)
class ControlAccountPair:
	company: str
	overhead_account: str
	labor_account: str
	overhead_account_name: str
	labor_account_name: str


def _pick_account(company: str, prefix: str) -> str | None:
	"""Prefer enabled leaf Account whose number/name starts with the classifier prefix."""
	rows = frappe.db.sql(
		"""
		SELECT name, account_name, account_number, disabled, is_group
		FROM `tabAccount`
		WHERE company = %s
		  AND (
		        account_number = %s
		     OR name LIKE %s
		  )
		ORDER BY disabled ASC, is_group ASC, name ASC
		""",
		(company, prefix, f"{prefix}%"),
		as_dict=True,
	)
	for r in rows:
		if int(r.disabled or 0) or int(r.is_group or 0):
			continue
		kind = classify_account_control(r.name)
		expected = "overhead" if prefix == OVERHEAD_ACCOUNT_CONTROL_PREFIX else "labor"
		if kind == expected:
			return r.name
	return None


def resolve_control_accounts(company: str) -> ControlAccountPair:
	"""Resolve control accounts using the same prefixes as Document Breakdown classifiers."""
	if not company:
		frappe.throw(_("Company is required to resolve control accounts."))

	overhead = _pick_account(company, OVERHEAD_ACCOUNT_CONTROL_PREFIX)
	labor = _pick_account(company, LABOR_ACCOUNT_CONTROL_PREFIX)
	if not overhead or not labor:
		frappe.throw(
			_(
				"Could not resolve Labor/Overhead control accounts for company {0}. "
				"Expected enabled leaf accounts classified as 211237 (overhead) and 211238 (labor)."
			).format(frappe.bold(company))
		)

	return ControlAccountPair(
		company=company,
		overhead_account=overhead,
		labor_account=labor,
		overhead_account_name=frappe.db.get_value("Account", overhead, "account_name") or "",
		labor_account_name=frappe.db.get_value("Account", labor, "account_name") or "",
	)


def control_kind_for_account(account: str | None) -> str | None:
	return classify_account_control(account)
