# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Multi-role Party identity helpers for Account Explorer Parties axis.

Matching identifiers discover related roles. GL Party Type / Party stay distinct.
Balances are never merged into a single accounting identity.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint, cstr

from erpnext_extensions.iran_accounting.account_explorer.measures import measures_from_opening_period
from erpnext_extensions.iran_accounting.account_explorer.party_opening import (
	get_party_opening_balances,
	get_party_period_balances,
)
from erpnext_extensions.iran_accounting.account_explorer.party_sources import (
	batch_party_identifiers,
	get_enabled_party_sources,
	get_enabled_party_types,
	get_party_source_config_map,
	get_identifier_warnings,
	resolve_party_display_name,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.unified_party_registry import (
	normalize_identifier,
	normalize_identifier_digits,
)


def identity_match_key(value: str | None) -> str:
	"""Stable nonempty key for Related Roles / Unified Identity matching."""
	digits = normalize_identifier_digits(value)
	if digits:
		return f"d:{digits}"
	text = normalize_identifier(value)
	return f"t:{text}" if text else ""


def _permission_ok(party_type: str, party: str) -> bool:
	if not party_type or not party:
		return False
	try:
		return bool(frappe.has_permission(party_type, "read", party))
	except Exception:
		return False


def batch_parties_by_identifiers(
	party_type: str,
	identifier_field: str | None,
	identifier_values: list[str],
) -> dict[str, list[str]]:
	"""Map raw identifier value → party names for one party type (batched)."""
	values = [cstr(v).strip() for v in identifier_values if cstr(v).strip()]
	if not party_type or not identifier_field or not values:
		return {}
	meta = frappe.get_meta(party_type)
	if not meta.has_field(identifier_field):
		return {}
	unique_values = list(dict.fromkeys(values))
	rows = frappe.get_all(
		party_type,
		filters={identifier_field: ("in", unique_values)},
		fields=["name", identifier_field],
		limit=max(len(unique_values) * 20, 100),
	)
	out: dict[str, list[str]] = defaultdict(list)
	for row in rows:
		raw = row.get(identifier_field)
		if raw is None or cstr(raw).strip() == "":
			continue
		if not _permission_ok(party_type, row.name):
			continue
		out[cstr(raw)].append(row.name)
	return dict(out)


def build_related_roles_maps(
	rows: list[dict],
	*,
	source_by_type: dict | None = None,
) -> dict[tuple[str, str], list[str]]:
	"""For each (party_type, party) on the page, list related enabled Party Types.

	Uses configured Identifier Field values only. Blank identifiers never match.
	"""
	if not rows:
		return {}
	source_by_type = source_by_type or get_party_source_config_map()
	page_ids: dict[tuple[str, str], str] = {}
	raw_by_key: dict[str, set[str]] = defaultdict(set)

	for row in rows:
		if row.get("is_virtual_group"):
			continue
		party_type = row.get("party_type")
		party = row.get("party")
		raw = row.get("party_identifier")
		if not party_type or not party:
			continue
		key = identity_match_key(raw)
		if not key:
			page_ids[(party_type, party)] = ""
			continue
		page_ids[(party_type, party)] = key
		raw_by_key[key].add(cstr(raw).strip())

	# Reverse lookup across all enabled types that have an identifier field.
	roles_by_key: dict[str, set[str]] = defaultdict(set)
	for source in get_enabled_party_sources():
		id_field = source.identifier_field
		if not id_field:
			continue
		raw_values: list[str] = []
		for key, raws in raw_by_key.items():
			raw_values.extend(raws)
		if not raw_values:
			continue
		found = batch_parties_by_identifiers(source.party_type, id_field, raw_values)
		for raw, parties in found.items():
			key = identity_match_key(raw)
			if not key or not parties:
				continue
			roles_by_key[key].add(source.party_type)

	# Always include the row's own type when it has a nonempty identifier.
	result: dict[tuple[str, str], list[str]] = {}
	enabled_order = [row.party_type for row in get_enabled_party_sources()]
	for (party_type, party), key in page_ids.items():
		if not key:
			result[(party_type, party)] = []
			continue
		roles = set(roles_by_key.get(key) or set())
		roles.add(party_type)
		ordered = [pt for pt in enabled_order if pt in roles]
		for pt in sorted(roles):
			if pt not in ordered:
				ordered.append(pt)
		result[(party_type, party)] = ordered
	return result


def format_related_roles(roles: list[str] | None) -> str:
	return ", ".join(roles or [])


def resolve_identity_members(
	*,
	party_type: str | None = None,
	party: str | None = None,
	identifier_value: str | None = None,
) -> list[dict]:
	"""Resolve related Party masters sharing a nonempty identifier."""
	source_by_type = get_party_source_config_map()
	raw_identifier = identifier_value
	if (not raw_identifier) and party_type and party:
		source = source_by_type.get(party_type)
		id_field = source.identifier_field if source else None
		if id_field:
			raw_identifier = batch_party_identifiers(party_type, [party], id_field).get(party)

	key = identity_match_key(raw_identifier)
	if not key:
		# Single role only — no identifier to fan out.
		if party_type and party and _permission_ok(party_type, party):
			return [
				{
					"party_type": party_type,
					"party": party,
					"display_title": resolve_party_display_name(party_type, party),
					"party_identifier": None,
					"is_seed": 1,
				}
			]
		return []

	members: list[dict] = []
	seen: set[tuple[str, str]] = set()
	raw = cstr(raw_identifier).strip()
	for source in get_enabled_party_sources():
		id_field = source.identifier_field
		if not id_field:
			continue
		found = batch_parties_by_identifiers(source.party_type, id_field, [raw])
		for name in found.get(raw) or []:
			pair = (source.party_type, name)
			if pair in seen:
				continue
			seen.add(pair)
			members.append(
				{
					"party_type": source.party_type,
					"party": name,
					"display_title": resolve_party_display_name(source.party_type, name),
					"party_identifier": raw,
					"is_seed": int(source.party_type == party_type and name == party),
				}
			)

	if party_type and party and (party_type, party) not in seen and _permission_ok(party_type, party):
		members.insert(
			0,
			{
				"party_type": party_type,
				"party": party,
				"display_title": resolve_party_display_name(party_type, party),
				"party_identifier": raw,
				"is_seed": 1,
			},
		)
	return members


def build_party_identity_analysis(
	spec: AccountExplorerQuerySpec,
	*,
	party_type: str,
	party: str,
) -> dict:
	"""Per-role finances + analytical combined totals for a Party Analyze click."""
	members = resolve_identity_members(party_type=party_type, party=party)
	if not members:
		return {
			"identity": {
				"party_type": party_type,
				"party": party,
				"display_title": resolve_party_display_name(party_type, party),
				"party_identifier": None,
				"related_roles": [],
			},
			"roles": [],
			"combined": measures_from_opening_period(0, 0, 0, 0),
			"member_tuples": [],
			"warnings": sorted(set(get_identifier_warnings())),
		}

	party_types = list(dict.fromkeys([m["party_type"] for m in members] + get_enabled_party_types()))
	opening = get_party_opening_balances(spec, party_types)
	period = get_party_period_balances(spec, party_types)

	role_rows: list[dict] = []
	od = oc = pd = pc = 0.0
	for member in members:
		key = (member["party_type"], member["party"])
		opening_debit, opening_credit = opening.get(key, (0.0, 0.0))
		period_debit, period_credit = period.get(key, (0.0, 0.0))
		measures = measures_from_opening_period(
			opening_debit, opening_credit, period_debit, period_credit
		)
		role_rows.append(
			{
				"row_key": f"party:{member['party_type']}:{member['party']}",
				"party_type": member["party_type"],
				"party": member["party"],
				"display_code": member["party"],
				"display_title": member["display_title"],
				"party_identifier": member.get("party_identifier"),
				"is_seed": cint(member.get("is_seed")),
				"drill_down_enabled": 0,
				**measures,
			}
		)
		od += opening_debit
		oc += opening_credit
		pd += period_debit
		pc += period_credit

	seed = next((m for m in members if m.get("is_seed")), members[0])
	related_roles = list(dict.fromkeys(m["party_type"] for m in members))
	return {
		"identity": {
			"party_type": seed["party_type"],
			"party": seed["party"],
			"display_title": seed["display_title"],
			"party_identifier": seed.get("party_identifier"),
			"related_roles": related_roles,
			"related_roles_label": format_related_roles(related_roles),
		},
		"roles": role_rows,
		"combined": measures_from_opening_period(od, oc, pd, pc),
		"member_tuples": [(m["party_type"], m["party"]) for m in members],
		"warnings": sorted(set(get_identifier_warnings())),
	}


def enrich_party_rows_with_related_roles(
	rows: list[dict],
	*,
	source_by_type: dict | None = None,
) -> None:
	"""Attach related_roles / related_roles_label after identifiers are set."""
	maps = build_related_roles_maps(rows, source_by_type=source_by_type)
	for row in rows:
		if row.get("is_virtual_group"):
			continue
		party_type = row.get("party_type")
		party = row.get("party")
		roles = maps.get((party_type, party), []) if party_type and party else []
		row["related_roles"] = roles
		row["related_roles_label"] = format_related_roles(roles)
