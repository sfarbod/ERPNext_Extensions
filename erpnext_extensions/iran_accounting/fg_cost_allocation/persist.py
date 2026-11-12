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
	DETAIL_DOCTYPE_MAP["account_dimension_summary"],
	DETAIL_DOCTYPE_MAP["reconciliation_check"],
	DETAIL_DOCTYPE_MAP["exception"],
	DETAIL_DOCTYPE_MAP["gl_line"],
	DETAIL_DOCTYPE_MAP["product"],
	DETAIL_DOCTYPE_MAP["batch"],
	DETAIL_DOCTYPE_MAP["expense_pool"],
]


def clear_details(monthly_name: str, revision: int | None = None) -> None:
	"""Bulk-delete detail rows. Uses raw DELETE to avoid QB lock churn on large sets.

	When ``revision`` is None (full clear / delete monthly), also removes
	``MFG Cost Calculation Revision`` audit rows for that monthly so deletes
	leave no orphans. Per-revision clears keep revision logs for history.
	"""
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
	if revision is None:
		rev_dt = DETAIL_DOCTYPE_MAP["revision"]
		frappe.db.sql(
			f"DELETE FROM `tab{rev_dt}` WHERE monthly_calculation=%s",
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
		"work_order",
		"expense_pool",
		"account",
		"cost_center",
		"department",
		"pool_monthly_net_expense",
		"calculated_batch_share",
		"manual_batch_share",
		"applied_batch_share",
		"allocated_amount",
		"currency",
		"residual_applied",
		"is_manual_override",
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
				bx.work_order or None,
				pool_name,
				bx.account,
				bx.cost_center or None,
				bx.department or None,
				_f(bx.pool_monthly_net_expense),
				_f(bx.calculated_batch_share),
				_f(bx.manual_batch_share) if bx.manual_batch_share is not None else 0.0,
				_f(bx.applied_batch_share),
				_f(bx.allocated_amount),
				currency,
				1 if bx.residual_applied else 0,
				1 if bx.is_manual_override else 0,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["batch_expense"], bx_fields, bx_rows)

	# --- products (with material consumption + selling price economics) ---
	from erpnext_extensions.iran_accounting.fg_cost_allocation.material_cost import (
		attribute_material_to_products,
		get_selling_price,
	)
	from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision

	precision = get_currency_precision(currency)
	settings_name = frappe.db.get_value(
		"Finished Goods Cost Allocation Settings", {"company": frappe.db.get_value("Monthly FG Cost Calculation", monthly_name, "company")}, "name"
	)
	selling_price_list = (
		frappe.db.get_value("Finished Goods Cost Allocation Settings", settings_name, "selling_price_list")
		if settings_name
		else None
	)
	persian_period = frappe.db.get_value("Monthly FG Cost Calculation", monthly_name, "persian_period")

	# Material attribution per batch
	material_by_batch_item: dict[tuple[str, str], dict] = {}
	by_batch: dict[str, list] = {}
	for batch, product, product_share, product_allocated in product_rows:
		by_batch.setdefault(batch.batch_no, []).append(
			{
				"item_code": product.item_code,
				"product_share": float(product_share),
				"stock_entries": sorted(product.stock_entries or []),
				"work_orders": sorted(product.work_orders or []),
			}
		)
	for bno, plist in by_batch.items():
		attrs = attribute_material_to_products(batch_no=bno, products=plist, precision=precision)
		for item_code, meta in attrs.items():
			material_by_batch_item[(bno, item_code)] = meta

	prod_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"persian_period",
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
		"product_share_pct",
		"material_consumption_value",
		"batch_allocated_expense",
		"product_allocated_expense",
		"total_product_cost",
		"unit_cost_on_actual",
		"unit_cost_on_normalized",
		"selling_price",
		"price_list",
		"gross_difference",
		"gross_margin_pct",
		"price_status",
		"currency",
		"material_allocation_method",
		"material_source",
		"material_source_detail",
		"selling_price_source",
		"item_price",
		"missing_uom_factor",
	]
	prod_rows = []
	for idx, (batch, product, product_share, product_allocated) in enumerate(product_rows, start=1):
		pname = _name()
		product_map[(batch.batch_no, product.item_code)] = pname
		mat = material_by_batch_item.get((batch.batch_no, product.item_code)) or {}
		material_val = Decimal(str(flt(mat.get("material_consumption_value") or 0)))
		total_cost = material_val + Decimal(str(product_allocated))
		unit_actual = (total_cost / product.actual_qty) if product.actual_qty else Decimal("0")
		unit_norm = (total_cost / product.normalized_qty) if product.normalized_qty else Decimal("0")
		price = get_selling_price(product.item_code, selling_price_list)
		selling = price.get("selling_price")
		gross_diff = gross_pct = None
		if selling is not None and price.get("status") == "Available":
			gross_diff = Decimal(str(selling)) - unit_norm
			if Decimal(str(selling)) != 0:
				gross_pct = (gross_diff / Decimal(str(selling))) * Decimal("100")
			else:
				gross_pct = Decimal("0")
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
				persian_period,
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
				_f(Decimal(str(product_share)) * Decimal("100")),
				_f(material_val),
				_f(batch.total_allocated_expense),
				_f(product_allocated),
				_f(total_cost),
				_f(unit_actual),
				_f(unit_norm),
				_f(selling) if selling is not None else 0.0,
				price.get("price_list"),
				_f(gross_diff) if gross_diff is not None else 0.0,
				_f(gross_pct) if gross_pct is not None else 0.0,
				price.get("status") or "Missing",
				currency,
				mat.get("material_allocation_method"),
				mat.get("material_source"),
				mat.get("material_source_detail"),
				price.get("reason") or (f"Item Price {price.get('item_price')}" if price.get("item_price") else ""),
				price.get("item_price"),
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
	# Product × Expense Pool is no longer a primary costing structure.
	# Historical rows remain for prior revisions; new revisions do not write PxE rows.
	_ = (px_fields, product_expenses)

	# Account / Dimension allocation summary (grouped Batch × Expense)
	from collections import defaultdict

	ads_fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"account",
		"account_name",
		"cost_center",
		"department",
		"expense_pool",
		"pool_monthly_net_expense",
		"allocated_batch_count",
		"total_applied_batch_share",
		"total_allocated_amount",
		"allocation_status",
		"currency",
	]
	grouped: dict[tuple, dict] = {}
	for bx in batch_expenses:
		pool_name = pool_map.get((bx.account, bx.cost_center, bx.department, bx.settings_rule)) or pool_map.get(
			(bx.account, bx.cost_center, bx.department)
		)
		key = (bx.account, bx.cost_center or "", bx.department or "", pool_name or "")
		g = grouped.setdefault(
			key,
			{
				"account": bx.account,
				"cost_center": bx.cost_center or None,
				"department": bx.department or None,
				"expense_pool": pool_name,
				"pool_net": bx.pool_monthly_net_expense,
				"batches": set(),
				"share": Decimal("0"),
				"amount": Decimal("0"),
			},
		)
		g["batches"].add(bx.batch_no)
		g["share"] += bx.applied_batch_share
		g["amount"] += bx.allocated_amount

	ads_rows = []
	for idx, g in enumerate(grouped.values(), start=1):
		acct_name = frappe.db.get_value("Account", g["account"], "account_name") or ""
		status = "OK"
		if abs(g["share"] - Decimal("1")) > Decimal("0.0001") and g["pool_net"] != 0:
			status = "Share variance"
		ads_rows.append(
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
				g["account"],
				acct_name,
				g["cost_center"],
				g["department"],
				g["expense_pool"],
				_f(g["pool_net"]),
				len(g["batches"]),
				_f(g["share"]),
				_f(g["amount"]),
				status,
				currency,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["account_dimension_summary"], ads_fields, ads_rows)

	return {"pools": pool_map, "batches": batch_map, "products": product_map}


