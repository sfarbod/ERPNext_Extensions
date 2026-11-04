# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only stock availability checks for Delivery Note Check Submission."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.stock.stock_ledger import NegativeStockError, is_negative_stock_allowed


def _bin_qty(item_code: str, warehouse: str) -> tuple[float, float]:
	"""Return (actual_qty, reserved_qty) from Bin without locking."""
	row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "reserved_qty", "reserved_stock"],
		as_dict=True,
	)
	if not row:
		return 0.0, 0.0
	# reserved_stock is the modern field used by SLE paths; fall back to reserved_qty.
	reserved = flt(row.reserved_stock) if row.reserved_stock is not None else flt(row.reserved_qty)
	return flt(row.actual_qty), reserved


def _sre_remaining_for_so_detail(so: str, so_detail: str, warehouse: str, item_code: str) -> float:
	"""Qty still reserved for this Sales Order Item at warehouse (read-only, no FOR UPDATE)."""
	if not so or not so_detail:
		return 0.0
	rows = frappe.db.sql(
		"""
		select reserved_qty, delivered_qty, transferred_qty, consumed_qty
		from `tabStock Reservation Entry`
		where docstatus = 1
			and voucher_type = 'Sales Order'
			and voucher_no = %s
			and voucher_detail_no = %s
			and warehouse = %s
			and item_code = %s
			and delivered_qty < reserved_qty
		""",
		(so, so_detail, warehouse, item_code),
		as_dict=True,
	)
	total = 0.0
	for r in rows:
		total += flt(r.reserved_qty) - flt(r.delivered_qty) - flt(r.transferred_qty) - flt(r.consumed_qty)
	return max(total, 0.0)


def _batch_available(item_code: str, warehouse: str, batch_no: str) -> float:
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	return flt(get_batch_qty(batch_no=batch_no, warehouse=warehouse, item_code=item_code) or 0)


def _iter_stock_rows(doc):
	"""Yield stock rows from items + packed_items with stock_qty demand."""
	for table in ("items", "packed_items"):
		for row in doc.get(table) or []:
			if not row.get("item_code") or not row.get("warehouse"):
				continue
			if not cint_is_stock(row.item_code):
				continue
			qty = flt(row.get("stock_qty") or row.get("qty") or 0)
			if not qty:
				continue
			yield table, row, qty


def cint_is_stock(item_code: str) -> bool:
	return bool(frappe.get_cached_value("Item", item_code, "is_stock_item"))


