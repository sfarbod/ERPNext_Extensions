# Copyright (c) 2026, ERPNext Extensions contributors
"""Replay SLE qty_after / stock_value / SVD / valuation_rate and Bin.

Warehouse-level sequence matches ERPNext 16.34.2 Stock Ledger (SABB qty_after
is warehouse identity, not batch). Aborts when the window already contains
5.2.0 valuation poison — posting-order repair must not mix with that repair.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.stock_posting_order import STATUS_VALUATION_POISON
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

POISON_RATE = Decimal("1000000000000")  # 1e12
VALUE_EPS = Decimal("0.5")
QTY_EPS = Decimal("0.0001")


def _q(x) -> Decimal:
	return D(x).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def sle_poison_reason(row) -> str | None:
	qty = D(_g(row, "actual_qty"))
	after = D(_g(row, "qty_after_transaction"))
	value = D(_g(row, "stock_value"))
	svd = D(_g(row, "stock_value_difference"))
	rate = D(_g(row, "valuation_rate"))
	incoming = D(_g(row, "incoming_rate"))
	if qty > 0 and incoming < 0:
		return "negative_incoming_rate"
	if qty > 0 and svd < -VALUE_EPS:
		return "sign_inverted_incoming_svd"
	if qty < 0 and svd > VALUE_EPS:
		return "sign_inverted_outgoing_svd"
	if abs(after) <= QTY_EPS and abs(value) > 1:
		return "qty_after_zero_nonzero_value"
	if abs(qty) <= QTY_EPS and abs(value) > 1:
		return "qty_zero_nonzero_value"
	if abs(rate) > POISON_RATE:
		return "exploded_rate"
	return None


def replay_series(rows: list, opening_qty=0, opening_value=0) -> list[dict]:
	"""Pure moving-average replay. Does not write."""
	running_qty = D(opening_qty)
	running_value = D(opening_value)
	out = []
	for row in rows:
		qty = D(_g(row, "actual_qty"))
		old_svd = D(_g(row, "stock_value_difference"))
		if qty > 0:
			rate = D(_g(row, "incoming_rate"))
			if rate == 0:
				rate = D(_g(row, "valuation_rate"))
			if rate == 0 and running_qty:
				rate = running_value / running_qty
			svd = qty * rate
			running_qty += qty
			running_value += svd
		else:
			rate = (running_value / running_qty) if running_qty else D(_g(row, "valuation_rate"))
			svd = qty * rate
			running_qty += qty
			running_value += svd
			if abs(running_qty) <= QTY_EPS:
				running_qty = D(0)
		val_rate = (running_value / running_qty) if running_qty else D(0)
		out.append(
			{
				"name": _g(row, "name"),
				"voucher_no": _g(row, "voucher_no"),
				"qty_after_transaction": running_qty,
				"stock_value": running_value,
				"stock_value_difference": svd,
				"valuation_rate": val_rate,
				"old_stock_value_difference": old_svd,
				"svd_changed": abs(svd - old_svd) > VALUE_EPS,
			}
		)
	return out


def window_poison_reason(item_code, warehouse, from_dt) -> str | None:
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	if prev:
		reason = sle_poison_reason(prev)
		if reason:
			return reason
	for row in rows:
		reason = sle_poison_reason(row)
		if reason:
			return reason
	return None


def replay_item_warehouse(item_code, warehouse, from_dt) -> dict:
	poison = window_poison_reason(item_code, warehouse, from_dt)
	if poison:
		return {
			"ok": False,
			"status": STATUS_VALUATION_POISON,
			"reason": poison,
			"item_code": item_code,
			"warehouse": warehouse,
		}
	prev = _fetch_previous(item_code, warehouse, from_dt)
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	series = replay_series(rows, opening_qty, opening_value)
	valuation_changed = False
	touched_vouchers = set()
	for i, row in enumerate(rows):
		step = series[i]
		if step["svd_changed"]:
			valuation_changed = True
			touched_vouchers.add(row.voucher_no)
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
	final_qty = series[-1]["qty_after_transaction"] if series else opening_qty
	final_value = series[-1]["stock_value"] if series else opening_value
	final_rate = series[-1]["valuation_rate"] if series else D(0)
	_update_bin(item_code, warehouse, final_qty, final_value, final_rate)
	return {
		"ok": True,
		"status": "REPLAYED",
		"item_code": item_code,
		"warehouse": warehouse,
		"final_qty": final_qty,
		"final_value": final_value,
		"valuation_changed": valuation_changed,
		"touched_vouchers": sorted(touched_vouchers),
		"rows": len(series),
	}


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _fetch_previous(item_code, warehouse, from_dt):
	rows = frappe.db.sql(
		"""
		SELECT name, qty_after_transaction, stock_value, stock_value_difference,
		       valuation_rate, incoming_rate, actual_qty, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime < %s
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(item_code, warehouse, from_dt),
		as_dict=True,
	)
	return rows[0] if rows else None


def _fetch_sles(item_code, warehouse, from_dt, *, before=False):
	op = "<" if before else ">="
	return frappe.db.sql(
		f"""
		SELECT name, voucher_no, voucher_type, actual_qty, qty_after_transaction,
		       incoming_rate, valuation_rate, stock_value, stock_value_difference,
		       posting_datetime, creation
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime {op} %s
		ORDER BY posting_datetime, creation
		""",
		(item_code, warehouse, from_dt),
		as_dict=True,
	)


def _update_bin(item_code, warehouse, qty, value, rate) -> None:
	if not frappe.db.exists("Bin", {"item_code": item_code, "warehouse": warehouse}):
		return
	frappe.db.sql(
		"""
		UPDATE `tabBin`
		SET actual_qty=%s, stock_value=%s, valuation_rate=%s
		WHERE item_code=%s AND warehouse=%s
		""",
		(flt(qty), flt(value), flt(rate), item_code, warehouse),
	)
