# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils.caching import request_cache

from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.account_hierarchy import (
	account_matches_configured_level,
	code_prefix,
	configured_level_lengths,
	find_account_by_normalized_number,
	is_pure_numeric_code,
	load_company_accounts,
	normalize_account_number,
)
from erpnext_extensions.iran_accounting.account_explorer.account_scope import (
	make_virtual_prefix_key,
	parse_virtual_prefix_key,
)
from erpnext_extensions.iran_accounting.account_explorer.constants import (
	REAL_ACCOUNT_KEY_PREFIX,
	SORTABLE_FIELDS,
	VIRTUAL_UNCLASSIFIED_KEY,
)
from erpnext_extensions.iran_accounting.account_explorer.measures import (
	add_measures,
	finalize_measures,
	row_has_activity,
	zero_measures,
)
from erpnext_extensions.iran_accounting.account_explorer.opening_balance import (
	get_account_wise_measures,
	get_accounts_with_direct_gl_postings,
)
from erpnext_extensions.iran_accounting.account_explorer.pagination import paginate_summary_rows, sort_rows
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec


@request_cache
def get_enabled_levels() -> list:
	from erpnext_extensions.iran_accounting.account_explorer.request_cache_helpers import (
		get_iran_accounting_settings,
	)

	settings = get_iran_accounting_settings()
	levels = [row for row in settings.account_explorer_levels or [] if row.enabled]
	return sorted(levels, key=lambda row: int(row.sequence))


def get_default_level_sequence() -> int | None:
	levels = get_enabled_levels()
	return int(levels[0].sequence) if levels else None


def build_account_level_summary(spec: AccountExplorerQuerySpec) -> dict:
	level_sequence = spec.level_sequence or get_default_level_sequence()
	levels = get_enabled_levels()
	level = next((row for row in levels if int(row.sequence) == int(level_sequence)), None)
	if not level:
		frappe.throw(_("No enabled Account Explorer level is configured."))

	accounts = load_company_accounts(spec.company)
	configured_lengths = configured_level_lengths(get_enabled_levels())
	scoped_names = spec.included_account_names or []

	measures_by_account = get_account_wise_measures(spec, scoped_names)

	from erpnext_extensions.iran_accounting.account_explorer.measures import row_has_activity

	group_accounts = {name for name in scoped_names if _is_group(accounts, name)}
	direct_posting_groups = get_accounts_with_direct_gl_postings(spec, group_accounts)

	groups: dict[str, dict] = {}
	warnings: list[str] = []
	# v5.1.1: never materialize __UNCLASSIFIED__ as a grid row. Track residual only.
	classification_residual = zero_measures()
	excluded_unclassified_accounts: list[dict] = []

	for account_name in scoped_names:
		row = _row_by_name(accounts, account_name)
		normalized = normalize_account_number(row.get("account_number"))
		account_measures = measures_by_account.get(account_name, zero_measures())

		if not is_pure_numeric_code(normalized):
			reason = "non_numeric_or_missing_account_number"
			_accumulate_unclassified_residual(
				classification_residual,
				excluded_unclassified_accounts,
				account_name=account_name,
				account_number=row.get("account_number"),
				reason=reason,
				measures=account_measures,
			)
			continue

		prefix = code_prefix(normalized, int(level.code_length))
		if not prefix:
			_accumulate_unclassified_residual(
				classification_residual,
				excluded_unclassified_accounts,
				account_name=account_name,
				account_number=row.get("account_number"),
				reason="empty_code_prefix",
				measures=account_measures,
			)
			continue

		if not account_matches_configured_level(normalized, configured_lengths):
			warnings.append(
				_("Account {0} has a code length that does not match configured levels.").format(
					account_name
				)
			)
		group_key = make_virtual_prefix_key(int(level.sequence), prefix)

		group = groups.setdefault(
			group_key,
			{
				"row_key": group_key,
				"display_code": "",
				"display_title": "",
				"is_virtual_group": 1,
				"level_sequence": int(level.sequence),
				"selected_account": None,
				"has_direct_group_posting": 0,
				**zero_measures(),
			},
		)
		add_measures(group, account_measures)

		if account_name in direct_posting_groups:
			group["has_direct_group_posting"] = 1

	_finalize_group_rows(groups, level, accounts, configured_lengths)

	rows = list(groups.values())
	for row in rows:
		finalize_measures(row)

	if spec.hide_zero_rows:
		rows = [row for row in rows if row_has_activity(row)]

	rows = sort_rows(rows, spec, SORTABLE_FIELDS)
	result = paginate_summary_rows(rows, spec)
	result["warnings"] = sorted(set(warnings))
	result["level_sequence"] = int(level.sequence)
	result["level_title"] = level.title

	finalize_measures(classification_residual)
	result["classification_residual"] = {
		"excluded_account_count": len(excluded_unclassified_accounts),
		"excluded_accounts": excluded_unclassified_accounts[:50],
		"period_debit": flt(classification_residual.get("period_debit")),
		"period_credit": flt(classification_residual.get("period_credit")),
		"net_balance": flt(classification_residual.get("net_balance")),
		"note": (
			"Accounts with missing/non-numeric account_number are excluded from "
			"visible Account hierarchy rows and analytical totals (v5.1.1)."
		),
	}
	if excluded_unclassified_accounts and row_has_activity(classification_residual):
		warnings = list(result.get("warnings") or [])
		warnings.append(
			_(
				"Excluded {0} account(s) with missing or non-numeric account codes "
				"from Account hierarchy (residual Debit {1} / Credit {2})."
			).format(
				len(excluded_unclassified_accounts),
				flt(classification_residual.get("period_debit")),
				flt(classification_residual.get("period_credit")),
			)
		)
		result["warnings"] = sorted(set(warnings))

	from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
		ACCOUNT_FACT_ENGINE_SLE_SCOPED,
		select_account_fact_engine,
		sle_scoped_meta,
	)

	engine = select_account_fact_engine(spec)
	result["account_fact_engine"] = engine
	# Never label Case A amounts as E3 / voucher_scoped / construction.
	if engine == ACCOUNT_FACT_ENGINE_SLE_SCOPED:
		meta = sle_scoped_meta(spec)
		result.update(meta)
		result["account_axis_engine"] = ACCOUNT_FACT_ENGINE_SLE_SCOPED
		unmapped_n = int(meta.get("sle_scoped_unmapped_warehouses") or 0)
		unmapped_val = flt(meta.get("sle_scoped_unmapped_signed_value") or 0)
		if unmapped_n or unmapped_val:
			warnings = list(result.get("warnings") or [])
			warnings.append(
				_(
					"Case A: {0} warehouse(s) have scoped stock value with no "
					"resolvable inventory account (unmapped signed value {1}). "
					"Item/Item Group totals include this residual; Account "
					"breakdown shows mapped accounts only."
				).format(unmapped_n, unmapped_val)
			)
			result["warnings"] = warnings
	else:
		result["account_axis_engine"] = engine
	return result


