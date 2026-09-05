# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Scoped Stock Ledger Entry filters for Item / Item Group axes."""

from __future__ import annotations

from datetime import datetime, time

import frappe
from frappe.utils import getdate

from erpnext_extensions.iran_accounting.account_explorer.gle_filters import normalize_filter_values
from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
	resolve_item_group_scope_names,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec


def period_start_datetime(from_date) -> datetime:
	return datetime.combine(getdate(from_date), time.min)


def period_end_datetime(to_date) -> datetime:
	return datetime.combine(getdate(to_date), time.max.replace(microsecond=0))


def apply_base_sle_filters(query, sle, spec: AccountExplorerQuerySpec):
	query = query.where(sle.company == spec.company)
	if not spec.include_cancelled_entries:
		query = query.where(sle.is_cancelled == 0)
	return query


def apply_inventory_document_filters(query, sle, spec: AccountExplorerQuerySpec, item_alias=None):
	"""Apply Item / Item Group / Warehouse filters from document_scope.inventory."""
	inventory = getattr(spec.document_scope, "inventory", None)
	if not inventory:
		return query

	warehouses = normalize_filter_values(getattr(inventory, "warehouse", None))
	if warehouses:
		query = query.where(sle.warehouse.isin(warehouses))

	items = normalize_filter_values(getattr(inventory, "item", None))
	if items:
		query = query.where(sle.item_code.isin(items))

	item_groups = normalize_filter_values(getattr(inventory, "item_group", None))
	# Analysis navigation scope also constrains the stock population.
	scope_group = None
	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	if item_group_scope and getattr(item_group_scope, "selected_item_group", None):
		scope_group = item_group_scope.selected_item_group

	expanded_sets: list[set[str]] = []
	if item_groups:
		expanded_sets.append(set(resolve_item_group_scope_names(item_groups)))
	if scope_group:
		expanded_sets.append(set(resolve_item_group_scope_names([scope_group])))

	if expanded_sets:
		expanded = set.intersection(*expanded_sets) if len(expanded_sets) > 1 else expanded_sets[0]
		item = item_alias or frappe.qb.DocType("Item")
		query = query.where(item.item_group.isin(sorted(expanded) or ["__no_match__"]))

	return query


def needs_item_join_for_inventory(spec: AccountExplorerQuerySpec) -> bool:
	inventory = getattr(spec.document_scope, "inventory", None)
	if inventory and normalize_filter_values(getattr(inventory, "item_group", None)):
		return True
	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	if item_group_scope and getattr(item_group_scope, "selected_item_group", None):
		return True
	return False


def apply_sle_opening_boundary(query, sle, spec: AccountExplorerQuerySpec):
	"""Pre-period: posting_datetime < from_date 00:00:00."""
	start = period_start_datetime(spec.from_date)
	return query.where(sle.posting_datetime < start)


def apply_sle_period_boundary(query, sle, spec: AccountExplorerQuerySpec):
	"""In-period: from_date 00:00:00 <= posting_datetime <= to_date 23:59:59."""
	start = period_start_datetime(spec.from_date)
	end = period_end_datetime(spec.to_date)
	return query.where(sle.posting_datetime >= start).where(sle.posting_datetime <= end)


def apply_analysis_item_scope(query, sle, spec: AccountExplorerQuerySpec):
	item_scope = getattr(spec.analysis, "item_scope", None)
	if item_scope and getattr(item_scope, "selected_item", None):
		query = query.where(sle.item_code == item_scope.selected_item)
	return query


def join_item_if_needed(query, sle, spec: AccountExplorerQuerySpec):
	item = frappe.qb.DocType("Item")
	if needs_item_join_for_inventory(spec):
		query = query.inner_join(item).on(item.name == sle.item_code)
		query = apply_inventory_document_filters(query, sle, spec, item_alias=item)
	else:
		query = apply_inventory_document_filters(query, sle, spec, item_alias=None)
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		apply_inventory_scope_to_sle_query,
	)

	query = apply_inventory_scope_to_sle_query(query, sle, spec)
	return query, item