def validate_stock_availability(doc) -> None:
	"""Throw NegativeStockError when delivery would drive available qty negative.

	Available qty for a DN row = Bin.actual_qty
	  - reserved stock belonging to *other* vouchers
	  + reserved stock belonging to this row's Sales Order Item (usable by this DN)

	Batch-controlled rows also require sufficient batch balance.
	Returns are skipped (inward).
	"""
	if cint(doc.get("is_return")):
		return

	# Aggregate demand per (item, warehouse) and track SO-detail reservations usable.
	demand: dict[tuple[str, str], float] = defaultdict(float)
	usable_reservation: dict[tuple[str, str], float] = defaultdict(float)
	batch_demand: dict[tuple[str, str, str], float] = defaultdict(float)
	row_refs: dict[tuple[str, str], list] = defaultdict(list)

	for _table, row, qty in _iter_stock_rows(doc):
		if is_negative_stock_allowed(item_code=row.item_code):
			continue
		key = (row.item_code, row.warehouse)
		demand[key] += abs(qty)
		row_refs[key].append(row)

		so = row.get("against_sales_order")
		so_detail = row.get("so_detail")
		if so and so_detail:
			usable_reservation[key] = max(
				usable_reservation[key],
				_sre_remaining_for_so_detail(so, so_detail, row.warehouse, row.item_code),
			)

		batch_no = row.get("batch_no")
		if batch_no:
			batch_demand[(row.item_code, row.warehouse, batch_no)] += abs(qty)

	for (item_code, warehouse), needed in demand.items():
		actual, reserved = _bin_qty(item_code, warehouse)
		# Stock reserved for this DN's SO detail remains available; other reserved stock does not.
		other_reserved = max(reserved - usable_reservation[(item_code, warehouse)], 0.0)
		available = actual - other_reserved
		if needed > available + 1e-6:
			rows = row_refs[(item_code, warehouse)]
			idx = rows[0].idx if rows else "?"
			frappe.throw(
				_(
					"Row #{0}: Insufficient stock for Item {1} in Warehouse {2}. "
					"Needed {3}, available {4} (actual {5}, reserved by others {6})."
				).format(
					idx,
					frappe.bold(item_code),
					frappe.bold(warehouse),
					flt(needed),
					flt(available),
					flt(actual),
					flt(other_reserved),
				),
				NegativeStockError,
				title=_("Insufficient Stock"),
			)

	for (item_code, warehouse, batch_no), needed in batch_demand.items():
		if is_negative_stock_allowed(item_code=item_code):
			continue
		available = _batch_available(item_code, warehouse, batch_no)
		if needed > available + 1e-6:
			frappe.throw(
				_(
					"Insufficient stock for Item {0} / Batch {1} in Warehouse {2}. "
					"Needed {3}, available {4}."
				).format(
					frappe.bold(item_code),
					frappe.bold(batch_no),
					frappe.bold(warehouse),
					flt(needed),
					flt(available),
				),
				NegativeStockError,
				title=_("Insufficient Batch Stock"),
			)


def cint(value) -> int:
	from frappe.utils import cint as _cint

	return _cint(value)


def validate_over_delivery(doc) -> None:
	"""Read-only over-delivery check against Sales Order / Sales Invoice quantities.

	``StatusUpdater.validate_qty`` only sees DB state *after* ``update_qty`` writes,
	so it cannot be reused for a draft. This mirrors the allowance math without writes.
	"""
	from erpnext.controllers.status_updater import get_allowance_for

	if cint(doc.get("is_return")):
		return

	item_allowance: dict = {}
	global_qty_allowance = None
	global_amount_allowance = None

	for row in doc.get("items") or []:
		so_detail = row.get("so_detail")
		if not so_detail:
			continue
		soi = frappe.db.get_value(
			"Sales Order Item",
			so_detail,
			["name", "item_code", "qty", "delivered_qty", "conversion_factor", "parent", "docstatus"],
			as_dict=True,
		)
		if not soi or cint(soi.docstatus) != 1:
			continue
		if not frappe.get_cached_value("Item", soi.item_code, "is_stock_item"):
			continue

		# delivered_qty / qty on Sales Order Item are in the SO row UOM;
		# convert both sides to stock UOM for comparison with DN stock_qty.
		cf = flt(soi.conversion_factor or 1) or 1.0
		stock_qty = flt(row.get("stock_qty") or (flt(row.qty) * flt(row.conversion_factor or 1)))
		so_stock_qty = flt(soi.qty) * cf
		already = flt(soi.delivered_qty) * cf

		allowance, item_allowance, global_qty_allowance, global_amount_allowance = get_allowance_for(
			soi.item_code,
			item_allowance,
			global_qty_allowance,
			global_amount_allowance,
			"qty",
		)
		max_allowed = so_stock_qty * (1 + flt(allowance) / 100.0)
		projected = already + stock_qty
		if projected > max_allowed + 0.01:
			frappe.throw(
				_(
					"Row #{0}: Over-delivery of Item {1} against Sales Order {2}. "
					"Ordered {3}, already delivered {4}, this Delivery Note {5}, max allowed {6}."
				).format(
					row.idx,
					frappe.bold(row.item_code),
					frappe.bold(soi.parent),
					flt(so_stock_qty),
					flt(already),
					flt(stock_qty),
					flt(max_allowed),
				),
				frappe.ValidationError,
				title=_("Over Delivery"),
			)
