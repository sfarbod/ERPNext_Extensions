# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Persist calculation detail DocTypes for a single revision (bulk inserts)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, flt, now, now_datetime

from erpnext_extensions.iran_accounting.fg_cost_allocation import DETAIL_DOCTYPE_MAP
from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	BatchAllocationState,
	BatchExpenseAlloc,
	ProductExpenseAlloc,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import ResolvedPeriod
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket

DETAIL_TYPES = [
	DETAIL_DOCTYPE_MAP["product_expense"],
	DETAIL_DOCTYPE_MAP["batch_expense"],
	DETAIL_DOCTYPE_MAP["gl_line"],
	DETAIL_DOCTYPE_MAP["product"],
	DETAIL_DOCTYPE_MAP["batch"],
	DETAIL_DOCTYPE_MAP["expense_pool"],
]


def clear_details(monthly_name: str, revision: int | None = None) -> None:
	"""Bulk-delete detail rows. Uses raw DELETE to avoid QB lock churn on large sets."""
	for dt in DETAIL_TYPES:
		table = f"tab{dt}"
		if revision is not None:
			frappe.db.sql(
				f"DELETE FROM `{table}` WHERE monthly_calculation=%s AND revision=%s",
				(monthly_name, cint(revision)),
			)
		else:
			frappe.db.sql(
				f"DELETE FROM `{table}` WHERE monthly_calculation=%s",
				(monthly_name,),
			)


def _f(value) -> float:
	if value is None:
		return 0.0
	return float(value)


def _name() -> str:
	return frappe.generate_hash(length=10)


def _bulk(doctype: str, fields: list[str], rows: list[list]):
	if not rows:
		return
	# chunk to avoid max packet
	chunk = 500
	for i in range(0, len(rows), chunk):
		frappe.db.bulk_insert(doctype, fields=fields, values=rows[i : i + chunk])


