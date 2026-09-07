# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Voucher-axis summary with SQL pagination (Phase 4).

Phase 3 root cause
------------------
``build_voucher_summary`` previously:

1. Aggregated *all* vouchers under analysis scope (scoped pass).
2. Aggregated *all* vouchers under document scope again (full pass).
3. Enriched parties / metadata for every voucher.
4. Sorted / sliced in Python.

At ~500k vouchers that meant two full GROUP BY scans plus multi-hundred-MB
Python structures (P95 ≈ 23.6s, peak ≈ 523 MB).

Phase 4 design (semantics unchanged)
------------------------------------
- Interactive page: **one** scoped GROUP BY (CTE) + window totals + LIMIT/OFFSET.
- Full-voucher debit/credit fetched only for the current page keys.
- Party / title enrichment runs only on the page.
- Export: one TEMPORARY TABLE materialization of the grouped set, then LIMIT pages
  (constant accounting-query shape; no per-page re-aggregation of GL).

Architectural floor (no new indexes this phase)
-----------------------------------------------
MariaDB must still scan/group matching GL rows once. At ~500k voucher groups on
this host that GROUP BY alone is ≈ 10–13s. Phase 4 removes the *second* full
aggregate and the Python OOM path; sub-3s at 500k requires index/schema work
deferred per Phase 3 decision tree.
"""

from __future__ import annotations

import frappe
from frappe.query_builder import Order
from frappe.query_builder.functions import Count, Min, Sum
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.account_explorer.constants import VOUCHER_SORTABLE_FIELDS
from erpnext_extensions.iran_accounting.account_explorer.gle_filters import (
	apply_document_scope_filters,
	apply_opening_entry_filters,
	apply_scoped_gle_filters,
	collect_scope_warnings,
)
from erpnext_extensions.iran_accounting.account_explorer.party_sources import resolve_party_display_name
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.voucher_metadata import enrich_voucher_rows

# Enrichment-only display fields map to stable SQL keys so page boundaries stay
# deterministic without materializing the full set.
_SQL_SORT_FIELD_MAP = {
	"posting_date": "posting_date",
	"voucher_type": "voucher_type",
	"voucher_no": "voucher_no",
	"party_type": "voucher_type",
	"party": "voucher_no",
	"party_name": "voucher_no",
	"voucher_title": "voucher_no",
	"scoped_debit": "scoped_debit",
	"scoped_credit": "scoped_credit",
	"scoped_net": "scoped_net",
	"full_voucher_debit": "scoped_debit",
	"full_voucher_credit": "scoped_credit",
}


def build_voucher_summary(
	spec: AccountExplorerQuerySpec,
	*,
	_totals: dict | None = None,
	_total_rows: int | None = None,
	_source_table: str | None = None,
) -> dict:
	"""Return one page of voucher summary rows with full-set totals."""
	page = max(cint(spec.pagination.page) or 1, 1)
	page_size = max(cint(spec.pagination.page_size) or 50, 1)
	offset = (page - 1) * page_size

	if _source_table:
		page_aggregates = _page_from_temp_table(_source_table, spec, limit=page_size, offset=offset)
		if _totals is None or _total_rows is None:
			totals, total_rows = _totals_from_temp_table(_source_table)
		else:
			totals, total_rows = _totals, _total_rows
	elif _totals is None or _total_rows is None:
		page_aggregates, totals, total_rows = _scoped_voucher_page_with_totals(
			spec, limit=page_size, offset=offset
		)
	else:
		page_aggregates = _scoped_voucher_page(spec, limit=page_size, offset=offset)
		totals, total_rows = _totals, _total_rows

	full_map = _full_voucher_amounts_for_keys(spec, page_aggregates)
	_enrich_party_fields(spec, page_aggregates, scoped=True)

	rows: list[dict] = []
	for row in page_aggregates:
		voucher_type = row.get("voucher_type")
		voucher_no = row.get("voucher_no")
		if not voucher_type or not voucher_no:
			continue
		scoped_debit = flt(row.get("scoped_debit"))
		scoped_credit = flt(row.get("scoped_credit"))
		full_row = full_map.get((voucher_type, voucher_no), {})
		party_type = row.get("party_type") or ""
		party = row.get("party") or ""
		rows.append(
			{
				"row_key": f"voucher:{voucher_type}:{voucher_no}",
				"posting_date": str(row.get("posting_date") or ""),
				"voucher_type": voucher_type,
				"voucher_no": voucher_no,
				"party_type": party_type,
				"party": party,
				"party_name": _party_name(party_type, party),
				"voucher_title": voucher_no,
				"reference": None,
				"scoped_debit": scoped_debit,
				"scoped_credit": scoped_credit,
				"scoped_net": scoped_debit - scoped_credit,
				"full_voucher_debit": flt(full_row.get("scoped_debit")),
				"full_voucher_credit": flt(full_row.get("scoped_credit")),
				"has_multiple_parties": int(flt(row.get("party_count") or 0) > 1),
				"is_virtual_group": 0,
				"drill_down_enabled": 1,
			}
		)

	enrich_voucher_rows(rows)

	return {
		"rows": rows,
		"totals": totals,
		"pagination": {
			"page": page,
			"page_size": page_size,
			"total_rows": total_rows,
			"has_next": offset + page_size < total_rows,
		},
		"warnings": collect_scope_warnings(spec),
	}


def iter_voucher_summary_pages(spec: AccountExplorerQuerySpec, *, page_size: int = 500):
	"""Yield successive voucher pages for export.

	Materializes the scoped GROUP BY once into a TEMPORARY TABLE, then pages
	with LIMIT/OFFSET so GL is not re-aggregated per export page.

	Note: UI enrichment path — not for bulk CSV/XLSX export. Use
	``iter_voucher_export_batches`` for large exports (no OFFSET N+1, no metadata).
	"""
	page_size = max(cint(page_size) or 500, 1)
	table = _create_scoped_voucher_temp_table(spec)
	try:
		totals, total_rows = _totals_from_temp_table(table)
		if total_rows == 0:
			yield {
				"rows": [],
				"totals": totals,
				"pagination": {"page": 1, "page_size": page_size, "total_rows": 0, "has_next": False},
				"warnings": collect_scope_warnings(spec),
			}
			return

		original_page = spec.pagination.page
		original_size = spec.pagination.page_size
		page = 1
		try:
			while True:
				offset = (page - 1) * page_size
				spec.pagination.page = page
				spec.pagination.page_size = page_size
				yield build_voucher_summary(
					spec,
					_totals=totals,
					_total_rows=total_rows,
					_source_table=table,
				)
				if offset + page_size >= total_rows:
					break
				page += 1
		finally:
			spec.pagination.page = original_page
			spec.pagination.page_size = original_size
	finally:
		frappe.db.sql(f"drop temporary table if exists `{table}`")


def iter_voucher_export_batches(spec: AccountExplorerQuerySpec, *, batch_size: int = 10000):
	"""Lean bulk-export iterator: one GROUP BY temp table, keyset batches, no UI enrichment.

	Yields ``(tuple_rows, totals, total_rows)`` where each tuple is::

	    (posting_date, voucher_type, voucher_no, scoped_debit, scoped_credit)

	Export columns only — no party / voucher-title / full-voucher lookups.
	Uses keyset pagination (not OFFSET) so SQL count stays O(batches), not O(rows).
	"""
	batch_size = max(cint(batch_size) or 10000, 1)
	table = _create_scoped_voucher_temp_table(spec)
	try:
		totals, total_rows = _totals_from_temp_table(table)
		if total_rows == 0:
			yield (), totals, 0
			return

		# Fixed export order — independent of UI sort (stable, keyset-friendly).
		last = None
		while True:
			if last is None:
				rows = frappe.db.sql(
					f"""
					select posting_date, voucher_type, voucher_no, scoped_debit, scoped_credit
					from `{table}`
					order by posting_date asc, voucher_type asc, voucher_no asc
					limit {batch_size}
					"""
				)
			else:
				rows = frappe.db.sql(
					f"""
					select posting_date, voucher_type, voucher_no, scoped_debit, scoped_credit
					from `{table}`
					where (posting_date, voucher_type, voucher_no) > (%s, %s, %s)
					order by posting_date asc, voucher_type asc, voucher_no asc
					limit {batch_size}
					""",
					last,
				)
			if not rows:
				break
			yield rows, totals, total_rows
			tail = rows[-1]
			last = (tail[0], tail[1], tail[2])
			if len(rows) < batch_size:
				break
	finally:
		frappe.db.sql(f"drop temporary table if exists `{table}`")


def _scoped_voucher_totals(spec: AccountExplorerQuerySpec) -> tuple[dict, int]:
	"""Public totals helper (export size probe). Prefer page-with-totals for UI."""
	_page, totals, total_rows = _scoped_voucher_page_with_totals(spec, limit=1, offset=0)
	return totals, total_rows


def _scoped_voucher_page_with_totals(
	spec: AccountExplorerQuerySpec, *, limit: int, offset: int
) -> tuple[list[dict], dict, int]:
	"""One CTE GROUP BY + window totals + LIMIT/OFFSET (interactive path)."""
	gle = frappe.qb.DocType("GL Entry")
	inner = _scoped_group_query(spec, gle)
	inner_sql = inner.get_sql()
	order_sql = _order_by_sql_for_alias(spec)
	rows = frappe.db.sql(
		f"""
		with voucher_groups as (
			{inner_sql}
		)
		select
			voucher_type,
			voucher_no,
			posting_date,
			scoped_debit,
			scoped_credit,
			count(*) over() as _total_rows,
			coalesce(sum(scoped_debit) over(), 0) as _total_debit,
			coalesce(sum(scoped_credit) over(), 0) as _total_credit
		from voucher_groups
		order by {order_sql}
		limit {cint(limit)} offset {cint(offset)}
		""",
		as_dict=True,
	)
	if not rows:
		# Empty page — still need totals when offset past end is rare; recompute cheaply.
		totals_row = frappe.db.sql(
			f"""
			select
				count(*) as total_rows,
				coalesce(sum(scoped_debit), 0) as scoped_debit,
				coalesce(sum(scoped_credit), 0) as scoped_credit
			from ({inner_sql}) as voucher_groups
			""",
			as_dict=True,
		)[0]
		scoped_debit = flt(totals_row.scoped_debit)
		scoped_credit = flt(totals_row.scoped_credit)
		return (
			[],
			{
				"scoped_debit": scoped_debit,
				"scoped_credit": scoped_credit,
				"scoped_net": scoped_debit - scoped_credit,
			},
			cint(totals_row.total_rows),
		)

	total_rows = cint(rows[0]._total_rows)
	scoped_debit = flt(rows[0]._total_debit)
	scoped_credit = flt(rows[0]._total_credit)
	totals = {
		"scoped_debit": scoped_debit,
		"scoped_credit": scoped_credit,
		"scoped_net": scoped_debit - scoped_credit,
	}
	for row in rows:
		row.pop("_total_rows", None)
		row.pop("_total_debit", None)
		row.pop("_total_credit", None)
	return rows, totals, total_rows


def _scoped_voucher_page(spec: AccountExplorerQuerySpec, *, limit: int, offset: int) -> list[dict]:
	gle = frappe.qb.DocType("GL Entry")
	query = _scoped_group_query(spec, gle)
	query = _apply_sql_order(query, gle, spec)
	return query.limit(limit).offset(offset).run(as_dict=True)


def _scoped_group_query(spec: AccountExplorerQuerySpec, gle):
	query = frappe.qb.from_(gle).select(
		gle.voucher_type,
		gle.voucher_no,
		Min(gle.posting_date).as_("posting_date"),
		Sum(gle.debit).as_("scoped_debit"),
		Sum(gle.credit).as_("scoped_credit"),
	)
	query = apply_scoped_gle_filters(query, gle, spec)
	query = (
		query.where(gle.voucher_type.isnotnull())
		.where(gle.voucher_no.isnotnull())
		.where(gle.voucher_no != "")
		.where(gle.posting_date >= spec.from_date)
		.where(gle.posting_date <= spec.to_date)
	)
	query = apply_opening_entry_filters(query, gle, spec)
	query = query.groupby(gle.company, gle.voucher_type, gle.voucher_no)
	if spec.hide_zero_rows:
		query = query.having((Sum(gle.debit) != 0) | (Sum(gle.credit) != 0))
	return query


def _order_by_sql_for_alias(spec: AccountExplorerQuerySpec) -> str:
	requested = spec.pagination.sort_field
	if requested not in VOUCHER_SORTABLE_FIELDS:
		requested = "posting_date"
	sql_field = _SQL_SORT_FIELD_MAP.get(requested, "posting_date")
	descending = (spec.pagination.sort_order or "asc").lower() == "desc"
	direction = "desc" if descending else "asc"
	primary = {
		"posting_date": "posting_date",
		"voucher_type": "voucher_type",
		"voucher_no": "voucher_no",
		"scoped_debit": "scoped_debit",
		"scoped_credit": "scoped_credit",
		"scoped_net": "(scoped_debit - scoped_credit)",
	}.get(sql_field, "posting_date")
	return f"{primary} {direction}, voucher_type asc, voucher_no asc"


def _apply_sql_order(query, gle, spec: AccountExplorerQuerySpec):
	requested = spec.pagination.sort_field
	if requested not in VOUCHER_SORTABLE_FIELDS:
		requested = "posting_date"
	sql_field = _SQL_SORT_FIELD_MAP.get(requested, "posting_date")
	descending = (spec.pagination.sort_order or "asc").lower() == "desc"
	order = Order.desc if descending else Order.asc

	order_term = {
		"posting_date": Min(gle.posting_date),
		"voucher_type": gle.voucher_type,
		"voucher_no": gle.voucher_no,
		"scoped_debit": Sum(gle.debit),
		"scoped_credit": Sum(gle.credit),
		"scoped_net": Sum(gle.debit) - Sum(gle.credit),
	}.get(sql_field, Min(gle.posting_date))

	query = query.orderby(order_term, order=order)
	query = query.orderby(gle.voucher_type, order=Order.asc)
	query = query.orderby(gle.voucher_no, order=Order.asc)
	return query


def _create_scoped_voucher_temp_table(spec: AccountExplorerQuerySpec) -> str:
	gle = frappe.qb.DocType("GL Entry")
	inner = _scoped_group_query(spec, gle)
	table = f"ae_voucher_{frappe.generate_hash(length=10)}"
	frappe.db.sql(f"create temporary table `{table}` as {inner.get_sql()}")
	frappe.db.sql(
		f"alter table `{table}` add index ae_voucher_sort (posting_date, voucher_type, voucher_no)"
	)
	return table


def _totals_from_temp_table(table: str) -> tuple[dict, int]:
	row = frappe.db.sql(
		f"""
		select
			count(*) as total_rows,
			coalesce(sum(scoped_debit), 0) as scoped_debit,
			coalesce(sum(scoped_credit), 0) as scoped_credit
		from `{table}`
		""",
		as_dict=True,
	)[0]
	scoped_debit = flt(row.scoped_debit)
	scoped_credit = flt(row.scoped_credit)
	return (
		{
			"scoped_debit": scoped_debit,
			"scoped_credit": scoped_credit,
			"scoped_net": scoped_debit - scoped_credit,
		},
		cint(row.total_rows),
	)


def _page_from_temp_table(
	table: str, spec: AccountExplorerQuerySpec, *, limit: int, offset: int
) -> list[dict]:
	order_sql = _order_by_sql_for_alias(spec)
	return frappe.db.sql(
		f"""
		select voucher_type, voucher_no, posting_date, scoped_debit, scoped_credit
		from `{table}`
		order by {order_sql}
		limit {cint(limit)} offset {cint(offset)}
		""",
		as_dict=True,
	)


def _full_voucher_amounts_for_keys(
	spec: AccountExplorerQuerySpec, rows: list[dict]
) -> dict[tuple[str, str], dict]:
	"""Document-scope debit/credit for the current page only."""
	if not rows:
		return {}
	keys = [(row.get("voucher_type"), row.get("voucher_no")) for row in rows if row.get("voucher_no")]
	if not keys:
		return {}

	gle = frappe.qb.DocType("GL Entry")
	query = frappe.qb.from_(gle).select(
		gle.voucher_type,
		gle.voucher_no,
		Sum(gle.debit).as_("scoped_debit"),
		Sum(gle.credit).as_("scoped_credit"),
	)
	query = apply_document_scope_filters(query, gle, spec)
	query = (
		query.where(gle.voucher_type.isnotnull())
		.where(gle.voucher_no.isnotnull())
		.where(gle.voucher_no != "")
		.where(gle.posting_date >= spec.from_date)
		.where(gle.posting_date <= spec.to_date)
	)
	query = apply_opening_entry_filters(query, gle, spec)

	voucher_nos = sorted({key[1] for key in keys if key[1]})
	voucher_types = sorted({key[0] for key in keys if key[0]})
	if voucher_nos:
		query = query.where(gle.voucher_no.isin(voucher_nos))
	if voucher_types:
		query = query.where(gle.voucher_type.isin(voucher_types))
	query = query.groupby(gle.company, gle.voucher_type, gle.voucher_no)

	wanted = set(keys)
	return {
		(row.voucher_type, row.voucher_no): row
		for row in query.run(as_dict=True)
		if (row.voucher_type, row.voucher_no) in wanted
	}


def _enrich_party_fields(spec: AccountExplorerQuerySpec, rows: list[dict], *, scoped: bool) -> None:
	if not rows:
		return
	voucher_nos = sorted({row.get("voucher_no") for row in rows if row.get("voucher_no")})
	voucher_types = sorted({row.get("voucher_type") for row in rows if row.get("voucher_type")})
	if not voucher_nos:
		return

	gle = frappe.qb.DocType("GL Entry")
	query = frappe.qb.from_(gle).select(
		gle.voucher_type,
		gle.voucher_no,
		gle.party_type,
		gle.party,
		Count(gle.name).as_("line_count"),
	)
	if scoped:
		query = apply_scoped_gle_filters(query, gle, spec)
	else:
		query = apply_document_scope_filters(query, gle, spec)
	query = (
		query.where(gle.party.isnotnull())
		.where(gle.party != "")
		.where(gle.posting_date >= spec.from_date)
		.where(gle.posting_date <= spec.to_date)
		.where(gle.voucher_no.isin(voucher_nos))
	)
	if voucher_types:
		query = query.where(gle.voucher_type.isin(voucher_types))
	query = apply_opening_entry_filters(query, gle, spec)
	query = query.groupby(gle.voucher_type, gle.voucher_no, gle.party_type, gle.party)

	party_rows = query.run(as_dict=True)
	party_map: dict[tuple[str, str], dict] = {}
	for row in party_rows:
		key = (row.voucher_type, row.voucher_no)
		entry = party_map.setdefault(key, {"parties": set(), "party_type": row.party_type, "party": row.party})
		entry["parties"].add((row.party_type, row.party))

	for row in rows:
		key = (row.get("voucher_type"), row.get("voucher_no"))
		party_info = party_map.get(key, {})
		parties = party_info.get("parties") or set()
		row["party_count"] = len(parties)
		row["party_type"] = party_info.get("party_type") or ""
		row["party"] = party_info.get("party") or ""


def _party_name(party_type: str, party: str) -> str:
	"""Resolve party display name without querying missing columns."""
	if not party_type or not party:
		return ""
	cache_key = f"{party_type}:{party}"
	cache = getattr(_party_name, "_cache", None)
	if cache is None:
		cache = {}
		_party_name._cache = cache
	cached = cache.get(cache_key)
	if cached is not None:
		return cached
	value = resolve_party_display_name(party_type, party) or party
	cache[cache_key] = value
	return value
