# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Persist calculation detail DocTypes for a single revision."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

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
	"""Delete persisted detail rows for a monthly calculation (optionally one revision)."""
	for dt in DETAIL_TYPES:
		filters: dict[str, Any] = {"monthly_calculation": monthly_name}
		if revision is not None:
			filters["revision"] = cint(revision)
		# Bulk delete avoids per-document queue overload on large months
		frappe.db.delete(dt, filters)


def _f(value) -> float:
	if value is None:
		return 0.0
	return float(value)


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
) -> dict[str, dict[tuple, str]]:
	"""Insert all detail docs. Returns maps of natural keys → docname."""
	pool_map: dict[tuple, str] = {}
	batch_map: dict[str, str] = {}
	product_map: dict[tuple[str, str], str] = {}

	common = {
		"monthly_calculation": monthly_name,
		"revision": cint(revision),
		"company": period.company,
		"doctype": None,
	}

	for pool in pools:
		doc = frappe.get_doc(
			{
				"doctype": DETAIL_DOCTYPE_MAP["expense_pool"],
				"monthly_calculation": monthly_name,
				"revision": revision,
				"company": period.company,
				"persian_year": period.persian_year,
				"persian_month": period.persian_month,
				"start_date": period.start_date,
				"end_date": period.end_date,
				"settings_rule": pool.settings_rule,
				"account": pool.account,
				"cost_center": pool.cost_center or None,
				"department": pool.department or None,
				"monthly_debit": _f(pool.monthly_debit),
				"monthly_credit": _f(pool.monthly_credit),
				"monthly_net_expense": _f(pool.monthly_net_expense),
				"gl_entry_count": pool.gl_entry_count,
				"allocated_amount": 0,
				"unallocated_amount": 0,
				"currency": currency,
			}
		)
		doc.insert(ignore_permissions=True)
		key = (pool.account, pool.cost_center or "", pool.department or "", pool.settings_rule)
		pool_map[key] = doc.name
		pool_map[(pool.account, pool.cost_center or "", pool.department or "")] = doc.name

		for line in pool.lines:
			frappe.get_doc(
				{
					"doctype": DETAIL_DOCTYPE_MAP["gl_line"],
					"monthly_calculation": monthly_name,
					"expense_pool": doc.name,
					"revision": revision,
					"company": period.company,
					"gl_entry": line.gl_entry,
					"voucher_type": line.voucher_type,
					"voucher_no": line.voucher_no,
					"posting_date": line.posting_date,
					"account": line.account,
					"cost_center": line.cost_center or None,
					"department": line.department or None,
					"debit": _f(line.debit),
					"credit": _f(line.credit),
					"net_amount": _f(line.net_amount),
					"remarks": line.remarks,
				}
			).insert(ignore_permissions=True)

	# Batch allocated totals by pool for pool.allocated_amount
	pool_allocated: dict[str, Decimal] = {}
	for bx in batch_expenses:
		pool_name = pool_map.get(bx.pool_key) or pool_map.get(
			(bx.account, bx.cost_center, bx.department, bx.settings_rule)
		)
		if not pool_name:
			pool_name = pool_map.get((bx.account, bx.cost_center, bx.department))
		if pool_name:
			pool_allocated[pool_name] = pool_allocated.get(pool_name, Decimal("0")) + bx.allocated_amount

	for pool_name, amt in pool_allocated.items():
		net = Decimal(str(flt(frappe.db.get_value(DETAIL_DOCTYPE_MAP["expense_pool"], pool_name, "monthly_net_expense"))))
		frappe.db.set_value(
			DETAIL_DOCTYPE_MAP["expense_pool"],
			pool_name,
			{
				"allocated_amount": _f(amt),
				"unallocated_amount": _f(net - amt),
			},
			update_modified=False,
		)

	for batch in batches:
		doc = frappe.get_doc(
			{
				"doctype": DETAIL_DOCTYPE_MAP["batch"],
				"monthly_calculation": monthly_name,
				"revision": revision,
				"company": period.company,
				"persian_year": period.persian_year,
				"persian_month": period.persian_month,
				"batch_no": batch.batch_no,
				"finished_item": batch.finished_item,
				"work_orders": json.dumps(batch.work_orders, ensure_ascii=False),
				"stock_entries": json.dumps(batch.stock_entries, ensure_ascii=False),
				"stock_entry_details": json.dumps(batch.stock_entry_details, ensure_ascii=False),
				"posting_date_min": batch.posting_date_min,
				"posting_date_max": batch.posting_date_max,
				"actual_qty": _f(batch.actual_qty),
				"uom": batch.uom,
				"normalization_factor": _f(batch.normalization_factor) if batch.normalization_factor is not None else None,
				"normalized_qty": _f(batch.normalized_qty),
				"total_monthly_normalized_qty": _f(batch.total_monthly_normalized_qty),
				"calculated_share": _f(batch.calculated_share),
				"manual_share": _f(batch.manual_share) if batch.manual_share is not None else None,
				"is_manual_override": 1 if batch.is_manual_override else 0,
				"applied_share": _f(batch.applied_share),
				"total_allocated_expense": _f(batch.total_allocated_expense),
				"currency": currency,
			}
		)
		doc.insert(ignore_permissions=True)
		batch_map[batch.batch_no] = doc.name

	for bx in batch_expenses:
		pool_name = pool_map.get(bx.pool_key) or pool_map.get((bx.account, bx.cost_center, bx.department))
		frappe.get_doc(
			{
				"doctype": DETAIL_DOCTYPE_MAP["batch_expense"],
				"monthly_calculation": monthly_name,
				"revision": revision,
				"batch_allocation": batch_map[bx.batch_no],
				"batch_no": bx.batch_no,
				"expense_pool": pool_name,
				"account": bx.account,
				"cost_center": bx.cost_center or None,
				"department": bx.department or None,
				"pool_monthly_net_expense": _f(bx.pool_monthly_net_expense),
				"applied_batch_share": _f(bx.applied_batch_share),
				"allocated_amount": _f(bx.allocated_amount),
				"currency": currency,
				"residual_applied": 1 if bx.residual_applied else 0,
			}
		).insert(ignore_permissions=True)

	for batch, product, product_share, product_allocated in product_rows:
		unit_actual = (
			(product_allocated / product.actual_qty) if product.actual_qty else Decimal("0")
		)
		unit_norm = (
			(product_allocated / product.normalized_qty) if product.normalized_qty else Decimal("0")
		)
		doc = frappe.get_doc(
			{
				"doctype": DETAIL_DOCTYPE_MAP["product"],
				"monthly_calculation": monthly_name,
				"revision": revision,
				"batch_allocation": batch_map[batch.batch_no],
				"batch_no": batch.batch_no,
				"item_code": product.item_code,
				"item_name": product.item_name,
				"work_order": next(iter(sorted(product.work_orders)), None),
				"stock_entry": next(iter(sorted(product.stock_entries)), None),
				"stock_entry_detail": next(iter(sorted(product.stock_entry_details)), None),
				"stock_entries": json.dumps(sorted(product.stock_entries), ensure_ascii=False),
				"actual_qty": _f(product.actual_qty),
				"uom": product.uom,
				"normalization_factor": _f(product.normalization_factor)
				if product.normalization_factor is not None
				else None,
				"normalization_uom": product.normalization_uom,
				"normalized_qty": _f(product.normalized_qty),
				"batch_total_normalized_qty": _f(batch.normalized_qty),
				"product_share": _f(product_share),
				"batch_allocated_expense": _f(batch.total_allocated_expense),
				"product_allocated_expense": _f(product_allocated),
				"unit_cost_on_actual": _f(unit_actual),
				"unit_cost_on_normalized": _f(unit_norm),
				"currency": currency,
				"missing_uom_factor": 1 if product.missing_uom_factor else 0,
			}
		)
		doc.insert(ignore_permissions=True)
		product_map[(batch.batch_no, product.item_code)] = doc.name

	for px in product_expenses:
		pool_name = pool_map.get(px.pool_key) or pool_map.get((px.account, px.cost_center, px.department))
		frappe.get_doc(
			{
				"doctype": DETAIL_DOCTYPE_MAP["product_expense"],
				"monthly_calculation": monthly_name,
				"revision": revision,
				"product_allocation": product_map[(px.batch_no, px.item_code)],
				"batch_allocation": batch_map[px.batch_no],
				"batch_no": px.batch_no,
				"item_code": px.item_code,
				"expense_pool": pool_name,
				"account": px.account,
				"cost_center": px.cost_center or None,
				"department": px.department or None,
				"pool_monthly_net_expense": _f(px.pool_monthly_net_expense),
				"applied_batch_share": _f(px.applied_batch_share),
				"product_share": _f(px.product_share),
				"batch_pool_allocated_amount": _f(px.batch_pool_allocated_amount),
				"product_pool_allocated_amount": _f(px.product_pool_allocated_amount),
				"currency": currency,
				"residual_applied": 1 if px.residual_applied else 0,
			}
		).insert(ignore_permissions=True)

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
