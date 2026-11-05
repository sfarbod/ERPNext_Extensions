# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only stock availability checks for Delivery Note Check Submission.

Stock and batch balances are evaluated as of the Delivery Note posting date/time,
not today's Bin quantity. Batch expiry follows ERPNext's convention:

	expired iff expiry_date is set and getdate(expiry_date) < getdate(posting_date)

Also mirrors ERPNext submit-time future-ledger negative stock checks:

- ``validate_negative_qty_in_future_sle`` (warehouse qty_after_transaction shift)
- Serial and Batch Bundle ``get_batchwise_available_qty`` (batch cumulative across
  legacy SLE.batch_no + Serial and Batch Entry rows)

A historical balance that looks sufficient can still fail Submit when a later
voucher would be driven negative by inserting this Delivery Note's outward qty.
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


def _float_precision() -> int:
	return cint(frappe.db.get_default("float_precision")) or 2


def _allow_negative_batch(batch_no: str) -> bool:
	try:
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
			allow_negative_stock_for_batch,
		)

		return bool(allow_negative_stock_for_batch(batch_no))
	except Exception:
		return bool(frappe.db.get_single_value("Stock Settings", "allow_negative_stock_for_batch"))


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


def _batch_nos_from_row(row) -> list[tuple[str, float]]:
	"""Return [(batch_no, abs_qty), ...] for a DN/packed row (inline batch or SABB)."""
	out: list[tuple[str, float]] = []
	if row.get("batch_no"):
		qty = abs(flt(row.get("stock_qty") or row.get("qty") or 0))
		if qty:
			out.append((row.batch_no, qty))
		return out

	bundle = row.get("serial_and_batch_bundle")
	if not bundle:
		return out
	entries = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle, "is_cancelled": 0},
		fields=["batch_no", "qty"],
	)
	for e in entries:
		if not e.batch_no:
			continue
		qty = abs(flt(e.qty))
		if qty:
			out.append((e.batch_no, qty))
	return out


def _fetch_batch_ledger(item_code: str, warehouse: str, batch_no: str) -> list[dict]:
	"""Read-only batch ledger matching SABB get_available_qty_from_* (no FOR UPDATE)."""
	legacy = frappe.db.sql(
		"""
		select
			batch_no,
			actual_qty as qty,
			posting_datetime,
			creation
		from `tabStock Ledger Entry`
		where item_code = %s
			and warehouse = %s
			and is_cancelled = 0
			and docstatus = 1
			and batch_no = %s
			and ifnull(serial_and_batch_bundle, '') = ''
		""",
		(item_code, warehouse, batch_no),
		as_dict=True,
	)
	sabb = frappe.db.sql(
		"""
		select
			child.batch_no,
			child.qty,
			child.posting_datetime,
			child.creation
		from `tabSerial and Batch Entry` child
		inner join `tabStock Ledger Entry` sle
			on child.parent = sle.serial_and_batch_bundle
		where child.item_code = %s
			and child.warehouse = %s
			and child.batch_no = %s
			and child.is_cancelled = 0
			and child.docstatus = 1
			and sle.is_cancelled = 0
			and child.type_of_transaction in ('Inward', 'Outward')
			and ifnull(child.voucher_type, '') != 'Pick List'
		""",
		(item_code, warehouse, batch_no),
		as_dict=True,
	)
	rows = list(legacy or []) + list(sabb or [])
	rows.sort(
		key=lambda r: (
			get_datetime(r.get("posting_datetime")) or get_datetime("1900-01-01"),
			get_datetime(r.get("creation")) or get_datetime("1900-01-01"),
		)
	)
	return rows


def _future_batch_negative_after_outbound(
	item_code: str,
	warehouse: str,
	batch_no: str,
	posting_datetime,
	outbound_qty: float,
) -> dict | None:
	"""Project DN outward into the batch ledger; return first future deficit if any.

	Mirrors Serial and Batch Bundle.get_batchwise_available_qty + throw_negative_batch.
	"""
	if outbound_qty <= 0 or _allow_negative_batch(batch_no):
		return None

	precision = _float_precision()
	posting_dt = get_datetime(posting_datetime)
	ledger = _fetch_batch_ledger(item_code, warehouse, batch_no)

	# Virtual outward at DN posting (creation far in the future so it sits after
	# any same-timestamp historical rows — same as a freshly submitted SABB).
	virtual = frappe._dict(
		batch_no=batch_no,
		qty=-abs(outbound_qty),
		posting_datetime=posting_dt,
		creation=get_datetime("9999-12-31 23:59:59"),
		_virtual=True,
	)
	combined = list(ledger) + [virtual]
	combined.sort(
		key=lambda r: (
			get_datetime(r.get("posting_datetime")) or get_datetime("1900-01-01"),
			get_datetime(r.get("creation")) or get_datetime("1900-01-01"),
		)
	)

	cumulative = 0.0
	for row in combined:
		cumulative += flt(row.get("qty"))
		row_dt = get_datetime(row.get("posting_datetime"))
		if flt(cumulative, precision) < 0 and abs(flt(cumulative, precision)) > 0.0001:
			if row_dt and posting_dt and row_dt >= posting_dt:
				return {
					"needed": abs(flt(cumulative, precision)),
					"cumulative": flt(cumulative, precision),
					"posting_date": row.get("posting_datetime"),
					"voucher_type": None if row.get("_virtual") else None,
					"conflict_posting_datetime": str(row_dt),
				}
	return None


