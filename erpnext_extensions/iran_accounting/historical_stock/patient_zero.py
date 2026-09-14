# Copyright (c) 2026, ERPNext Extensions contributors
"""Find the first invalid valuation transition per item + warehouse (+ batch)."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	POISON_RATE,
	QTY_EPS,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.util import g
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason


def transition_is_invalid(prev, row) -> str | None:
	"""Return a reason if ``row`` is the first poisoned SLE after ``prev``."""
	poison = sle_poison_reason(row)
	if poison:
		return poison
	qty = flt(g(row, "actual_qty"))
	incoming = flt(g(row, "incoming_rate"))
	svd = flt(g(row, "stock_value_difference"))
	rate = flt(g(row, "valuation_rate"))
	value = flt(g(row, "stock_value"))
	after = flt(g(row, "qty_after_transaction"))
	if qty > QTY_EPS and abs(incoming) < QTY_EPS and abs(svd) < VALUE_EPS:
		if prev is not None:
			prev_rate = abs(flt(g(prev, "valuation_rate"))) + abs(flt(g(prev, "incoming_rate")))
			if prev_rate > VALUE_EPS:
				return "nonzero_to_zero_incoming"
		return "zero_incoming_with_qty"
	if abs(after) <= QTY_EPS and abs(value) > 1:
		return "qty_after_zero_nonzero_value"
	if after > 1 and abs(value) < VALUE_EPS and abs(qty) > QTY_EPS:
		if prev is not None and abs(flt(g(prev, "stock_value"))) > 1:
			return "value_collapsed_to_zero"
	if abs(rate) > POISON_RATE:
		return "exploded_rate"
	return None


def find_patient_zero_identity(item, warehouse, batch: str | None = None) -> dict | None:
	"""First invalid SLE on an item+warehouse identity. Batch is optional."""
	if not item or not warehouse:
		return None
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT sle.name, sle.voucher_no, sle.item_code, sle.warehouse, sle.actual_qty,
		       sle.incoming_rate, sle.outgoing_rate, sle.stock_value_difference,
		       sle.stock_value, sle.valuation_rate, sle.qty_after_transaction,
		       sle.posting_datetime, sle.creation, sle.batch_no
		FROM `tabStock Ledger Entry` sle
		WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=0
		  AND sle.voucher_type='Stock Entry'
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT 8000
		""",
		(item, warehouse),
		as_dict=True,
	)
	# Warehouse patient-zero first. A later poisoned lot often depends on an
	# earlier leftover on a different batch of the same identity.
	found = find_patient_zero(rows, batch=None)
	if batch and found:
		same = find_patient_zero(rows, batch=batch)
		if same:
			return same
	return found


def find_patient_zero(rows: list, *, batch: str | None = None) -> dict | None:
	"""``rows`` must already be ordered posting_datetime ASC, creation ASC."""
	prev = None
	for row in rows:
		if batch:
			canon = g(row, "canonical_batch") or g(row, "batch_no") or ""
			if canon and canon != batch:
				prev = row
				continue
		reason = transition_is_invalid(prev, row)
		if reason:
			return {
				"voucher_no": g(row, "voucher_no"),
				"sle_name": g(row, "name"),
				"item_code": g(row, "item_code"),
				"warehouse": g(row, "warehouse"),
				"batch": batch or g(row, "batch_no") or g(row, "canonical_batch"),
				"posting_datetime": g(row, "posting_datetime"),
				"reason": reason,
				"previous_voucher": g(prev, "voucher_no") if prev else None,
			}
		prev = row
	return None
