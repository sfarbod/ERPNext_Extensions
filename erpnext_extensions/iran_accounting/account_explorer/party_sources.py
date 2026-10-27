# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils.caching import request_cache

from erpnext_extensions.iran_accounting.account_explorer.constants import NATIVE_PARTY_TYPES
from erpnext_extensions.iran_accounting.account_explorer.request_cache_helpers import (
	get_iran_accounting_settings,
)


@request_cache
def get_enabled_party_sources() -> list:
	settings = get_iran_accounting_settings()
	rows = [row for row in settings.account_explorer_party_sources or [] if row.enabled]
	return sorted(rows, key=lambda row: int(row.sequence))


def get_enabled_party_types() -> list[str]:
	return [row.party_type for row in get_enabled_party_sources()]


@request_cache
def get_party_source_config_map() -> dict[str, object]:
	return {row.party_type: row for row in get_enabled_party_sources()}


def get_party_source_config(party_type: str):
	return get_party_source_config_map().get(party_type)


def get_identifier_warnings() -> list[str]:
	warnings: list[str] = []
	for row in get_enabled_party_sources():
		if not row.identifier_field:
			continue
		meta = frappe.get_meta(row.party_type)
		if not meta.has_field(row.identifier_field):
			warnings.append(
				_("Identifier field {0} is missing on {1}.").format(row.identifier_field, row.party_type)
			)
	return warnings


def get_party_identifier(party_type: str, party: str, identifier_field: str | None) -> str | None:
	if not identifier_field or not party:
		return None
	meta = frappe.get_meta(party_type)
	if not meta.has_field(identifier_field):
		return None
	return frappe.db.get_value(party_type, party, identifier_field)


# Preferred display fields for native ERPNext party doctypes.
PARTY_DISPLAY_FIELD_MAP = {
	"Customer": "customer_name",
	"Supplier": "supplier_name",
	"Employee": "employee_name",
	"Shareholder": "title",
}

_GENERIC_PARTY_DISPLAY_CANDIDATES = (
	"title",
	"customer_name",
	"supplier_name",
	"employee_name",
	"party_name",
)


@request_cache
def _party_type_display_field(party_type: str) -> str | None:
	if party_type in PARTY_DISPLAY_FIELD_MAP:
		return PARTY_DISPLAY_FIELD_MAP[party_type]
	if not frappe.db.exists("DocType", party_type):
		return None
	meta = frappe.get_meta(party_type)
	for candidate in _GENERIC_PARTY_DISPLAY_CANDIDATES:
		if meta.has_field(candidate):
			return candidate
	return None


def _safe_party_field_value(party_type: str, party: str, fieldname: str) -> str | None:
	"""Return field value only when the column exists — never SELECT a missing field."""
	if not party_type or not party or not fieldname:
		return None
	meta = frappe.get_meta(party_type)
	if not meta.has_field(fieldname):
		return None
	return frappe.db.get_value(party_type, party, fieldname)


def resolve_party_display_name(party_type: str, party: str) -> str:
	"""Safe party display name for Voucher Summary / GL / Print.

	Never queries a non-existent column. Falls back to party id.
	"""
	if not party_type or not party:
		return ""
	preferred = PARTY_DISPLAY_FIELD_MAP.get(party_type)
	if preferred:
		value = _safe_party_field_value(party_type, party, preferred)
		if value:
			return value
		return party
	field = _party_type_display_field(party_type)
	if field:
		value = frappe.db.get_value(party_type, party, field)
		if value:
			return value
	return party


def get_party_display_title(party_type: str, party: str) -> str:
	"""Party-axis Party Name: always prefer the master display field.

	Party ID stays on ``display_code`` / ``party``. Selling/Buying naming
	settings must not hide ``customer_name`` / ``supplier_name`` when the
	document name is a series id (common Production mismatch).
	"""
	return resolve_party_display_name(party_type, party)


def batch_party_display_titles(party_type: str, parties: list[str]) -> dict[str, str]:
	"""Batch-resolve Party Name from the master display field."""
	parties = [party for party in parties if party]
	if not party_type or not parties:
		return {}

	field = _party_type_display_field(party_type)
	if not field:
		return {party: party for party in parties}

	rows = frappe.get_all(
		party_type,
		filters={"name": ("in", parties)},
		fields=["name", field],
		limit=len(parties),
	)
	title_map = {row.name: row.get(field) or row.name for row in rows}
	return {party: title_map.get(party, party) for party in parties}


def batch_party_identifiers(
	party_type: str, parties: list[str], identifier_field: str | None
) -> dict[str, str | None]:
	parties = [party for party in parties if party]
	if not party_type or not parties or not identifier_field:
		return {party: None for party in parties}

	meta = frappe.get_meta(party_type)
	if not meta.has_field(identifier_field):
		return {party: None for party in parties}

	rows = frappe.get_all(
		party_type,
		filters={"name": ("in", parties)},
		fields=["name", identifier_field],
		limit=len(parties),
	)
	id_map = {row.name: row.get(identifier_field) for row in rows}
	return {party: id_map.get(party) for party in parties}


def enrich_party_rows(
	rows: list[dict],
	*,
	source_by_type: dict | None = None,
) -> None:
	"""In-place enrichment for party summary rows (batched per party type)."""
	if not rows:
		return
	source_by_type = source_by_type or get_party_source_config_map()
	by_type: dict[str, list[str]] = {}
	for row in rows:
		if row.get("is_virtual_group"):
			continue
		party_type = row.get("party_type")
		party = row.get("party")
		if not party_type or not party:
			continue
		by_type.setdefault(party_type, []).append(party)

	titles: dict[str, dict[str, str]] = {}
	identifiers: dict[str, dict[str, str | None]] = {}
	for party_type, parties in by_type.items():
		unique_parties = list(dict.fromkeys(parties))
		titles[party_type] = batch_party_display_titles(party_type, unique_parties)
		source = source_by_type.get(party_type)
		id_field = source.identifier_field if source else None
		identifiers[party_type] = batch_party_identifiers(party_type, unique_parties, id_field)

	for row in rows:
		if row.get("is_virtual_group"):
			continue
		party_type = row.get("party_type")
		party = row.get("party")
		if not party_type or not party:
			continue
		row["display_title"] = titles.get(party_type, {}).get(party, party)
		row["party_identifier"] = identifiers.get(party_type, {}).get(party)

	from erpnext_extensions.iran_accounting.account_explorer.party_identity import (
		enrich_party_rows_with_related_roles,
	)

	enrich_party_rows_with_related_roles(rows, source_by_type=source_by_type)


def validate_party_type(party_type: str) -> None:
	if party_type not in NATIVE_PARTY_TYPES:
		frappe.throw(_("Unsupported party type {0}.").format(party_type))
