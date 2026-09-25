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
		# Explicit allow_zero_valuation_rate on the Stock Entry Detail is document
		# authority for a free/zero receipt (e.g. Rahkaran Material Receipt). That
		# is LEGITIMATE_ZERO — not a Patient Zero root. Downstream zero consumers
		# of that lot remain zero by conservation, not by corruption.
		if _stock_entry_allows_zero_valuation(row):
			return None
		if prev is None:
			return "zero_incoming_with_qty"
		prev_value = abs(flt(g(prev, "stock_value")))
		prev_rate = abs(flt(g(prev, "valuation_rate"))) + abs(flt(g(prev, "incoming_rate")))
		# Zero-lot continuation: prior tip already had no stock value (including
		# post-depletion qty=0/value=0, or a prior allow_zero receipt). A new
		# zero inbound does not invent corruption — do not resurrect old rates.
		if prev_value <= VALUE_EPS and prev_rate <= VALUE_EPS:
			return None
		if prev_rate > VALUE_EPS or prev_value > VALUE_EPS:
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


def _stock_entry_allows_zero_valuation(row) -> bool:
	"""True when the Stock Entry Detail for this SLE explicitly allows zero rate."""
	import frappe

	voucher = g(row, "voucher_no")
	item = g(row, "item_code")
	if not voucher or not item:
		return False
	# Detail may be resolved via voucher_detail_no when present on the SLE.
	vdn = g(row, "voucher_detail_no")
	if vdn:
		return bool(frappe.db.get_value("Stock Entry Detail", vdn, "allow_zero_valuation_rate"))
	return bool(
		frappe.db.get_value(
			"Stock Entry Detail",
			{"parent": voucher, "item_code": item, "allow_zero_valuation_rate": 1},
			"name",
		)
	)


def find_patient_zero_identity(
	item,
	warehouse,
	batch: str | None = None,
	*,
	as_of=None,
) -> dict | None:
	"""First invalid SLE on an item+warehouse identity. Batch is optional.

	``as_of`` — when set (posting_datetime of the row under repair), only consider
	SLE at or before that instant. A later zero inbound must not block an earlier
	EXACT reconstructable issue/transfer.
	"""
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
	found = find_patient_zero(rows, batch=None, as_of=as_of)
	if batch and found:
		same = find_patient_zero(rows, batch=batch, as_of=as_of)
		if same:
			return same
	return found


def find_patient_zero(rows: list, *, batch: str | None = None, as_of=None) -> dict | None:
	"""``rows`` must already be ordered posting_datetime ASC, creation ASC.

	``as_of`` — ignore SLE strictly after this posting_datetime so a later
	patient-zero candidate cannot block an earlier reconstructable root.
	"""
	as_of_s = str(as_of) if as_of else None
	prev = None
	for row in rows:
		row_dt = g(row, "posting_datetime")
		if as_of_s and row_dt and str(row_dt) > as_of_s:
			break
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
