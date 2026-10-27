# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

from frappe.utils import cstr

from erpnext_extensions.iran_accounting.account_explorer.constants import PARTY_SORTABLE_FIELDS
from erpnext_extensions.iran_accounting.account_explorer.measures import measures_from_opening_period
from erpnext_extensions.iran_accounting.account_explorer.pagination import (
	is_empty_classification_value,
	paginate_summary_rows,
	sort_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.party_identity import (
	identity_match_key,
	resolve_identity_members,
)
from erpnext_extensions.iran_accounting.account_explorer.party_opening import (
	get_party_opening_balances,
	get_party_period_balances,
)
from erpnext_extensions.iran_accounting.account_explorer.party_sources import (
	enrich_party_rows,
	get_identifier_warnings,
	get_party_source_config_map,
	get_enabled_party_types,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

_SORT_FIELDS_REQUIRING_FULL_ENRICHMENT = frozenset(
	{"display_title", "party_identifier", "related_roles_label"}
)


def _needs_full_party_enrichment(spec: AccountExplorerQuerySpec) -> bool:
	field = spec.pagination.sort_field
	if field not in PARTY_SORTABLE_FIELDS:
		field = "display_code"
	scope = spec.party_scope
	if scope.party_name_query or scope.identifier_query or scope.unified_identity:
		return True
	return field in _SORT_FIELDS_REQUIRING_FULL_ENRICHMENT


def _apply_party_row_filters(rows: list[dict], spec: AccountExplorerQuerySpec) -> list[dict]:
	scope = spec.party_scope
	allowed_pairs: set[tuple[str, str]] | None = None

	if spec.resolved_member_tuples:
		allowed_pairs = {(pt, p) for pt, p in spec.resolved_member_tuples}
	elif scope.unified_identity and (scope.selected_party or scope.identifier_query):
		members = resolve_identity_members(
			party_type=scope.party_type,
			party=scope.selected_party,
			identifier_value=scope.identifier_query,
		)
		allowed_pairs = {(m["party_type"], m["party"]) for m in members}
	elif scope.selected_party and not scope.unified_identity:
		allowed_pairs = {
			(row.get("party_type"), row.get("party"))
			for row in rows
			if row.get("party") == scope.selected_party
			and (not scope.party_type or row.get("party_type") == scope.party_type)
		}

	name_q = cstr(scope.party_name_query or "").strip().casefold()
	id_q = cstr(scope.identifier_query or "").strip()
	id_key = identity_match_key(id_q) if id_q else ""

	filtered: list[dict] = []
	for row in rows:
		party_type = row.get("party_type")
		party = row.get("party")
		if allowed_pairs is not None and (party_type, party) not in allowed_pairs:
			continue
		if scope.party_type and not scope.unified_identity and party_type != scope.party_type:
			continue
		if name_q:
			title = cstr(row.get("display_title") or "").casefold()
			code = cstr(row.get("display_code") or row.get("party") or "").casefold()
			if name_q not in title and name_q not in code:
				continue
		if id_key and not scope.unified_identity:
			if identity_match_key(row.get("party_identifier")) != id_key:
				continue
		filtered.append(row)
	return filtered


def build_party_summary(spec: AccountExplorerQuerySpec) -> dict:
	party_types = get_enabled_party_types()
	if spec.party_scope.party_type and not spec.party_scope.unified_identity:
		party_types = [spec.party_scope.party_type]
	elif spec.resolved_member_tuples:
		party_types = list(dict.fromkeys([pt for pt, _ in spec.resolved_member_tuples] + party_types))

	opening = get_party_opening_balances(spec, party_types)
	period = get_party_period_balances(spec, party_types)
	keys = set(opening.keys()) | set(period.keys())
	if spec.resolved_member_tuples:
		# Keep GL-present members; also include identity members with zero activity.
		keys |= set(spec.resolved_member_tuples)
	elif (
		spec.party_scope.selected_party
		and not spec.party_scope.unified_identity
		and not spec.party_scope.party_name_query
		and not spec.party_scope.identifier_query
	):
		# Exact Party ID — narrow before aggregation materialization.
		selected = spec.party_scope.selected_party
		ptype = spec.party_scope.party_type
		keys = {
			(pt, p)
			for pt, p in keys
			if p == selected and (not ptype or pt == ptype)
		}
	source_by_type = get_party_source_config_map()

	rows: list[dict] = []
	for party_type, party in sorted(keys):
		if is_empty_classification_value(party_type) or is_empty_classification_value(party):
			continue
		opening_debit, opening_credit = opening.get((party_type, party), (0.0, 0.0))
		period_debit, period_credit = period.get((party_type, party), (0.0, 0.0))
		rows.append(
			{
				"row_key": f"party:{party_type}:{party}",
				"party_type": party_type,
				"party": party,
				"display_code": party,
				"display_title": party,
				"party_identifier": None,
				"related_roles": [],
				"related_roles_label": "",
				"is_virtual_group": 0,
				"drill_down_enabled": 0,
				**measures_from_opening_period(
					opening_debit, opening_credit, period_debit, period_credit
				),
			}
		)

	full_enrich = _needs_full_party_enrichment(spec)
	if full_enrich:
		enrich_party_rows(rows, source_by_type=source_by_type)
		rows = _apply_party_row_filters(rows, spec)

	rows = sort_rows(rows, spec, PARTY_SORTABLE_FIELDS)
	result = paginate_summary_rows(rows, spec)

	if not full_enrich:
		enrich_party_rows(result["rows"], source_by_type=source_by_type)

	result["warnings"] = sorted(set(get_identifier_warnings()))
	return result
