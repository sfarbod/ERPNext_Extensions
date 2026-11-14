# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils.caching import request_cache

from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.account_hierarchy import (
	account_in_subtree,
	account_matches_configured_level,
	accounts_by_name,
	code_prefix,
	configured_level_lengths,
	direct_child_accounts,
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


# Execution-local leaf measures (Case B). Keyed by execution_id + revision +
# financial hash (level_sequence excluded). Not used for Case A SLE.
_LEAF_MEASURES_CACHE_TTL_SEC = 30 * 60


def _get_account_wise_measures_for_level_summary(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None
) -> dict[str, dict]:
	"""Fetch leaf measures, reusing an execution-scoped cache when safe.

	Reuse requires the same ``execution_id``, accounting revision, and financial
	scope (dates/filters/engine). Account Level changes are presentation rollups
	and share the cached leaf dict. Case A inventory scopes never use this cache
	because GL revision does not prove SLE freshness.
	"""
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		has_inventory_document_filters,
	)

	if has_inventory_document_filters(spec):
		return get_account_wise_measures(spec, account_names)

	execution_id = getattr(frappe.flags, "ae_execution_id", None)
	if not execution_id:
		return get_account_wise_measures(spec, account_names)

	from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
		get_accounting_revision,
	)
	from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
		measures_query_hash,
	)

	revision = int(get_accounting_revision(spec.company))
	cache_key = f"ae:leaf:{execution_id}:{measures_query_hash(spec)}:{revision}"
	cached = frappe.cache().get_value(cache_key)
	if isinstance(cached, dict):
		frappe.flags.ae_leaf_measures_cache_hit = 1
		if account_names is None:
			return cached
		return {
			name: cached.get(name) or zero_measures()
			for name in account_names
		}

	measures = get_account_wise_measures(spec, account_names)
	frappe.cache().set_value(cache_key, measures, expires_in_sec=_LEAF_MEASURES_CACHE_TTL_SEC)
	frappe.flags.ae_leaf_measures_cache_hit = 0
	return measures


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

	measures_by_account = _get_account_wise_measures_for_level_summary(spec, scoped_names)

	from erpnext_extensions.iran_accounting.account_explorer.measures import row_has_activity

	group_accounts = {name for name in scoped_names if _is_group(accounts, name)}
	direct_posting_groups = get_accounts_with_direct_gl_postings(spec, group_accounts)

	groups: dict[str, dict] = {}
	warnings: list[str] = []
	# v5.1.1: never materialize __UNCLASSIFIED__ as a grid row. Track residual only.
	classification_residual = zero_measures()
	excluded_unclassified_accounts: list[dict] = []

	# Analyze / hierarchy drill: NestedSet is authoritative. Code-prefix rollups can
	# invent sibling labels when a leaf is mis-parented under another branch
	# (e.g. account_number 111206 under Group 21 → phantom Group 11).
	tree_root = spec.account_scope.tree_root_account or (
		spec.account_scope.selected_account if spec.account_scope.mode == "account" else None
	)
	level_code_length = int(level.code_length)

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

		prefix = code_prefix(normalized, level_code_length)
		if not prefix:
			# Terminal / coarser-level codes under an analyzed root are optional L4/L5
			# absences — not classification integrity failures.
			if (
				tree_root
				and account_name == tree_root
				and len(normalized) < level_code_length
			):
				continue
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

	if tree_root:
		rows = _filter_rows_to_nested_set_subtree(
			rows,
			accounts,
			tree_root,
			exclude_root=True,
		)
		if not rows:
			# Prefer NestedSet direct children over silently returning company-wide groups.
			rows = _build_nested_set_child_rows(
				accounts,
				tree_root,
				measures_by_account,
				level_sequence=int(level.sequence),
			)
			for row in rows:
				finalize_measures(row)
		if not rows:
			# Terminal Subsidiary Ledger (or any leaf): surface own GL turnover.
			terminal = _build_terminal_leaf_row(
				accounts,
				tree_root,
				measures_by_account,
				level_sequence=int(level.sequence),
			)
			if terminal:
				finalize_measures(terminal)
				rows = [terminal]

	if spec.hide_zero_rows:
		rows = [row for row in rows if row_has_activity(row)]

	rows = sort_rows(rows, spec, SORTABLE_FIELDS)
	result = paginate_summary_rows(rows, spec)
	result["warnings"] = sorted(set(warnings))
	result["level_sequence"] = int(level.sequence)
	result["level_title"] = level.title

	finalize_measures(classification_residual)
	from erpnext_extensions.iran_accounting.account_explorer.classification_exclusions import (
		build_classification_reconciliation,
		enrich_excluded_account_rows,
		persian_exclusion_message,
	)

	enriched_excluded = enrich_excluded_account_rows(spec.company, excluded_unclassified_accounts)
	included_totals = result.get("totals") or zero_measures()
	reconciliation = build_classification_reconciliation(
		included_totals=included_totals,
		excluded_residual=classification_residual,
	)
	# Cap detail payload; typical residual is a handful of accounts.
	detail_rows = enriched_excluded[:50]
	result["classification_residual"] = {
		"excluded_account_count": len(enriched_excluded),
		"excluded_accounts": [
			{
				"account": row["account"],
				"account_number": row.get("account_number"),
				"account_name": row.get("account_name"),
				"reason": row.get("reason"),
				"period_debit": row.get("period_debit"),
				"period_credit": row.get("period_credit"),
			}
			for row in detail_rows
		],
		# Full enriched rows for the details dialog (same scope; no second GL scan).
		"excluded_account_details": detail_rows,
		"period_debit": flt(classification_residual.get("period_debit")),
		"period_credit": flt(classification_residual.get("period_credit")),
		"net_balance": flt(classification_residual.get("net_balance")),
		"debit_balance": flt(classification_residual.get("debit_balance")),
		"credit_balance": flt(classification_residual.get("credit_balance")),
		"reconciliation": reconciliation,
		"message_fa": persian_exclusion_message(len(enriched_excluded)) if enriched_excluded else None,
		"note": (
			"Accounts with missing/non-numeric account_number are excluded from "
			"visible Account hierarchy rows. Included + Excluded = Full Scope "
			"for additive measures (period debit/credit, net_balance)."
		),
	}
	if enriched_excluded and row_has_activity(classification_residual):
		warnings = list(result.get("warnings") or [])
		warnings.append(persian_exclusion_message(len(enriched_excluded)))
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
	row = {
		"account": account_name,
		"account_number": account_number,
		"reason": reason,
		"opening_debit": flt(measures.get("opening_debit")),
		"opening_credit": flt(measures.get("opening_credit")),
		"period_debit": flt(measures.get("period_debit")),
		"period_credit": flt(measures.get("period_credit")),
		"closing_debit": flt(measures.get("closing_debit")),
		"closing_credit": flt(measures.get("closing_credit")),
		"net_balance": flt(measures.get("net_balance")),
		"debit_balance": flt(measures.get("debit_balance")),
		"credit_balance": flt(measures.get("credit_balance")),
	}
	excluded.append(row)


