# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""SQL aggregation helpers over Stock Ledger Entry for Item / Item Group axes.

Opening / period semantics follow ERPNext Stock Balance:

1. Stock Reconciliation (non-serial absolute set):
   qty_diff  = qty_after_transaction - previous_qty_after
   value_diff = stock_value - previous_stock_value
   (NOT raw actual_qty, which is often 0 for Opening Stock / value-only recon)

2. Other vouchers:
   qty_diff = actual_qty
   value_diff = stock_value_difference

3. Opening includes:
   - posting_datetime < from_date 00:00:00, OR
   - voucher is an Opening Stock Reconciliation / Stock Entry is_opening=Yes
     with posting_date <= to_date (ERPNext opening voucher rule)

4. Period movement excludes opening vouchers so they are not double-counted
   as In/Out.

Boundary: transactions ON from_date are period movement unless classified as
opening vouchers (case B above).
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.gle_filters import normalize_filter_values
from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
	resolve_item_group_scope_names,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.stock_sle_filters import (
	period_end_datetime,
	period_start_datetime,
)


def _gl_exists_sql(spec: AccountExplorerQuerySpec, *, sle_alias: str) -> tuple[str, list]:
	"""Correlated EXISTS matching inventory_scope.apply_gl_cross_filters_to_sle."""
	from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import validate_dimension_field
	from erpnext_extensions.iran_accounting.account_explorer.gle_filters import _has_nonempty_filter_value
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		_has_account_analysis_filter,
		has_gl_cross_filters_for_inventory,
	)

	if not has_gl_cross_filters_for_inventory(spec):
		return "", []

	document = spec.document_scope
	parts = [
		f"gle.company = `{sle_alias}`.company",
		f"gle.voucher_type = `{sle_alias}`.voucher_type",
		f"gle.voucher_no = `{sle_alias}`.voucher_no",
	]
	params: list = []

	if not document.status.include_cancelled_entries:
		parts.append("gle.is_cancelled = 0")

	accounting = document.accounting
	doc_accounts = normalize_filter_values(accounting.account)
	if doc_accounts:
		parts.append(f"gle.account in ({', '.join(['%s'] * len(doc_accounts))})")
		params.extend(doc_accounts)
	elif getattr(spec, "included_account_names", None) and _has_account_analysis_filter(spec):
		accts = list(spec.included_account_names)[:5000]
		if accts:
			parts.append(f"gle.account in ({', '.join(['%s'] * len(accts))})")
			params.extend(accts)

	if accounting.party_type:
		parts.append("gle.party_type = %s")
		params.append(accounting.party_type)
	parties = normalize_filter_values(accounting.party)
	if parties:
		parts.append(f"gle.party in ({', '.join(['%s'] * len(parties))})")
		params.extend(parties)

	if spec.party_scope.party_type:
		parts.append("gle.party_type = %s")
		params.append(spec.party_scope.party_type)
	if spec.party_scope.selected_party:
		parts.append("gle.party = %s")
		params.append(spec.party_scope.selected_party)

	dimension_type = spec.dimension_scope.dimension_type
	if dimension_type and spec.dimension_scope.selected_dimension_value is not None:
		validate_dimension_field(dimension_type)
		if spec.dimension_scope.selected_dimension_value == "":
			parts.append(f"(gle.`{dimension_type}` = '' OR gle.`{dimension_type}` IS NULL)")
		else:
			parts.append(f"gle.`{dimension_type}` = %s")
			params.append(spec.dimension_scope.selected_dimension_value)

	for fieldname, value in (document.accounting_dimensions or {}).items():
		if value is None or value == "":
			continue
		validate_dimension_field(fieldname)
		values = normalize_filter_values(value)
		if values:
			parts.append(f"gle.`{fieldname}` in ({', '.join(['%s'] * len(values))})")
			params.extend(values)

	currency = document.currency
	if currency and _has_nonempty_filter_value(currency.currency):
		if currency.currency_type == "transaction_currency":
			parts.append(
				"(gle.transaction_currency = %s OR ((gle.transaction_currency IS NULL OR gle.transaction_currency = '') AND gle.account_currency = %s))"
			)
			params.extend([currency.currency, currency.currency])
		else:
			parts.append("gle.account_currency = %s")
			params.append(currency.currency)

	exists = f"EXISTS (SELECT 1 FROM `tabGL Entry` gle WHERE {' AND '.join(parts)})"
	return exists, params


