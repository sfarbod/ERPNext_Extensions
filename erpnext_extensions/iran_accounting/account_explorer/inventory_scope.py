# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Canonical inventory scope resolver for bidirectional cross-axis filtering.

v5.1.1 asymmetric contract:

CASE A — Item / Item Group / Warehouse filters:
  Stock axes and Account Levels share one scoped SLE population.
  Account Levels = SLE → warehouse → inventory account (sle_scoped_stock).
  Party / Dimension / Currency / Voucher use SLE→voucher→posted GL EXISTS
  (document relationship; no stock-value equality vs Item Group).

CASE B — Account as starting axis (no stock Item/IG filter):
  Account measures = posted GL. Related Items / Item Groups are discovery
  only; reverse equality is NOT required.

GL → Inventory: restrict SLE to vouchers that have scoped GL lines
(``apply_gl_cross_filters_to_sle``) when GL-side filters are active.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import frappe
from frappe.query_builder import DocType
from pypika.terms import ExistsCriterion

from erpnext_extensions.iran_accounting.account_explorer.gle_filters import (
	_has_nonempty_filter_value,
	normalize_filter_values,
)
from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
	get_leaf_item_groups,
	resolve_item_group_scope_names,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.stock_opening import (
	get_item_codes_for_item_groups,
)


class VoucherKey(NamedTuple):
	voucher_type: str
	voucher_no: str


@dataclass(frozen=True)
class InventoryScope:
	"""Resolved inventory population for cross-axis queries."""

	item_codes: frozenset[str] | None = None
	item_groups: frozenset[str] | None = None
	leaf_item_groups: frozenset[str] | None = None
	warehouses: frozenset[str] | None = None

	@property
	def is_inventory_constrained(self) -> bool:
		return bool(self.item_codes or self.item_groups or self.warehouses)

	@property
	def has_item_codes(self) -> bool:
		return self.item_codes is not None and len(self.item_codes) > 0


def _inventory_filter(spec: AccountExplorerQuerySpec):
	return getattr(spec.document_scope, "inventory", None)


def collect_scoped_item_codes(spec: AccountExplorerQuerySpec) -> list[str] | None:
	"""Item codes implied by inventory filters / scopes, or None if unconstrained."""
	inventory = _inventory_filter(spec)
	items = normalize_filter_values(getattr(inventory, "item", None) if inventory else None)
	item_groups = normalize_filter_values(getattr(inventory, "item_group", None) if inventory else None)

	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	if item_group_scope and item_group_scope.selected_item_group:
		item_groups = list({*item_groups, item_group_scope.selected_item_group})

	item_scope = getattr(spec.analysis, "item_scope", None)
	if item_scope and item_scope.selected_item:
		items = list({*items, item_scope.selected_item})

	if not items and not item_groups:
		return None

	resolved: set[str] = set(items)
	if item_groups:
		expanded = resolve_item_group_scope_names(item_groups)
		resolved.update(get_item_codes_for_item_groups(expanded))
	return sorted(resolved)


def warehouses_for_inventory_accounts(company: str, accounts: list[str]) -> list[str]:
	"""ERPNext-native warehouse → inventory account reverse lookup (batched)."""
	if not company or not accounts:
		return []
	from erpnext.stock import get_warehouse_account_map

	wanted = set(accounts)
	wh_map = get_warehouse_account_map(company)
	return sorted(
		wh
		for wh, info in (wh_map or {}).items()
		if info and getattr(info, "account", None) in wanted
	)


def resolve_inventory_scope(spec: AccountExplorerQuerySpec) -> InventoryScope:
	inventory = _inventory_filter(spec)
	item_groups = normalize_filter_values(getattr(inventory, "item_group", None) if inventory else None)
	warehouses = normalize_filter_values(getattr(inventory, "warehouse", None) if inventory else None)
	inventory_accounts = normalize_filter_values(
		getattr(inventory, "inventory_account", None) if inventory else None
	)

	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	if item_group_scope and item_group_scope.selected_item_group:
		item_groups = list({*item_groups, item_group_scope.selected_item_group})

	item_codes_list = collect_scoped_item_codes(spec)
	item_codes = frozenset(item_codes_list) if item_codes_list is not None else None

	expanded_groups: set[str] = set()
	if item_groups:
		expanded_groups = set(resolve_item_group_scope_names(item_groups))
	leaf_groups = frozenset(get_leaf_item_groups(sorted(expanded_groups))) if expanded_groups else None

	if inventory_accounts:
		resolved_wh = warehouses_for_inventory_accounts(spec.company, inventory_accounts)
		if warehouses:
			warehouses = sorted(set(warehouses) & set(resolved_wh))
		else:
			warehouses = resolved_wh

	return InventoryScope(
		item_codes=item_codes,
		item_groups=frozenset(expanded_groups) if expanded_groups else None,
		leaf_item_groups=leaf_groups,
		warehouses=frozenset(warehouses) if warehouses else None,
	)


