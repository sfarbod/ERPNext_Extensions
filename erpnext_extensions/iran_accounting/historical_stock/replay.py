# Copyright (c) 2026, ERPNext Extensions contributors
"""Forward replay after patient-zero rate reconstruction.

Unlike posting-order replay, this path is allowed to start from a reconstructed
incoming_rate. It still aborts on 5.2.0 sign/exploded poison that is *not* the
zero-rate pattern being repaired.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import VALUE_EPS
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	QTY_EPS,
	_fetch_previous,
	_fetch_sles,
	_update_bin,
	replay_series,
	sle_poison_reason,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D


def _zero_rate_poison_ok(reason: str | None) -> bool:
	"""qty>0 leftover-value / exploded / negative incoming still abort."""
	if reason in (
		"negative_incoming_rate",
		"sign_inverted_incoming_svd",
		"sign_inverted_outgoing_svd",
		"exploded_rate",
		"qty_after_zero_nonzero_value",
		"qty_zero_nonzero_value",
	):
		return False
	return True


def replay_from_patient_zero(item_code, warehouse, batch=None, *, from_dt=None) -> dict:
	if not item_code or not warehouse:
		return {"ok": False, "reason": "missing_identity"}
	if from_dt is None:
		from_dt = "1900-01-01 00:00:00"
	from_dt = get_datetime(from_dt)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	if prev:
		reason = sle_poison_reason(prev)
		if reason and not _zero_rate_poison_ok(reason) and reason != "qty_after_zero_nonzero_value":
			# leftover value at qty 0 is the chain we are repairing
			if abs(D(prev.qty_after_transaction)) > QTY_EPS:
				return {"ok": False, "status": "VALUATION_POISON_DEPENDENCY", "reason": reason}
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	for row in rows:
		reason = sle_poison_reason(row)
		if reason in ("negative_incoming_rate", "sign_inverted_incoming_svd", "exploded_rate"):
			return {
				"ok": False,
				"status": "VALUATION_POISON_DEPENDENCY",
				"reason": reason,
				"voucher": row.voucher_no,
			}
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	series = replay_series(rows, opening_qty, opening_value)
	touched = []
	for i, row in enumerate(rows):
		step = series[i]
		frappe.db.set_value(
			"Stock Ledger Entry",
			row.name,
			{
				"qty_after_transaction": flt(step["qty_after_transaction"]),
				"stock_value": flt(step["stock_value"]),
				"stock_value_difference": flt(step["stock_value_difference"]),
				"valuation_rate": flt(step["valuation_rate"]),
			},
			update_modified=False,
		)
		touched.append(row.voucher_no)
	final_qty = series[-1]["qty_after_transaction"] if series else opening_qty
	final_value = series[-1]["stock_value"] if series else opening_value
	final_rate = series[-1]["valuation_rate"] if series else D(0)
	_update_bin(item_code, warehouse, final_qty, final_value, final_rate)
	return {
		"ok": True,
		"status": "REPLAYED",
		"item_code": item_code,
		"warehouse": warehouse,
		"rows": len(series),
		"touched_vouchers": sorted(set(touched)),
		"final_qty": flt(final_qty),
		"final_value": flt(final_value),
		"final_rate": flt(final_rate),
	}


def simulate_replay(item_code, warehouse, from_dt) -> dict:
	from_dt = get_datetime(from_dt)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	series = replay_series(rows, opening_qty, opening_value)
	return {
		"ok": True,
		"dry_run": True,
		"rows": len(series),
		"final_qty": flt(series[-1]["qty_after_transaction"]) if series else flt(opening_qty),
		"final_value": flt(series[-1]["stock_value"]) if series else flt(opening_value),
	}
