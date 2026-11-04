# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.account_explorer.constants import (
	ARABIC_DIGITS,
	PERSIAN_DIGITS,
)


def normalize_account_number(raw: str | None) -> str:
	if not raw:
		return ""
	value = str(raw).strip()
	value = value.translate(PERSIAN_DIGITS).translate(ARABIC_DIGITS)
	return value


def is_pure_numeric_code(normalized: str) -> bool:
	return bool(normalized) and normalized.isdigit()


def account_matches_configured_level(normalized: str, configured_lengths: set[int]) -> bool:
	return is_pure_numeric_code(normalized) and len(normalized) in configured_lengths


def code_prefix(normalized: str, code_length: int) -> str | None:
	if not is_pure_numeric_code(normalized) or len(normalized) < code_length:
		return None
	return normalized[:code_length]


def load_company_accounts(company: str) -> list[dict]:
	return frappe.db.sql(
		"""
		select
			name, account_name, account_number, parent_account, is_group, disabled,
			lft, rgt, root_type, report_type, account_currency
		from `tabAccount`
		where company = %s
		order by lft
		""",
		company,
		as_dict=True,
	)


def accounts_by_name(accounts: list[dict]) -> dict[str, dict]:
	return {row.name: row for row in accounts}


def descendant_accounts(accounts: list[dict], root_account: str | None) -> list[str]:
	if not root_account:
		return [row.name for row in accounts]
	root = next((row for row in accounts if row.name == root_account), None)
	if not root:
		return []
	return [row.name for row in accounts if row.lft >= root.lft and row.rgt <= root.rgt]


def account_in_subtree(
	accounts: list[dict] | dict[str, dict],
	account_name: str | None,
	root_account: str | None,
	*,
	include_root: bool = True,
) -> bool:
	"""True when ``account_name`` is in the NestedSet subtree of ``root_account``."""
	if not account_name or not root_account:
		return False
	if isinstance(accounts, dict):
		by_name = accounts
	else:
		by_name = accounts_by_name(accounts)
	root = by_name.get(root_account)
	node = by_name.get(account_name)
	if not root or not node:
		return False
	if node.name == root.name:
		return bool(include_root)
	return int(node.lft) >= int(root.lft) and int(node.rgt) <= int(root.rgt)


def direct_child_accounts(accounts: list[dict], root_account: str | None) -> list[dict]:
	"""Return NestedSet direct children of ``root_account`` (same company list)."""
	if not root_account:
		return []
	return [row for row in accounts if row.parent_account == root_account]


def find_account_by_normalized_number(accounts: list[dict], normalized_number: str) -> dict | None:
	for row in accounts:
		if normalize_account_number(row.account_number) == normalized_number:
			return row
	return None


def configured_level_lengths(settings_levels: list) -> set[int]:
	return {int(row.code_length) for row in settings_levels or [] if row.enabled}