def has_inventory_document_filters(spec: AccountExplorerQuerySpec) -> bool:
	return resolve_inventory_scope(spec).is_inventory_constrained


def has_gl_cross_filters_for_inventory(spec: AccountExplorerQuerySpec) -> bool:
	"""True when GL-side analysis/document filters should constrain SLE via EXISTS."""
	document = spec.document_scope
	accounting = document.accounting
	if _has_nonempty_filter_value(accounting.account) or _has_nonempty_filter_value(accounting.party):
		return True
	if accounting.party_type:
		return True
	for value in (document.accounting_dimensions or {}).values():
		if _has_nonempty_filter_value(value):
			return True
	currency = document.currency
	if currency and _has_nonempty_filter_value(currency.currency):
		return True
	if spec.party_scope.party_type or _has_nonempty_filter_value(spec.party_scope.selected_party):
		return True
	if _has_nonempty_filter_value(spec.unified_party_scope.selected_unified_party):
		return True
	if spec.resolved_member_tuples:
		return True
	if spec.dimension_scope.selected_dimension_value is not None:
		return True
	if _has_account_analysis_filter(spec):
		return True
	return False


def has_sle_voucher_cross_filters(spec: AccountExplorerQuerySpec) -> bool:
	document = spec.document_scope
	voucher = document.voucher
	if voucher and (
		_has_nonempty_filter_value(voucher.voucher_type)
		or _has_nonempty_filter_value(voucher.voucher_no)
		or _has_nonempty_filter_value(voucher.against_voucher_type)
		or _has_nonempty_filter_value(voucher.against_voucher_no)
	):
		return True
	if _has_nonempty_filter_value(spec.voucher_scope.voucher_type) or _has_nonempty_filter_value(
		spec.voucher_scope.voucher_no
	):
		return True
	return False


def apply_sle_voucher_cross_filters(query, sle, spec: AccountExplorerQuerySpec):
	"""Voucher document/analysis filters map directly onto SLE voucher keys."""
	if not has_sle_voucher_cross_filters(spec):
		return query
	document = spec.document_scope
	voucher = document.voucher
	if voucher.voucher_type:
		query = query.where(sle.voucher_type == voucher.voucher_type)
	if voucher.voucher_no:
		query = query.where(sle.voucher_no == voucher.voucher_no)
	if spec.voucher_scope.voucher_type:
		query = query.where(sle.voucher_type == spec.voucher_scope.voucher_type)
	if spec.voucher_scope.voucher_no:
		query = query.where(sle.voucher_no == spec.voucher_scope.voucher_no)
	return query


def _has_account_analysis_filter(spec: AccountExplorerQuerySpec) -> bool:
	accounting = spec.document_scope.accounting
	if _has_nonempty_filter_value(accounting.account):
		return True
	account_scope = spec.analysis.account_scope
	if account_scope.selected_account or account_scope.virtual_row_key:
		return True
	return False


def _apply_sle_base_match(sub, sle, gle, spec: AccountExplorerQuerySpec):
	sub = sub.where(sle.company == spec.company)
	sub = sub.where(sle.voucher_type == gle.voucher_type)
	sub = sub.where(sle.voucher_no == gle.voucher_no)
	if not spec.include_cancelled_entries:
		sub = sub.where(sle.is_cancelled == 0)
	return sub


def _apply_sle_inventory_population(sub, sle, scope: InventoryScope):
	if scope.item_codes is not None:
		if not scope.item_codes:
			return sub.where(sle.name == "")
		codes = list(scope.item_codes)[:5000]
		sub = sub.where(sle.item_code.isin(codes))
	if scope.warehouses:
		sub = sub.where(sle.warehouse.isin(list(scope.warehouses)))
	return sub