def _inventory_where_clauses(spec: AccountExplorerQuerySpec, *, alias: str = "sle") -> tuple[str, list]:
	"""Build reusable SLE WHERE fragments + params (company/cancelled/inventory/GL cross)."""
	clauses = [f"`{alias}`.company = %s"]
	params: list = [spec.company]
	if not spec.include_cancelled_entries:
		clauses.append(f"`{alias}`.is_cancelled = 0")

	inventory = getattr(spec.document_scope, "inventory", None)
	warehouses = normalize_filter_values(getattr(inventory, "warehouse", None) if inventory else None)
	if warehouses:
		clauses.append(f"`{alias}`.warehouse in ({', '.join(['%s'] * len(warehouses))})")
		params.extend(warehouses)

	items = normalize_filter_values(getattr(inventory, "item", None) if inventory else None)
	item_scope = getattr(spec.analysis, "item_scope", None)
	if item_scope and getattr(item_scope, "selected_item", None):
		items = list({*(items or []), item_scope.selected_item})
	if items:
		clauses.append(f"`{alias}`.item_code in ({', '.join(['%s'] * len(items))})")
		params.extend(items)

	item_groups = normalize_filter_values(getattr(inventory, "item_group", None) if inventory else None)
	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	scope_group = (
		item_group_scope.selected_item_group
		if item_group_scope and getattr(item_group_scope, "selected_item_group", None)
		else None
	)
	expanded_sets: list[set[str]] = []
	if item_groups:
		expanded_sets.append(set(resolve_item_group_scope_names(item_groups)))
	if scope_group:
		expanded_sets.append(set(resolve_item_group_scope_names([scope_group])))
	if expanded_sets:
		expanded = set.intersection(*expanded_sets) if len(expanded_sets) > 1 else expanded_sets[0]
		if not expanded:
			clauses.append("1=0")
		else:
			clauses.append(
				f"`{alias}`.item_code in (select name from `tabItem` where item_group in ({', '.join(['%s'] * len(expanded))}))"
			)
			params.extend(sorted(expanded))

	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		has_gl_cross_filters_for_inventory,
		has_sle_voucher_cross_filters,
	)

	if has_sle_voucher_cross_filters(spec):
		voucher = spec.document_scope.voucher
		if voucher.voucher_type:
			clauses.append(f"`{alias}`.voucher_type = %s")
			params.append(voucher.voucher_type)
		if voucher.voucher_no:
			clauses.append(f"`{alias}`.voucher_no = %s")
			params.append(voucher.voucher_no)
		if spec.voucher_scope.voucher_type:
			clauses.append(f"`{alias}`.voucher_type = %s")
			params.append(spec.voucher_scope.voucher_type)
		if spec.voucher_scope.voucher_no:
			clauses.append(f"`{alias}`.voucher_no = %s")
			params.append(spec.voucher_scope.voucher_no)

	if has_gl_cross_filters_for_inventory(spec):
		exists_sql, exists_params = _gl_exists_sql(spec, sle_alias=alias)
		if exists_sql:
			clauses.append(exists_sql)
			params.extend(exists_params)

	return " AND ".join(clauses), params


def _opening_voucher_sql(spec: AccountExplorerQuerySpec) -> tuple[str, list]:
	"""Names of Opening Stock SR + Stock Entry is_opening=Yes with posting_date <= to_date."""
	params = [spec.to_date, spec.to_date]
	sql = """
		SELECT name FROM `tabStock Reconciliation`
		WHERE docstatus = 1 AND purpose = 'Opening Stock' AND posting_date <= %s
		UNION ALL
		SELECT name FROM `tabStock Entry`
		WHERE docstatus = 1 AND is_opening = 'Yes' AND posting_date <= %s
	"""
	return sql, params