def _filter_rows_to_nested_set_subtree(
	rows: list[dict],
	accounts: list[dict],
	tree_root: str,
	*,
	exclude_root: bool = True,
) -> list[dict]:
	"""Keep only presentation rows whose selected_account lies in the NestedSet subtree."""
	by_name = accounts_by_name(accounts)
	root = by_name.get(tree_root)
	if not root:
		frappe.throw(
			_("Account hierarchy node {0} was not found for company scope.").format(tree_root)
		)
	kept: list[dict] = []
	for row in rows:
		selected = row.get("selected_account")
		if not selected:
			# Virtual prefix without a real Account master — only keep when the
			# display code itself resolves inside the NestedSet subtree.
			code = normalize_account_number(row.get("display_code"))
			matched = find_account_by_normalized_number(accounts, code) if code else None
			selected = matched.name if matched else None
		if not selected:
			continue
		if not account_in_subtree(
			by_name,
			selected,
			tree_root,
			include_root=not exclude_root,
		):
			continue
		kept.append(row)
	return kept


def _build_nested_set_child_rows(
	accounts: list[dict],
	tree_root: str,
	measures_by_account: dict[str, dict],
	*,
	level_sequence: int,
) -> list[dict]:
	"""Aggregate NestedSet direct children of ``tree_root`` (Analyze destination)."""
	by_name = accounts_by_name(accounts)
	root = by_name.get(tree_root)
	if not root:
		frappe.throw(
			_("Account hierarchy node {0} was not found for company scope.").format(tree_root)
		)
	rows: list[dict] = []
	for child in direct_child_accounts(accounts, tree_root):
		subtree_names = [
			row.name
			for row in accounts
			if int(row.lft) >= int(child.lft) and int(row.rgt) <= int(child.rgt)
		]
		group = {
			"row_key": f"{REAL_ACCOUNT_KEY_PREFIX}:{child.name}",
			"display_code": normalize_account_number(child.account_number) or child.name,
			"display_title": child.account_name or child.name,
			"is_virtual_group": 0,
			"level_sequence": level_sequence,
			"selected_account": child.name,
			"is_group": 1 if child.is_group else 0,
			"has_direct_group_posting": 0,
			**zero_measures(),
		}
		for name in subtree_names:
			add_measures(group, measures_by_account.get(name, zero_measures()))
		rows.append(group)
	return rows


def _build_terminal_leaf_row(
	accounts: list[dict],
	tree_root: str,
	measures_by_account: dict[str, dict],
	*,
	level_sequence: int,
) -> dict | None:
	"""Own-turnover row when ``tree_root`` has no NestedSet children (terminal ledger).

	Account Levels 4/5 are optional. A Subsidiary Ledger leaf must remain
	analyzable and must expose its own posted GL measures without inventing
	children or emitting a false classification residual.
	"""
	by_name = accounts_by_name(accounts)
	root = by_name.get(tree_root)
	if not root:
		frappe.throw(
			_("Account hierarchy node {0} was not found for company scope.").format(tree_root)
		)
	if direct_child_accounts(accounts, tree_root):
		return None
	measures = measures_by_account.get(tree_root, zero_measures())
	row = {
		"row_key": f"{REAL_ACCOUNT_KEY_PREFIX}:{root.name}",
		"display_code": normalize_account_number(root.account_number) or root.name,
		"display_title": root.account_name or root.name,
		"is_virtual_group": 0,
		"level_sequence": level_sequence,
		"selected_account": root.name,
		"is_group": 1 if root.is_group else 0,
		"is_terminal_account": 1,
		"has_direct_group_posting": 0,
		**zero_measures(),
	}
	add_measures(row, measures)
	return row


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