def _accumulate_unclassified_residual(
	residual: dict,
	excluded: list[dict],
	*,
	account_name: str,
	account_number,
	reason: str,
	measures: dict,
) -> None:
	from erpnext_extensions.iran_accounting.account_explorer.measures import row_has_activity

	if not row_has_activity(measures):
		return
	add_measures(residual, measures)
	excluded.append(
		{
			"account": account_name,
			"account_number": account_number,
			"reason": reason,
			"period_debit": flt(measures.get("period_debit")),
			"period_credit": flt(measures.get("period_credit")),
		}
	)


def _finalize_group_rows(groups: dict, level, accounts: list[dict], configured_lengths: set[int]) -> None:
	# Defense: never leave a materialized unclassified taxonomy row in groups.
	groups.pop(VIRTUAL_UNCLASSIFIED_KEY, None)
	for group_key, group in list(groups.items()):
		parsed = parse_virtual_prefix_key(group_key)
		if not parsed:
			continue
		_level_sequence, prefix = parsed
		real = find_account_by_normalized_number(accounts, prefix)
		if real and is_pure_numeric_code(prefix) and len(prefix) == int(level.code_length):
			group.update(
				{
					"row_key": f"{REAL_ACCOUNT_KEY_PREFIX}:{real.name}",
					"display_code": prefix,
					"display_title": real.account_name or real.name,
					"is_virtual_group": 0,
					"selected_account": real.name,
					"is_group": 1 if real.is_group else 0,
				}
			)
		else:
			group.update(
				{
					"display_code": prefix,
					"display_title": prefix,
					"is_virtual_group": 1,
					"selected_account": None,
				}
			)


def _row_by_name(accounts: list[dict], name: str) -> dict:
	for row in accounts:
		if row.name == name:
			return row
	return {}


def _is_group(accounts: list[dict], name: str) -> bool:
	row = _row_by_name(accounts, name)
	return bool(row.get("is_group"))