def _apply_gl_cross_filters(sub, gle, spec: AccountExplorerQuerySpec):
	"""Apply GL-side cross-axis filters to a subquery (no inventory voucher scope)."""
	from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import validate_dimension_field
	from erpnext_extensions.iran_accounting.account_explorer.gle_filters import apply_member_tuple_filter

	document = spec.document_scope
	sub = sub.where(gle.company == spec.company)
	if not document.status.include_cancelled_entries:
		sub = sub.where(gle.is_cancelled == 0)

	accounting = document.accounting
	doc_accounts = normalize_filter_values(accounting.account)
	if doc_accounts:
		sub = sub.where(gle.account.isin(doc_accounts))
	elif _has_account_analysis_filter(spec) and spec.included_account_names:
		sub = sub.where(gle.account.isin(spec.included_account_names))

	if accounting.party_type:
		sub = sub.where(gle.party_type == accounting.party_type)
	parties = normalize_filter_values(accounting.party)
	if parties:
		sub = sub.where(gle.party.isin(parties))

	if spec.resolved_member_tuples:
		sub = apply_member_tuple_filter(sub, gle, spec.resolved_member_tuples)
	elif spec.party_scope.party_type:
		sub = sub.where(gle.party_type == spec.party_scope.party_type)
	if spec.party_scope.selected_party:
		sub = sub.where(gle.party == spec.party_scope.selected_party)

	dimension_type = spec.dimension_scope.dimension_type
	if dimension_type and spec.dimension_scope.selected_dimension_value is not None:
		validate_dimension_field(dimension_type)
		dim = gle[dimension_type]
		if spec.dimension_scope.selected_dimension_value == "":
			sub = sub.where((dim == "") | (dim.isnull()))
		else:
			sub = sub.where(dim == spec.dimension_scope.selected_dimension_value)

	for fieldname, value in (document.accounting_dimensions or {}).items():
		if value is None or value == "":
			continue
		validate_dimension_field(fieldname)
		values = normalize_filter_values(value)
		if values:
			dim = gle[fieldname]
			sub = sub.where(dim.isin(values) if len(values) > 1 else dim == values[0])

	voucher = document.voucher
	if voucher.voucher_type:
		sub = sub.where(gle.voucher_type == voucher.voucher_type)
	if voucher.voucher_no:
		sub = sub.where(gle.voucher_no == voucher.voucher_no)

	if spec.voucher_scope.voucher_type:
		sub = sub.where(gle.voucher_type == spec.voucher_scope.voucher_type)
	if spec.voucher_scope.voucher_no:
		sub = sub.where(gle.voucher_no == spec.voucher_scope.voucher_no)

	currency = document.currency
	if currency and _has_nonempty_filter_value(currency.currency):
		if currency.currency_type == "transaction_currency":
			sub = sub.where(
				(gle.transaction_currency == currency.currency)
				| (
					(gle.transaction_currency.isnull() | (gle.transaction_currency == ""))
					& (gle.account_currency == currency.currency)
				)
			)
		else:
			sub = sub.where(gle.account_currency == currency.currency)

	return sub


def apply_inventory_voucher_scope_to_gle(query, gle, spec: AccountExplorerQuerySpec):
	"""Inventory → GL: REAL GL rows whose vouchers have scoped SLE (EXISTS).

	Canonical bridge (no giant IN lists):
	  GL Entry WHERE EXISTS (
	    SELECT 1 FROM tabStock Ledger Entry sle
	    WHERE sle.voucher_type = gle.voucher_type
	      AND sle.voucher_no = gle.voucher_no
	      AND scoped item / item-group / warehouse conditions
	  )

	Applies to opening, period, and closing GL queries alike (same EXISTS; date
	buckets stay on the GL side via opening/turnover policy filters).

	Does **not** filter GL by inventory account alone — only by SLE voucher keys.
	"""
	scope = resolve_inventory_scope(spec)
	if not scope.is_inventory_constrained:
		return query

	sle = DocType("Stock Ledger Entry")
	sub = frappe.qb.from_(sle).select(1)
	sub = _apply_sle_base_match(sub, sle, gle, spec)
	sub = _apply_sle_inventory_population(sub, sle, scope)
	return query.where(ExistsCriterion(sub))


def apply_gl_cross_filters_to_sle(query, sle, spec: AccountExplorerQuerySpec):
	"""GL → Inventory: SLE lines on vouchers with scoped GL postings."""
	if not has_gl_cross_filters_for_inventory(spec):
		return query

	gle = DocType("GL Entry")
	sub = frappe.qb.from_(gle).select(1)
	sub = sub.where(sle.company == gle.company)
	sub = sub.where(sle.voucher_type == gle.voucher_type)
	sub = sub.where(sle.voucher_no == gle.voucher_no)
	sub = _apply_gl_cross_filters(sub, gle, spec)
	return query.where(ExistsCriterion(sub))


def apply_inventory_scope_to_sle_query(query, sle, spec: AccountExplorerQuerySpec):
	"""Apply cross-axis filters to an SLE query (reverse direction)."""
	query = apply_sle_voucher_cross_filters(query, sle, spec)
	return apply_gl_cross_filters_to_sle(query, sle, spec)