def persist_all(
	*,
	monthly_name: str,
	revision: int,
	period: ResolvedPeriod,
	currency: str,
	pools: list[ExpensePoolSnapshot],
	batches: list[BatchAllocationState],
	batch_expenses: list[BatchExpenseAlloc],
	product_rows: list[tuple[BatchAllocationState, ProductBucket, Decimal, Decimal]],
	product_expenses: list[ProductExpenseAlloc],
) -> dict[str, dict]:
	rev = cint(revision)
	company = period.company
	user = frappe.session.user
	ts = now()
	pool_map: dict[tuple, str] = {}
	batch_map: dict[str, str] = {}
	product_map: dict[tuple[str, str], str] = {}

	# --- pools ---
	pool_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"company",
		"persian_year",
		"persian_month",
		"start_date",
		"end_date",
		"settings_rule",
		"account",
		"cost_center",
		"department",
		"monthly_debit",
		"monthly_credit",
		"monthly_net_expense",
		"gl_entry_count",
		"allocated_amount",
		"unallocated_amount",
		"currency",
	]
	pool_rows = []
	gl_rows = []
	gl_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"expense_pool",
		"revision",
		"company",
		"gl_entry",
		"voucher_type",
		"voucher_no",
		"posting_date",
		"account",
		"cost_center",
		"department",
		"debit",
		"credit",
		"net_amount",
		"remarks",
	]

	# precompute pool allocated from batch_expenses
	alloc_by_rule: dict[str, Decimal] = {}
	for bx in batch_expenses:
		alloc_by_rule[bx.settings_rule] = alloc_by_rule.get(bx.settings_rule, Decimal("0")) + bx.allocated_amount

	for idx, pool in enumerate(pools, start=1):
		pname = _name()
		key = (pool.account, pool.cost_center or "", pool.department or "", pool.settings_rule)
		pool_map[key] = pname
		pool_map[(pool.account, pool.cost_center or "", pool.department or "")] = pname
		allocated = alloc_by_rule.get(pool.settings_rule, Decimal("0"))
		pool_rows.append(
			[
				pname,
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				rev,
				company,
				period.persian_year,
				period.persian_month,
				period.start_date,
				period.end_date,
				pool.settings_rule,
				pool.account,
				pool.cost_center or None,
				pool.department or None,
				_f(pool.monthly_debit),
				_f(pool.monthly_credit),
				_f(pool.monthly_net_expense),
				pool.gl_entry_count,
				_f(allocated),
				_f(pool.monthly_net_expense - allocated),
				currency,
			]
		)
		for j, line in enumerate(pool.lines, start=1):
			gl_rows.append(
				[
					_name(),
					ts,
					ts,
					user,
					user,
					0,
					j,
					monthly_name,
					pname,
					rev,
					company,
					line.gl_entry,
					line.voucher_type,
					line.voucher_no,
					line.posting_date,
					line.account,
					line.cost_center or None,
					line.department or None,
					_f(line.debit),
					_f(line.credit),
					_f(line.net_amount),
					line.remarks,
				]
			)

	_bulk(DETAIL_DOCTYPE_MAP["expense_pool"], pool_fields, pool_rows)
	_bulk(DETAIL_DOCTYPE_MAP["gl_line"], gl_fields, gl_rows)

	# --- batches ---
	batch_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"company",
		"persian_year",
		"persian_month",
		"batch_no",
		"finished_item",
		"work_orders",
		"stock_entries",
		"stock_entry_details",
		"posting_date_min",
		"posting_date_max",
		"actual_qty",
		"uom",
		"normalization_factor",
		"normalized_qty",
		"total_monthly_normalized_qty",
		"calculated_share",
		"manual_share",
		"is_manual_override",
		"applied_share",
		"total_allocated_expense",
		"currency",
	]
	batch_rows = []
	for idx, batch in enumerate(batches, start=1):
		bname = _name()
		batch_map[batch.batch_no] = bname
		batch_rows.append(
			[
				bname,
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				rev,
				company,
				period.persian_year,
				period.persian_month,
				batch.batch_no,
				batch.finished_item,
				json.dumps(batch.work_orders, ensure_ascii=False),
				json.dumps(batch.stock_entries, ensure_ascii=False),
				json.dumps(batch.stock_entry_details, ensure_ascii=False),
				batch.posting_date_min,
				batch.posting_date_max,
				_f(batch.actual_qty),
				batch.uom,
				_f(batch.normalization_factor) if batch.normalization_factor is not None else 0.0,
				_f(batch.normalized_qty),
				_f(batch.total_monthly_normalized_qty),
				_f(batch.calculated_share),
				_f(batch.manual_share) if batch.manual_share is not None else 0.0,
				1 if batch.is_manual_override else 0,
				_f(batch.applied_share),
				_f(batch.total_allocated_expense),
				currency,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["batch"], batch_fields, batch_rows)

	# --- batch x expense ---
	bx_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"batch_allocation",
		"batch_no",
		"expense_pool",
		"account",
		"cost_center",
		"department",
		"pool_monthly_net_expense",
		"applied_batch_share",
		"allocated_amount",
		"currency",
		"residual_applied",
	]
	bx_rows = []
	for idx, bx in enumerate(batch_expenses, start=1):
		pool_name = pool_map.get((bx.account, bx.cost_center, bx.department, bx.settings_rule)) or pool_map.get(
			(bx.account, bx.cost_center, bx.department)
		)
		bx_rows.append(
			[
				_name(),
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				rev,
				batch_map[bx.batch_no],
				bx.batch_no,
				pool_name,
				bx.account,
				bx.cost_center or None,
				bx.department or None,
				_f(bx.pool_monthly_net_expense),
				_f(bx.applied_batch_share),
				_f(bx.allocated_amount),
				currency,
				1 if bx.residual_applied else 0,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["batch_expense"], bx_fields, bx_rows)

	# --- products ---
	prod_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"batch_allocation",
		"batch_no",
		"item_code",
		"item_name",
		"work_order",
		"stock_entry",
		"stock_entry_detail",
		"stock_entries",
		"actual_qty",
		"uom",
		"normalization_factor",
		"normalization_uom",
		"normalized_qty",
		"batch_total_normalized_qty",
		"product_share",
		"batch_allocated_expense",
		"product_allocated_expense",
		"unit_cost_on_actual",
		"unit_cost_on_normalized",
		"currency",
		"missing_uom_factor",
	]
	prod_rows = []
	for idx, (batch, product, product_share, product_allocated) in enumerate(product_rows, start=1):
		pname = _name()
		product_map[(batch.batch_no, product.item_code)] = pname
		unit_actual = (product_allocated / product.actual_qty) if product.actual_qty else Decimal("0")
		unit_norm = (product_allocated / product.normalized_qty) if product.normalized_qty else Decimal("0")
		prod_rows.append(
			[
				pname,
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				rev,
				batch_map[batch.batch_no],
				batch.batch_no,
				product.item_code,
				product.item_name,
				next(iter(sorted(product.work_orders)), None),
				next(iter(sorted(product.stock_entries)), None),
				next(iter(sorted(product.stock_entry_details)), None),
				json.dumps(sorted(product.stock_entries), ensure_ascii=False),
				_f(product.actual_qty),
				product.uom,
				_f(product.normalization_factor) if product.normalization_factor is not None else 0.0,
				product.normalization_uom,
				_f(product.normalized_qty),
				_f(batch.normalized_qty),
				_f(product_share),
				_f(batch.total_allocated_expense),
				_f(product_allocated),
				_f(unit_actual),
				_f(unit_norm),
				currency,
				1 if product.missing_uom_factor else 0,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["product"], prod_fields, prod_rows)

	# --- product x expense ---
	px_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"product_allocation",
		"batch_allocation",
		"batch_no",
		"item_code",
		"expense_pool",
		"account",
		"cost_center",
		"department",
		"pool_monthly_net_expense",
		"applied_batch_share",
		"product_share",
		"batch_pool_allocated_amount",
		"product_pool_allocated_amount",
		"currency",
		"residual_applied",
	]
	px_rows = []
	for idx, px in enumerate(product_expenses, start=1):
		pool_name = pool_map.get((px.account, px.cost_center, px.department, px.settings_rule)) or pool_map.get(
			(px.account, px.cost_center, px.department)
		)
		px_rows.append(
			[
				_name(),
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				rev,
				product_map[(px.batch_no, px.item_code)],
				batch_map[px.batch_no],
				px.batch_no,
				px.item_code,
				pool_name,
				px.account,
				px.cost_center or None,
				px.department or None,
				_f(px.pool_monthly_net_expense),
				_f(px.applied_batch_share),
				_f(px.product_share),
				_f(px.batch_pool_allocated_amount),
				_f(px.product_pool_allocated_amount),
				currency,
				1 if px.residual_applied else 0,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["product_expense"], px_fields, px_rows)

	return {"pools": pool_map, "batches": batch_map, "products": product_map}


def write_revision_log(
	*,
	monthly_name: str,
	revision: int,
	action: str,
	previous_status: str,
	new_status: str,
	source_fingerprint: str,
	totals: dict,
	override_map: dict,
	reconciliation: dict,
	retain_overrides: bool,
) -> None:
	frappe.get_doc(
		{
			"doctype": DETAIL_DOCTYPE_MAP["revision"],
			"monthly_calculation": monthly_name,
			"revision": revision,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"previous_status": previous_status,
			"new_status": new_status,
			"source_fingerprint": source_fingerprint,
			"totals_json": json.dumps(totals, ensure_ascii=False, default=str),
			"override_map_json": json.dumps(override_map, ensure_ascii=False, default=str),
			"reconciliation_json": json.dumps(reconciliation, ensure_ascii=False, default=str),
			"retain_overrides": 1 if retain_overrides else 0,
		}
	).insert(ignore_permissions=True)