def persist_reconciliation_checks(
	*,
	monthly_name: str,
	revision: int,
	persian_period: str,
	currency: str,
	checks: list[dict],
) -> int:
	"""Materialize reconciliation checks as searchable rows (failed + sample of passed)."""
	fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"persian_period",
		"check_name",
		"description",
		"status",
		"account",
		"account_name",
		"cost_center",
		"department",
		"batch_no",
		"item_code",
		"expense_pool",
		"expected_value",
		"actual_value",
		"difference",
		"details",
		"currency",
	]
	user = frappe.session.user
	ts = now()
	rows = []
	failed = [c for c in checks if not c.get("ok")]
	passed = [c for c in checks if c.get("ok")]
	# Persist all failed + up to 100 passed samples to keep tables usable
	selected = failed + passed[:100]
	for idx, c in enumerate(selected, start=1):
		status = "Passed" if c.get("ok") else "Failed"
		detail = c.get("detail") or ""
		exp = c.get("expected")
		act = c.get("actual")
		try:
			diff = float(act) - float(exp) if exp is not None and act is not None else None
		except Exception:
			diff = None
		rows.append(
			[
				_name(),
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				cint(revision),
				persian_period,
				c.get("name") or "",
				detail,
				status,
				None,
				None,
				None,
				None,
				None,
				None,
				None,
				_f(exp) if exp is not None else 0,
				_f(act) if act is not None else 0,
				_f(diff) if diff is not None else 0,
				detail,
				currency,
			]
		)
	_bulk(DETAIL_DOCTYPE_MAP["reconciliation_check"], fields, rows)
	return len(rows)


