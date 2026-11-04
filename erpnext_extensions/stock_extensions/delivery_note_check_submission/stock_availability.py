# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only stock availability checks for Delivery Note Check Submission.

Stock and batch balances are evaluated as of the Delivery Note posting date/time,
not today's Bin quantity. Batch expiry follows ERPNext's convention:

	expired iff expiry_date is set and getdate(expiry_date) < getdate(posting_date)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate

from erpnext.stock.stock_ledger import NegativeStockError, is_negative_stock_allowed
from erpnext.stock.utils import _get_stock_balance, get_combine_datetime

from erpnext_extensions.stock_extensions.delivery_note_check_submission.errors import make_error


def cint_is_stock(item_code: str) -> bool:
	return bool(frappe.get_cached_value("Item", item_code, "is_stock_item"))


def _posting_cutoff(doc) -> tuple[Any, Any, Any]:
	"""Return (posting_date, posting_time, posting_datetime) for historical queries."""
	posting_date = doc.get("posting_date")
	posting_time = doc.get("posting_time") or "00:00:00"
	if not posting_date:
		frappe.throw(_("Delivery Note Posting Date is required for stock validation."))
	return posting_date, posting_time, get_combine_datetime(posting_date, posting_time)


def _historical_actual_qty(item_code: str, warehouse: str, posting_date, posting_time) -> float:
	"""Qty after last SLE at or before DN posting (read-only)."""
	return flt(
		_get_stock_balance(
			item_code,
			warehouse,
			posting_date=posting_date,
			posting_time=posting_time,
		)
		or 0
	)


def _reserved_qty_as_of(
	item_code: str,
	warehouse: str,
	posting_datetime,
	*,
	exclude_so_detail: str | None = None,
) -> float:
	"""Reserved remaining qty as of posting_datetime (SRE.creation cutoff; no FOR UPDATE)."""
	params: list = [item_code, warehouse, posting_datetime]
	exclude_sql = ""
	if exclude_so_detail:
		exclude_sql = " and voucher_detail_no != %s"
		params.append(exclude_so_detail)

	row = frappe.db.sql(
		f"""
		select
			coalesce(sum(reserved_qty - delivered_qty - transferred_qty - consumed_qty), 0) as reserved
		from `tabStock Reservation Entry`
		where docstatus = 1
			and item_code = %s
			and warehouse = %s
			and creation <= %s
			and delivered_qty < reserved_qty
			{exclude_sql}
		""",
		tuple(params),
	)
	return max(flt(row[0][0] if row else 0), 0.0)


