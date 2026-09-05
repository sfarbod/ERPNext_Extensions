# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Stable QuerySpec fingerprints for Account Explorer prepared results (v4.6.0)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

FINGERPRINT_VERSION = "v511.6"

# Presentation fields applied *after* prepared materialization (sort / page).
_PRESENTATION_KEYS = frozenset({"page", "sort_field", "sort_order", "page_size"})


def _stable(value: Any) -> Any:
	if value is None:
		return None
	if isinstance(value, (str, int, float, bool)):
		return value
	if isinstance(value, (list, tuple)):
		return [_stable(item) for item in value]
	if isinstance(value, set):
		return sorted(_stable(item) for item in value)
	if isinstance(value, dict):
		return {str(key): _stable(value[key]) for key in sorted(value.keys(), key=str)}
	return str(value)


def canonical_query_dict(spec: AccountExplorerQuerySpec) -> dict:
	"""Serialize accounting-relevant QuerySpec fields for hashing.

	Page / sort / page_size are excluded so one prepared artifact serves all
	interactive presentation variants for the same accounting scope.
	"""
	from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
		select_account_fact_engine,
	)

	ds = spec.document_scope
	an = spec.analysis
	return {
		"v": FINGERPRINT_VERSION,
		"company": ds.company,
		"fiscal_year": ds.fiscal_year,
		"from_date": str(ds.from_date) if ds.from_date else None,
		"to_date": str(ds.to_date) if ds.to_date else None,
		"finance_book": ds.finance_book,
		"hide_zero_rows": int(bool(ds.hide_zero_rows)),
		"status": {
			"include_opening_entries": int(bool(ds.status.include_opening_entries)),
			"include_cancelled_entries": int(bool(ds.status.include_cancelled_entries)),
			"include_default_finance_book_entries": int(bool(ds.status.include_default_finance_book_entries)),
			"include_period_closing_vouchers": int(bool(ds.status.include_period_closing_vouchers)),
		},
		"voucher": {
			"voucher_type": ds.voucher.voucher_type,
			"voucher_no": ds.voucher.voucher_no,
			"against_voucher_type": ds.voucher.against_voucher_type,
			"against_voucher_no": ds.voucher.against_voucher_no,
			"reference_no": ds.voucher.reference_no,
		},
		"accounting": {
			"account": _stable(ds.accounting.account),
			"party_type": ds.accounting.party_type,
			"party": _stable(ds.accounting.party),
		},
		"accounting_dimensions": _stable(ds.accounting_dimensions or {}),
		"currency": {
			"currency_type": ds.currency.currency_type,
			"currency": ds.currency.currency,
		},
		"inventory": {
			"item_group": _stable(getattr(ds, "inventory", None) and ds.inventory.item_group),
			"item": _stable(getattr(ds, "inventory", None) and ds.inventory.item),
			"warehouse": _stable(getattr(ds, "inventory", None) and ds.inventory.warehouse),
		},
		"account_fact_engine": select_account_fact_engine(spec),
		"view_axis": an.view_axis,
		"detail_mode": an.detail_mode,
		"level_sequence": an.level_sequence,
		"account_scope": {
			"mode": an.account_scope.mode,
			"selected_account": an.account_scope.selected_account,
			"virtual_row_key": an.account_scope.virtual_row_key,
			"is_virtual_group": int(bool(an.account_scope.is_virtual_group)),
			"level_sequence": an.account_scope.level_sequence,
			"tree_root_account": an.account_scope.tree_root_account,
		},
		"party_scope": {
			"party_type": an.party_scope.party_type,
			"selected_party": an.party_scope.selected_party,
		},
		"unified_party_scope": {
			"selected_unified_party": an.unified_party_scope.selected_unified_party,
			"include_unmapped": int(bool(an.unified_party_scope.include_unmapped)),
		},
		"dimension_scope": {
			"dimension_type": an.dimension_scope.dimension_type,
			"selected_dimension_value": an.dimension_scope.selected_dimension_value,
		},
		"voucher_scope": {
			"voucher_type": an.voucher_scope.voucher_type,
			"voucher_no": an.voucher_scope.voucher_no,
		},
		"item_group_scope": {
			"selected_item_group": getattr(an.item_group_scope, "selected_item_group", None),
		},
		"item_scope": {
			"selected_item": getattr(an.item_scope, "selected_item", None),
		},
		"presentation_currency": spec.presentation_currency,
	}


def query_hash(spec: AccountExplorerQuerySpec) -> str:
	payload = json.dumps(canonical_query_dict(spec), separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def build_fingerprint(spec: AccountExplorerQuerySpec, accounting_revision: int) -> str:
	"""ae:v511:{company}:{query_hash}:{accounting_revision}"""
	company_token = hashlib.sha1((spec.company or "").encode("utf-8")).hexdigest()[:10]
	return f"ae:{FINGERPRINT_VERSION}:{company_token}:{query_hash(spec)}:{int(accounting_revision)}"


def redis_lock_key(fingerprint: str) -> str:
	return f"ae:{FINGERPRINT_VERSION}:lock:{fingerprint}"


def redis_job_meta_key(fingerprint: str) -> str:
	return f"ae:{FINGERPRINT_VERSION}:job:{fingerprint}"