def _aggregate_stock_buckets(
	spec: AccountExplorerQuerySpec,
	*,
	group_col: str,
	key_name: str,
	join_item: bool = False,
) -> tuple[dict[str, dict], dict[str, dict]]:
	"""Return (opening_map, period_map) keyed by group_col."""
	start = period_start_datetime(spec.from_date)
	end = period_end_datetime(spec.to_date)
	where_sql, where_params = _inventory_where_clauses(spec, alias="sle")
	opening_sql, opening_params = _opening_voucher_sql(spec)

	item_join = "INNER JOIN `tabItem` item ON item.name = sle.item_code" if join_item else ""

	sql = f"""
		WITH opening_vouchers AS (
			{opening_sql}
		),
		base_sle AS (
			SELECT
				sle.name,
				sle.item_code,
				sle.warehouse,
				sle.posting_datetime,
				sle.creation,
				sle.voucher_type,
				sle.voucher_no,
				sle.actual_qty,
				sle.qty_after_transaction,
				sle.stock_value,
				sle.stock_value_difference,
				sle.batch_no,
				sle.serial_no,
				{"item.item_group AS item_group," if join_item else ""}
				CASE
					WHEN EXISTS (
						SELECT 1 FROM opening_vouchers ov WHERE ov.name = sle.voucher_no
					) THEN 1 ELSE 0
				END AS is_opening_voucher
			FROM `tabStock Ledger Entry` sle
			{item_join}
			WHERE {where_sql}
			  AND sle.posting_datetime <= %s
		),
		normalized AS (
			SELECT
				b.*,
				LAG(b.qty_after_transaction) OVER (
					PARTITION BY b.item_code, b.warehouse
					ORDER BY b.posting_datetime, b.creation, b.name
				) AS prev_qty,
				LAG(b.stock_value) OVER (
					PARTITION BY b.item_code, b.warehouse
					ORDER BY b.posting_datetime, b.creation, b.name
				) AS prev_value
			FROM base_sle b
		),
		diffs AS (
			SELECT
				{group_col} AS grp,
				posting_datetime,
				is_opening_voucher,
				CASE
					WHEN voucher_type = 'Stock Reconciliation'
						AND IFNULL(batch_no, '') = ''
						AND IFNULL(serial_no, '') = ''
					THEN qty_after_transaction - IFNULL(prev_qty, 0)
					ELSE actual_qty
				END AS qty_diff,
				CASE
					WHEN voucher_type = 'Stock Reconciliation'
						AND IFNULL(batch_no, '') = ''
						AND IFNULL(serial_no, '') = ''
					THEN stock_value - IFNULL(prev_value, 0)
					ELSE stock_value_difference
				END AS value_diff
			FROM normalized
		)
		SELECT
			grp AS `{key_name}`,
			SUM(CASE
				WHEN posting_datetime < %s OR is_opening_voucher = 1 THEN qty_diff ELSE 0
			END) AS opening_qty,
			SUM(CASE
				WHEN posting_datetime < %s OR is_opening_voucher = 1 THEN value_diff ELSE 0
			END) AS opening_value,
			SUM(CASE
				WHEN posting_datetime >= %s AND posting_datetime <= %s AND is_opening_voucher = 0
					AND qty_diff > 0 THEN qty_diff ELSE 0
			END) AS in_qty,
			SUM(CASE
				WHEN posting_datetime >= %s AND posting_datetime <= %s AND is_opening_voucher = 0
					AND qty_diff < 0 THEN -qty_diff ELSE 0
			END) AS out_qty,
			SUM(CASE
				WHEN posting_datetime >= %s AND posting_datetime <= %s AND is_opening_voucher = 0
					AND value_diff > 0 THEN value_diff ELSE 0
			END) AS inward_value,
			SUM(CASE
				WHEN posting_datetime >= %s AND posting_datetime <= %s AND is_opening_voucher = 0
					AND value_diff < 0 THEN -value_diff ELSE 0
			END) AS outward_value
		FROM diffs
		WHERE grp IS NOT NULL AND grp != ''
		GROUP BY grp
	"""

	# Param order: opening_vouchers (2) + where_params + end + start*2 + (start,end)*4
	params = [
		*opening_params,
		*where_params,
		end,
		start,
		start,
		start,
		end,
		start,
		end,
		start,
		end,
		start,
		end,
	]
	rows = frappe.db.sql(sql, tuple(params), as_dict=True)
	opening: dict[str, dict] = {}
	period: dict[str, dict] = {}
	for row in rows:
		key = row.get(key_name) or ""
		if not key:
			continue
		oq = flt(row.opening_qty)
		ov = flt(row.opening_value)
		iq = flt(row.in_qty)
		oq_out = flt(row.out_qty)
		iv = flt(row.inward_value)
		ov_out = flt(row.outward_value)
		if oq or ov:
			opening[key] = {"opening_qty": oq, "opening_value": ov}
		if iq or oq_out or iv or ov_out:
			period[key] = {
				"in_qty": iq,
				"out_qty": oq_out,
				"inward_value": iv,
				"outward_value": ov_out,
			}
		# Opening-only or period-only already handled; also keep zero-period keys with opening
		if (oq or ov) and key not in period:
			pass
		if (iq or oq_out or iv or ov_out) and key not in opening:
			pass
	return opening, period