def _sre_remaining_for_so_detail_as_of(
	so: str, so_detail: str, warehouse: str, item_code: str, posting_datetime
) -> float:
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
			and creation <= %s
			and delivered_qty < reserved_qty
		""",
		(so, so_detail, warehouse, item_code, posting_datetime),
		as_dict=True,
	)
	total = 0.0
	for r in rows:
		total += flt(r.reserved_qty) - flt(r.delivered_qty) - flt(r.transferred_qty) - flt(r.consumed_qty)
	return max(total, 0.0)


def _historical_batch_qty(
	item_code: str,
	warehouse: str,
	batch_no: str,
	posting_date,
	posting_time,
) -> float:
	"""Batch balance as of DN posting, including batches that are expired *today*
	but were still valid on the posting date.

	Uses ``for_stock_levels=True`` so ERPNext does not filter ``expiry_date >= today()``.
	Expiry vs posting_date is enforced separately.
	"""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	qty = get_batch_qty(
		batch_no=batch_no,
		warehouse=warehouse,
		item_code=item_code,
		posting_date=posting_date,
		posting_time=posting_time,
		for_stock_levels=True,
		ignore_reserved_stock=True,
	)
	return flt(qty or 0)


def is_batch_expired_on(expiry_date, posting_date) -> bool:
	"""ERPNext convention: expired when expiry_date < posting_date (equality is valid)."""
	if not expiry_date or not posting_date:
		return False
	return getdate(expiry_date) < getdate(posting_date)


def _iter_stock_rows(doc):
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


def collect_stock_availability_errors(doc) -> list[dict]:
	"""Collect insufficient-stock errors using historical balances as of DN posting."""
	errors: list[dict] = []
	if cint(doc.get("is_return")):
		return errors

	try:
		posting_date, posting_time, posting_datetime = _posting_cutoff(doc)
	except Exception as exc:
		return [
			make_error(
				category="stock_availability",
				message=exc,
				exc_type=type(exc).__name__,
			)
		]

	# Aggregate demand per (item, warehouse) and per batch.
	demand: dict[tuple[str, str], float] = defaultdict(float)
	usable_reservation: dict[tuple[str, str], float] = defaultdict(float)
	batch_demand: dict[tuple[str, str, str], float] = defaultdict(float)
	row_refs: dict[tuple[str, str], list] = defaultdict(list)
	batch_row_refs: dict[tuple[str, str, str], list] = defaultdict(list)

	for table, row, qty in _iter_stock_rows(doc):
		if is_negative_stock_allowed(item_code=row.item_code):
			continue
		key = (row.item_code, row.warehouse)
		demand[key] += abs(qty)
		row_refs[key].append((table, row))

		so = row.get("against_sales_order")
		so_detail = row.get("so_detail")
		if so and so_detail:
			usable_reservation[key] = max(
				usable_reservation[key],
				_sre_remaining_for_so_detail_as_of(
					so, so_detail, row.warehouse, row.item_code, posting_datetime
				),
			)

		batch_no = row.get("batch_no")
		if batch_no:
			bkey = (row.item_code, row.warehouse, batch_no)
			batch_demand[bkey] += abs(qty)
			batch_row_refs[bkey].append((table, row))

	for (item_code, warehouse), needed in demand.items():
		try:
			actual = _historical_actual_qty(item_code, warehouse, posting_date, posting_time)
			# Reservations for other vouchers as of posting; SO-detail usable qty added back.
			total_reserved = _reserved_qty_as_of(item_code, warehouse, posting_datetime)
			other_reserved = max(total_reserved - usable_reservation[(item_code, warehouse)], 0.0)
			available = actual - other_reserved
		except Exception as exc:
			errors.append(
				make_error(
					category="stock_availability",
					message=exc,
					exc_type=type(exc).__name__,
					item_code=item_code,
					warehouse=warehouse,
					row=row_refs[(item_code, warehouse)][0][1].idx
					if row_refs[(item_code, warehouse)]
					else None,
				)
			)
			continue

		if needed > available + 1e-6:
			table, row = row_refs[(item_code, warehouse)][0]
			errors.append(
				make_error(
					category="stock_availability",
					message=_(
						"Row #{0}: Insufficient stock for Item {1} in Warehouse {2} "
						"as of Delivery Note date {3}. Needed {4}, available {5} "
						"(historical actual {6}, reserved by others {7})."
					).format(
						row.idx,
						frappe.bold(item_code),
						frappe.bold(warehouse),
						posting_date,
						flt(needed),
						flt(available),
						flt(actual),
						flt(other_reserved),
					),
					exc_type="NegativeStockError",
					item_code=item_code,
					row=row.idx,
					warehouse=warehouse,
					table=table,
					extra={
						"needed": flt(needed),
						"available": flt(available),
						"historical_actual": flt(actual),
						"posting_date": str(getdate(posting_date)),
					},
				)
			)

	for (item_code, warehouse, batch_no), needed in batch_demand.items():
		if is_negative_stock_allowed(item_code=item_code):
			continue
		# Skip batch stock check when batch is expired on posting date — expiry collector handles it.
		expiry = frappe.db.get_value("Batch", batch_no, "expiry_date")
		if is_batch_expired_on(expiry, posting_date):
			continue
		try:
			available = _historical_batch_qty(
				item_code, warehouse, batch_no, posting_date, posting_time
			)
		except Exception as exc:
			table, row = batch_row_refs[(item_code, warehouse, batch_no)][0]
			errors.append(
				make_error(
					category="stock_availability",
					message=exc,
					exc_type=type(exc).__name__,
					item_code=item_code,
					row=row.idx,
					warehouse=warehouse,
					batch_no=batch_no,
					table=table,
				)
			)
			continue

		if needed > available + 1e-6:
			table, row = batch_row_refs[(item_code, warehouse, batch_no)][0]
			errors.append(
				make_error(
					category="stock_availability",
					message=_(
						"Row #{0}: Insufficient stock for Item {1} / Batch {2} in Warehouse {3} "
						"as of Delivery Note date {4}. Needed {5}, available {6}."
					).format(
						row.idx,
						frappe.bold(item_code),
						frappe.bold(batch_no),
						frappe.bold(warehouse),
						posting_date,
						flt(needed),
						flt(available),
					),
					exc_type="NegativeStockError",
					item_code=item_code,
					row=row.idx,
					warehouse=warehouse,
					batch_no=batch_no,
					table=table,
					extra={
						"needed": flt(needed),
						"available": flt(available),
						"posting_date": str(getdate(posting_date)),
					},
				)
			)

	return errors


def validate_stock_availability(doc) -> None:
	"""Legacy throw-on-first API (kept for callers); prefer collect_stock_availability_errors."""
	errors = collect_stock_availability_errors(doc)
	if errors:
		frappe.throw(errors[0]["message"], NegativeStockError, title=_("Insufficient Stock"))


def collect_over_delivery_errors(doc) -> list[dict]:
	"""Per-row over-delivery checks (read-only)."""
	from erpnext.controllers.status_updater import get_allowance_for

	errors: list[dict] = []
	if cint(doc.get("is_return")):
		return errors

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
			errors.append(
				make_error(
					category="over_delivery",
					message=_(
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
					exc_type="ValidationError",
					item_code=row.item_code,
					row=row.idx,
					table="items",
				)
			)
	return errors


def validate_over_delivery(doc) -> None:
	errors = collect_over_delivery_errors(doc)
	if errors:
		frappe.throw(errors[0]["message"], frappe.ValidationError, title=_("Over Delivery"))