def _future_warehouse_negative_after_outbound(
	item_code: str,
	warehouse: str,
	posting_datetime,
	outbound_qty: float,
	*,
	voucher_no: str | None = None,
) -> dict | None:
	"""Read-only equivalent of validate_negative_qty_in_future_sle warehouse check.

	Inserting ``-outbound_qty`` shifts every future ``qty_after_transaction`` by that
	amount. Flag the earliest future SLE that would become negative.
	"""
	if outbound_qty <= 0:
		return None

	precision = _float_precision()
	params: dict[str, Any] = {
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_datetime": get_datetime(posting_datetime),
		"shift": -abs(outbound_qty),
	}
	exclude = ""
	if voucher_no:
		exclude = "and voucher_no != %(voucher_no)s"
		params["voucher_no"] = voucher_no

	rows = frappe.db.sql(
		f"""
		select
			qty_after_transaction,
			(qty_after_transaction + %(shift)s) as projected_qty,
			posting_date,
			posting_time,
			voucher_type,
			voucher_no
		from `tabStock Ledger Entry`
		where item_code = %(item_code)s
			and warehouse = %(warehouse)s
			and is_cancelled = 0
			and posting_datetime >= %(posting_datetime)s
			{exclude}
		order by posting_datetime asc, creation asc
		""",
		params,
		as_dict=True,
	)
	for row in rows or []:
		projected = flt(row.projected_qty, precision)
		if projected < 0 and abs(projected) > 0.0001:
			return {
				"needed": abs(projected),
				"available": flt(row.qty_after_transaction, precision),
				"posting_date": row.posting_date,
				"posting_time": row.posting_time,
				"voucher_type": row.voucher_type,
				"voucher_no": row.voucher_no,
			}
	return None


def collect_stock_availability_errors(doc) -> list[dict]:
	"""Collect insufficient-stock errors using historical + future-ledger checks."""
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

		for batch_no, bqty in _batch_nos_from_row(row):
			bkey = (row.item_code, row.warehouse, batch_no)
			batch_demand[bkey] += abs(bqty)
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

		table, row = row_refs[(item_code, warehouse)][0]
		if needed > available + 1e-6:
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
			# Still check future ledger — independent signal — but historical failure
			# already blocks; continue to surface future conflicts too when useful.

		future = _future_warehouse_negative_after_outbound(
			item_code,
			warehouse,
			posting_datetime,
			needed,
			voucher_no=doc.name,
		)
		if future:
			errors.append(
				make_error(
					category="stock_availability",
					message=_(
						"{0} units of {1} needed in {2} on {3} {4} for {5} {6} to complete this transaction."
					).format(
						frappe.bold(flt(future["needed"])),
						frappe.bold(item_code),
						frappe.bold(warehouse),
						future.get("posting_date"),
						future.get("posting_time"),
						future.get("voucher_type") or "",
						frappe.bold(future.get("voucher_no") or ""),
					),
					exc_type="NegativeStockError",
					item_code=item_code,
					row=row.idx,
					warehouse=warehouse,
					table=table,
					extra={
						"needed": flt(future["needed"]),
						"available": flt(future.get("available") or 0),
						"posting_date": str(getdate(posting_date)),
						"conflict_voucher": future.get("voucher_no"),
						"check": "future_warehouse_sle",
					},
				)
			)

	for (item_code, warehouse, batch_no), needed in batch_demand.items():
		if is_negative_stock_allowed(item_code=item_code):
			continue
		if _allow_negative_batch(batch_no):
			continue
		# Skip batch stock check when batch is expired on posting date — expiry collector handles it.
		expiry = frappe.db.get_value("Batch", batch_no, "expiry_date")
		if is_batch_expired_on(expiry, posting_date):
			continue
		table, row = batch_row_refs[(item_code, warehouse, batch_no)][0]
		try:
			available = _historical_batch_qty(
				item_code, warehouse, batch_no, posting_date, posting_time
			)
		except Exception as exc:
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
						"check": "historical_batch",
					},
				)
			)

		# Future / backdated batch cumulative (MAT-DN-2026-00281 class of failures).
		future_batch = _future_batch_negative_after_outbound(
			item_code, warehouse, batch_no, posting_datetime, needed
		)
		if future_batch:
			errors.append(
				make_error(
					category="stock_availability",
					message=_(
						"The Batch {0} of an item {1} has negative stock in the warehouse {2} "
						"as of {3}. Please add a stock quantity of {4} to proceed with this entry."
					).format(
						frappe.bold(batch_no),
						frappe.bold(item_code),
						frappe.bold(warehouse),
						future_batch.get("conflict_posting_datetime") or posting_datetime,
						frappe.bold(flt(future_batch["needed"])),
					),
					exc_type="NegativeStockError",
					item_code=item_code,
					row=row.idx,
					warehouse=warehouse,
					batch_no=batch_no,
					table=table,
					extra={
						"needed": flt(future_batch["needed"]),
						"available": flt(available),
						"posting_date": str(getdate(posting_date)),
						"check": "future_batch_ledger",
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