def get_item_opening_buckets(spec: AccountExplorerQuerySpec) -> dict[str, dict]:
	opening, _period = _aggregate_stock_buckets(spec, group_col="item_code", key_name="item_code")
	return opening


def get_item_period_buckets(spec: AccountExplorerQuerySpec) -> dict[str, dict]:
	_opening, period = _aggregate_stock_buckets(spec, group_col="item_code", key_name="item_code")
	return period


def get_item_group_opening_buckets(spec: AccountExplorerQuerySpec) -> dict[str, dict]:
	opening, _period = _aggregate_stock_buckets(
		spec,
		group_col="item_group",
		key_name="item_group",
		join_item=True,
	)
	return opening


def get_item_group_period_buckets(spec: AccountExplorerQuerySpec) -> dict[str, dict]:
	_opening, period = _aggregate_stock_buckets(
		spec,
		group_col="item_group",
		key_name="item_group",
		join_item=True,
	)
	return period


def get_item_stock_buckets(spec: AccountExplorerQuerySpec) -> tuple[dict[str, dict], dict[str, dict]]:
	"""Single-query opening + period for Item axis."""
	return _aggregate_stock_buckets(spec, group_col="item_code", key_name="item_code")


def get_item_group_stock_buckets(spec: AccountExplorerQuerySpec) -> tuple[dict[str, dict], dict[str, dict]]:
	"""Single-query opening + period for Item Group axis."""
	return _aggregate_stock_buckets(
		spec,
		group_col="item_group",
		key_name="item_group",
		join_item=True,
	)


def get_related_stock_accounts_for_items(spec: AccountExplorerQuerySpec, item_codes: list[str]) -> list[str]:
	"""Accounts that received GL postings from stock vouchers involving scoped items."""
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import InventoryScope

	return get_related_stock_accounts_for_scope(
		spec,
		InventoryScope(item_codes=frozenset(item_codes) if item_codes is not None else None),
	)


def get_related_stock_accounts_for_scope(spec: AccountExplorerQuerySpec, scope) -> list[str]:
	"""Warehouse inventory accounts for warehouses with scoped SLE activity.

	Uses ERPNext ``get_warehouse_account_map`` (Warehouse.account → parent → company
	default). Does **not** return every GL account on matching vouchers.
	"""
	from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
		resolve_scoped_inventory_accounts,
	)

	if not getattr(scope, "is_inventory_constrained", False):
		return []
	return resolve_scoped_inventory_accounts(spec)


def get_item_codes_for_item_groups(item_groups: list[str]) -> list[str]:
	if not item_groups:
		return []
	return frappe.get_all(
		"Item",
		filters={"item_group": ["in", item_groups], "is_stock_item": 1},
		pluck="name",
	)
