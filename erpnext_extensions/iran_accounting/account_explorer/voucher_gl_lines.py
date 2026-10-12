# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Voucher axis — one grid row per posted GL Entry (line-level)."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.utils import cint, cstr, flt, getdate
from pypika import Order

from erpnext_extensions.iran_accounting.account_explorer.constants import VOUCHER_SORTABLE_FIELDS
from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import (
	filter_usable_gl_dimensions,
	get_discovered_dimensions,
)
from erpnext_extensions.iran_accounting.account_explorer.gle_filters import (
	apply_opening_entry_filters,
	apply_scoped_gle_filters,
	collect_scope_warnings,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import _party_name

_SQL_SORT_MAP = {
	"posting_date": "gle.posting_date",
	"voucher_type": "gle.voucher_type",
	"voucher_no": "gle.voucher_no",
	"account": "gle.account",
	"account_code": "acc.account_number",
	"account_name": "acc.account_name",
	"party": "gle.party",
	"party_type": "gle.party_type",
	"debit": "gle.debit",
	"credit": "gle.credit",
	"gl_entry": "gle.name",
	"remarks": "gle.remarks",
}


def build_voucher_gl_line_summary(spec: AccountExplorerQuerySpec) -> dict:
	"""Return one SQL-paginated page of GL Entry lines for the Voucher axis."""
	page = max(cint(spec.pagination.page) or 1, 1)
	page_size = max(cint(spec.pagination.page_size) or 50, 1)
	offset = (page - 1) * page_size
	dimensions = filter_usable_gl_dimensions(get_discovered_dimensions())

	rows, totals, total_rows = _paged_gl_lines(spec, dimensions, limit=page_size, offset=offset)
	_attach_inventory_attribution(spec, rows)

	return {
		"rows": rows,
		"totals": totals,
		"pagination": {
			"page": page,
			"page_size": page_size,
			"total_rows": total_rows,
			"has_next": offset + page_size < total_rows,
		},
		"dimensions": [
			{
				"fieldname": dimension["fieldname"],
				"label": dimension["label"],
				"label_fa": dimension.get("label_fa"),
				"document_type": dimension.get("document_type"),
				"is_native": dimension.get("is_native", 0),
			}
			for dimension in dimensions
		],
		"warnings": collect_scope_warnings(spec),
		"voucher_view_mode": "gl_line",
	}


def iter_voucher_gl_line_export_batches(spec: AccountExplorerQuerySpec, *, batch_size: int = 5000):
	"""Yield lean GL-line tuples for streaming export (keyset on gle.name)."""
	batch_size = max(cint(batch_size) or 5000, 1)
	dimensions = filter_usable_gl_dimensions(get_discovered_dimensions())
	totals, total_rows = _gl_line_totals(spec)
	if total_rows == 0:
		yield (), totals, 0
		return

	last_name = None
	while True:
		batch = _export_batch(spec, dimensions, batch_size=batch_size, after_name=last_name)
		if not batch:
			break
		yield batch, totals, total_rows
		last_name = batch[-1][0]  # gle.name
		if len(batch) < batch_size:
			break


def _paged_gl_lines(
	spec: AccountExplorerQuerySpec,
	dimensions: list[dict],
	*,
	limit: int,
	offset: int,
) -> tuple[list[dict], dict, int]:
	gle = frappe.qb.DocType("GL Entry")
	acc = frappe.qb.DocType("Account")
	inner = _base_line_query(spec, gle, acc, dimensions)
	inner_sql = inner.get_sql()
	order_sql = _order_by_sql(spec)
	dim_select = "".join(f", gle.`{d['fieldname']}`" for d in dimensions)
	rows = frappe.db.sql(
		f"""
		with gle_lines as (
			{inner_sql}
		)
		select
			gle_lines.*,
			count(*) over() as _total_rows,
			coalesce(sum(debit) over(), 0) as _total_debit,
			coalesce(sum(credit) over(), 0) as _total_credit
		from gle_lines
		order by {order_sql}
		limit {cint(limit)} offset {cint(offset)}
		""",
		as_dict=True,
	)
	if not rows:
		totals, total_rows = _gl_line_totals(spec)
		return [], totals, total_rows

	total_rows = cint(rows[0]._total_rows)
	total_debit = Decimal(str(rows[0]._total_debit or 0))
	total_credit = Decimal(str(rows[0]._total_credit or 0))
	totals = {
		"debit": float(total_debit),
		"credit": float(total_credit),
		"net": float(total_debit - total_credit),
		# Backward-compatible aliases for older UI bindings.
		"scoped_debit": float(total_debit),
		"scoped_credit": float(total_credit),
		"scoped_net": float(total_debit - total_credit),
	}
	out = [_row_dict(row, dimensions) for row in rows]
	return out, totals, total_rows


def _gl_line_totals(spec: AccountExplorerQuerySpec) -> tuple[dict, int]:
	gle = frappe.qb.DocType("GL Entry")
	acc = frappe.qb.DocType("Account")
	dimensions = filter_usable_gl_dimensions(get_discovered_dimensions())
	inner = _base_line_query(spec, gle, acc, dimensions)
	inner_sql = inner.get_sql()
	row = frappe.db.sql(
		f"""
		select
			count(*) as total_rows,
			coalesce(sum(debit), 0) as total_debit,
			coalesce(sum(credit), 0) as total_credit
		from (
			{inner_sql}
		) gle_lines
		""",
		as_dict=True,
	)
	if not row:
		return {"debit": 0.0, "credit": 0.0, "net": 0.0, "scoped_debit": 0.0, "scoped_credit": 0.0, "scoped_net": 0.0}, 0
	debit = Decimal(str(row[0].total_debit or 0))
	credit = Decimal(str(row[0].total_credit or 0))
	totals = {
		"debit": float(debit),
		"credit": float(credit),
		"net": float(debit - credit),
		"scoped_debit": float(debit),
		"scoped_credit": float(credit),
		"scoped_net": float(debit - credit),
	}
	return totals, cint(row[0].total_rows)


def _base_line_query(spec: AccountExplorerQuerySpec, gle, acc, dimensions: list[dict]):
	select_fields = [
		gle.name.as_("gl_entry"),
		gle.posting_date,
		gle.voucher_type,
		gle.voucher_no,
		gle.voucher_detail_no,
		gle.account,
		acc.account_number.as_("account_code"),
		acc.account_name,
		gle.against.as_("against_account"),
		gle.party_type,
		gle.party,
		gle.debit,
		gle.credit,
		gle.account_currency,
		gle.debit_in_account_currency,
		gle.credit_in_account_currency,
		gle.finance_book,
		gle.is_opening,
		gle.remarks,
		gle.against_voucher_type,
		gle.against_voucher,
		gle.cost_center,
		gle.project,
	]
	native_fields = {
		"cost_center",
		"project",
		"account",
		"party",
		"party_type",
		"finance_book",
		"against",
		"remarks",
	}
	for dimension in dimensions:
		fieldname = dimension["fieldname"]
		if fieldname in native_fields:
			continue
		select_fields.append(gle[fieldname])

	query = (
		frappe.qb.from_(gle)
		.left_join(acc)
		.on(gle.account == acc.name)
		.select(*select_fields)
	)
	query = apply_scoped_gle_filters(query, gle, spec)
	query = (
		query.where(gle.voucher_type.isnotnull())
		.where(gle.voucher_no.isnotnull())
		.where(gle.voucher_no != "")
		.where(gle.posting_date >= getdate(spec.from_date))
		.where(gle.posting_date <= getdate(spec.to_date))
	)
	query = apply_opening_entry_filters(query, gle, spec)
	if spec.hide_zero_rows:
		query = query.where((gle.debit != 0) | (gle.credit != 0))
	return query


def _order_by_sql(spec: AccountExplorerQuerySpec) -> str:
	requested = spec.pagination.sort_field
	if requested not in VOUCHER_SORTABLE_FIELDS and requested not in _SQL_SORT_MAP:
		requested = "posting_date"
	primary = _SQL_SORT_MAP.get(requested, "gle.posting_date")
	# Window query aliases drop table prefixes from qb select aliases.
	primary = primary.replace("gle.", "").replace("acc.", "")
	direction = "desc" if (spec.pagination.sort_order or "asc").lower() == "desc" else "asc"
	return f"{primary} {direction}, voucher_type asc, voucher_no asc, gl_entry asc"


def _row_dict(row, dimensions: list[dict]) -> dict:
	party_type = row.get("party_type") or ""
	party = row.get("party") or ""
	debit = flt(row.get("debit"))
	credit = flt(row.get("credit"))
	account_code = cstr(row.get("account_code") or "")
	entry = {
		"row_key": f"glentry:{row.get('gl_entry')}",
		"gl_entry": row.get("gl_entry"),
		"posting_date": str(row.get("posting_date") or ""),
		"voucher_type": row.get("voucher_type"),
		"voucher_no": row.get("voucher_no"),
		"voucher_detail_no": row.get("voucher_detail_no") or "",
		"account": row.get("account"),
		"account_code": account_code,
		"account_name": row.get("account_name") or "",
		"against_account": row.get("against_account") or "",
		"party_type": party_type,
		"party": party,
		"party_name": _party_name(party_type, party),
		"debit": debit,
		"credit": credit,
		"account_currency": row.get("account_currency") or "",
		"currency": row.get("account_currency") or "",
		"debit_in_account_currency": flt(row.get("debit_in_account_currency")),
		"credit_in_account_currency": flt(row.get("credit_in_account_currency")),
		"finance_book": row.get("finance_book") or "",
		"is_opening": row.get("is_opening") or "No",
		"remarks": row.get("remarks") or "",
		"description": row.get("remarks") or "",
		"against_voucher_type": row.get("against_voucher_type") or "",
		"against_voucher": row.get("against_voucher") or "",
		"cost_center": row.get("cost_center") or "",
		"project": row.get("project") or "",
		"reference": "",
		"item_code": "",
		"item_name": "",
		"item_group": "",
		"warehouse": "",
		"batch_no": "",
		"inventory_attribution": "",
		"is_virtual_group": 0,
		"drill_down_enabled": 0,
		# Legacy aliases so older UI actions still resolve voucher identity.
		"scoped_debit": debit,
		"scoped_credit": credit,
		"scoped_net": debit - credit,
		"voucher_title": row.get("voucher_no"),
	}
	for dimension in dimensions:
		fieldname = dimension["fieldname"]
		value = row.get(fieldname) or ""
		entry[fieldname] = value
		entry[f"dim:{fieldname}"] = value
	for key in ("_total_rows", "_total_debit", "_total_credit"):
		entry.pop(key, None)
	return entry


def _export_batch(
	spec: AccountExplorerQuerySpec,
	dimensions: list[dict],
	*,
	batch_size: int,
	after_name: str | None,
) -> list[tuple]:
	gle = frappe.qb.DocType("GL Entry")
	acc = frappe.qb.DocType("Account")
	query = _base_line_query(spec, gle, acc, dimensions)
	query = query.orderby(gle.name, order=Order.asc).limit(batch_size)
	if after_name:
		query = query.where(gle.name > after_name)
	raw = query.run(as_dict=True)
	out = []
	for row in raw:
		out.append(
			(
				row.get("gl_entry") or row.get("name"),
				row.get("posting_date"),
				row.get("voucher_type"),
				row.get("voucher_no"),
				row.get("account_code"),
				row.get("account_name"),
				row.get("debit"),
				row.get("credit"),
				row.get("account_currency"),
				row.get("remarks"),
				row.get("party"),
				row.get("cost_center"),
				row.get("account"),
			)
		)
	return out


def _attach_inventory_attribution(spec: AccountExplorerQuerySpec, rows: list[dict]) -> None:
	"""Fill Item/Warehouse only when voucher_detail_no maps to exactly one SLE."""
	if not rows:
		return
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		has_inventory_document_filters,
	)

	detail_keys = {
		(row.get("voucher_type"), row.get("voucher_no"), row.get("voucher_detail_no"))
		for row in rows
		if row.get("voucher_detail_no")
	}
	if not detail_keys:
		if has_inventory_document_filters(spec):
			for row in rows:
				row["inventory_attribution"] = "Not directly attributable"
		return

	# Bulk SLE lookup by voucher_detail_no
	detail_nos = list({key[2] for key in detail_keys if key[2]})
	if not detail_nos:
		return
	sle_rows = frappe.db.sql(
		"""
		select voucher_type, voucher_no, voucher_detail_no, item_code, warehouse, batch_no
		from `tabStock Ledger Entry`
		where company=%s and is_cancelled=0 and voucher_detail_no in %s
		""",
		(spec.company, detail_nos),
		as_dict=True,
	)
	grouped: dict[tuple, list] = {}
	for sle in sle_rows:
		key = (sle.voucher_type, sle.voucher_no, sle.voucher_detail_no)
		grouped.setdefault(key, []).append(sle)

	item_codes = {sle.item_code for sle in sle_rows if sle.item_code}
	item_meta = {}
	if item_codes:
		item_meta = {
			row.name: row
			for row in frappe.get_all(
				"Item",
				filters={"name": ["in", list(item_codes)]},
				fields=["name", "item_name", "item_group"],
			)
		}

	for row in rows:
		key = (row.get("voucher_type"), row.get("voucher_no"), row.get("voucher_detail_no"))
		matches = grouped.get(key) or []
		if len(matches) == 1:
			sle = matches[0]
			item = item_meta.get(sle.item_code)
			row["item_code"] = sle.item_code or ""
			row["item_name"] = (item.item_name if item else "") or ""
			row["item_group"] = (item.item_group if item else "") or ""
			row["warehouse"] = sle.warehouse or ""
			row["batch_no"] = sle.batch_no or ""
			row["inventory_attribution"] = "Direct"
		elif len(matches) > 1:
			row["inventory_attribution"] = "Multiple / Not directly attributable"
		elif has_inventory_document_filters(spec):
			row["inventory_attribution"] = "Not directly attributable"