def persist_exceptions(
	*,
	monthly_name: str,
	revision: int,
	persian_period: str,
	blocking: list[str],
	products: dict | None = None,
) -> int:
	"""Materialize blocking exceptions as searchable MFG Cost Exception rows."""
	fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"monthly_calculation",
		"revision",
		"persian_period",
		"exception_type",
		"severity",
		"resolution_status",
		"item_code",
		"item_name",
		"item_group",
		"stock_uom",
		"primary_uom",
		"conversion_factor",
		"batch_no",
		"work_order",
		"stock_entry",
		"account",
		"cost_center",
		"department",
		"actual_qty",
		"description",
	]
	user = frappe.session.user
	ts = now()
	rows = []
	idx = 0
	seen_keys = set()

	def _append_row(
		*,
		etype,
		severity,
		item=None,
		item_name="",
		item_group="",
		stock_uom="",
		primary_uom=None,
		conversion_factor=0,
		batch=None,
		work_order=None,
		stock_entry=None,
		actual_qty=0,
		description="",
	):
		nonlocal idx
		key = (etype, item or "", batch or "", description[:120])
		if key in seen_keys:
			return
		seen_keys.add(key)
		idx += 1
		rows.append(
			[
				_name(),
				ts,
				ts,
				user,
				user,
				0,
				idx,
				monthly_name,
				cint(revision),
				persian_period,
				etype,
				severity,
				"Open",
				item,
				item_name,
				item_group,
				stock_uom,
				primary_uom,
				_f(conversion_factor),
				batch,
				work_order,
				stock_entry,
				None,
				None,
				None,
				_f(actual_qty),
				description,
			]
		)

	# Product-level missing UOM rows (searchable by Item / Batch)
	for product in (products or {}).values():
		if not getattr(product, "missing_uom_factor", False):
			continue
		meta = frappe.db.get_value(
			"Item", product.item_code, ["item_name", "item_group", "stock_uom"], as_dict=True
		) or {}
		wo = ",".join(sorted(getattr(product, "work_orders", []) or [])) or None
		se = ",".join(sorted(getattr(product, "stock_entries", []) or [])) or None
		if wo and len(wo) > 500:
			wo = wo[:497] + "..."
		if se and len(se) > 500:
			se = se[:497] + "..."
		_append_row(
			etype="Missing UOM Conversion",
			severity="Blocking",
			item=product.item_code,
			item_name=meta.get("item_name") or getattr(product, "item_name", "") or "",
			item_group=meta.get("item_group") or "",
			stock_uom=meta.get("stock_uom") or getattr(product, "stock_uom", "") or "",
			primary_uom=getattr(product, "primary_uom", None),
			conversion_factor=getattr(product, "conversion_factor", 0) or 0,
			batch=product.batch_no,
			work_order=wo,
			stock_entry=se,
			actual_qty=getattr(product, "actual_qty", 0) or 0,
			description=(
				f"Missing verified conversion for item {product.item_code} "
				f"batch {product.batch_no} (Stock UOM → Primary UOM)"
			),
		)

	for msg in blocking or []:
		etype = "Other Validation Errors"
		severity = "Blocking"
		item = batch = None
		if "UOM" in msg or "conversion" in msg.lower():
			etype = (
				"Missing UOM Conversion"
				if "Missing" in msg or "missing" in msg.lower() or "incomplete" in msg.lower()
				else "Invalid Conversion Factor"
			)
		elif "share variance" in msg.lower() or "Applied share" in msg:
			etype = "Share Variance"
		elif "No enabled expense" in msg or "rules" in msg.lower():
			etype = "Missing Account / Dimension Rule"
		elif "Item not in Costing" in msg or "configured" in msg.lower():
			etype = "Unconfigured Item"
		parts = msg.split()
		for i, p in enumerate(parts):
			if p == "item" and i + 1 < len(parts):
				item = parts[i + 1].strip("():,")
			if p == "batch" and i + 1 < len(parts):
				batch = parts[i + 1].strip("():,")
		item_name = item_group = stock_uom = ""
		if item and frappe.db.exists("Item", item):
			meta = frappe.db.get_value("Item", item, ["item_name", "item_group", "stock_uom"], as_dict=True)
			item_name = meta.item_name
			item_group = meta.item_group
			stock_uom = meta.stock_uom
		_append_row(
			etype=etype,
			severity=severity,
			item=item,
			item_name=item_name,
			item_group=item_group,
			stock_uom=stock_uom,
			batch=batch,
			description=msg,
		)

	_bulk(DETAIL_DOCTYPE_MAP["exception"], fields, rows)
	return len(rows)


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
