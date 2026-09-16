# Copyright (c) 2026, ERPNext Extensions contributors
"""READ-ONLY warehouse valuation simulator — reuses native replay_series MA."""

from __future__ import annotations

from copy import deepcopy

from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import order_with_times
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	_fetch_previous,
	_fetch_sles,
	replay_series,
	sle_poison_reason,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D


def simulate_warehouse_replay(
	item_code: str,
	warehouse: str,
	from_dt,
	*,
	proposed_times: dict | None = None,
) -> dict:
	"""Simulate Item+Warehouse SLE chain from ``from_dt`` (read-only).

	Uses ERPNext order (posting_datetime, creation) via ``replay_series``.
	Optional ``proposed_times`` remaps voucher posting times before replay.
	"""
	from_dt = get_datetime(from_dt)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	if not rows:
		return {
			"ok": False,
			"reason": "no SLE rows in window",
			"item": item_code,
			"warehouse": warehouse,
			"from_datetime": str(from_dt),
		}

	# Current (as stored order)
	current_series = replay_series(rows, opening_qty, opening_value)

	# Proposed: reorder by proposed times when provided
	proposed_times = proposed_times or {}
	if proposed_times:
		ordered = order_with_times(rows, proposed_times)
	else:
		ordered = list(rows)
	proposed_series = replay_series(ordered, opening_qty, opening_value)

	# Align by SLE name for divergence / affected batches
	cur_by_name = {rows[i].name: current_series[i] for i in range(len(rows))}
	prop_by_name = {ordered[i].name: proposed_series[i] for i in range(len(ordered))}
	first_divergence = None
	affected = []
	batches = set()
	vouchers = set()
	work_orders = set()
	neg_qty = []
	neg_incoming = []
	exploded = []
	for row in ordered:
		c = cur_by_name.get(row.name) or {}
		p = prop_by_name.get(row.name) or {}
		diff_fields = []
		for f in ("qty_after_transaction", "stock_value", "valuation_rate", "stock_value_difference"):
			if abs(flt(c.get(f)) - flt(p.get(f))) > 0.5:
				diff_fields.append(f)
		if diff_fields:
			if first_divergence is None:
				first_divergence = {
					"sle": row.name,
					"voucher": row.voucher_no,
					"batch": row.batch_no,
					"fields": diff_fields,
				}
			affected.append(
				{
					"sle": row.name,
					"voucher": row.voucher_no,
					"batch": row.batch_no,
					"fields": diff_fields,
					"current": {k: c.get(k) for k in ("qty_after_transaction", "stock_value", "valuation_rate")},
					"proposed": {k: p.get(k) for k in ("qty_after_transaction", "stock_value", "valuation_rate")},
				}
			)
		if row.batch_no:
			batches.add(row.batch_no)
		vouchers.add(row.voucher_no)
		if flt(p.get("qty_after_transaction")) < -0.0001:
			neg_qty.append(row.voucher_no)
		poison = sle_poison_reason(_row_as_poison_probe(row, p))
		if poison == "negative_incoming_rate":
			neg_incoming.append(row.voucher_no)
		if poison == "exploded_rate":
			exploded.append(row.voucher_no)

	# Attach work orders for affected vouchers
	wo_map = _work_orders(list(vouchers)[:500])
	for a in affected:
		a["work_order"] = wo_map.get(a["voucher"])
		if a.get("work_order"):
			work_orders.add(a["work_order"])

	final_c = current_series[-1] if current_series else {}
	final_p = proposed_series[-1] if proposed_series else {}
	expected_bin = {
		"actual_qty": flt(final_p.get("qty_after_transaction")),
		"stock_value": flt(final_p.get("stock_value")),
		"valuation_rate": flt(final_p.get("valuation_rate")),
	}

	# Idempotence: re-run proposed simulation
	proposed_series_2 = replay_series(ordered, opening_qty, opening_value)
	idempotent = _series_equal(proposed_series, proposed_series_2)

	return {
		"ok": True,
		"dry_run": True,
		"item": item_code,
		"warehouse": warehouse,
		"from_datetime": str(from_dt),
		"opening_qty": float(opening_qty),
		"opening_value": float(opening_value),
		"previous_voucher": prev.voucher_no if prev else None,
		"row_count": len(rows),
		"proposed_times": {k: str(v) for k, v in proposed_times.items()},
		"first_divergence": first_divergence,
		"downstream_affected_sle": affected,
		"affected_batches": sorted(batches),
		"affected_vouchers": sorted(vouchers),
		"affected_work_orders": sorted(work_orders),
		"negative_qty_vouchers": sorted(set(neg_qty)),
		"negative_incoming_vouchers": sorted(set(neg_incoming)),
		"exploded_rate_vouchers": sorted(set(exploded)),
		"final_qty_current": flt(final_c.get("qty_after_transaction")),
		"final_qty_proposed": flt(final_p.get("qty_after_transaction")),
		"final_qty_unchanged": abs(flt(final_c.get("qty_after_transaction")) - flt(final_p.get("qty_after_transaction")))
		<= 0.0001,
		"expected_bin": expected_bin,
		"expected_gl_impact": "selective_rebuild_if_valuation_changed",
		"idempotent": idempotent,
	}


def _row_as_poison_probe(row, step: dict):
	"""Build a lightweight object for sle_poison_reason using proposed step values."""

	class _R:
		pass

	r = _R()
	qty = flt(row.actual_qty)
	svd = flt(step.get("stock_value_difference"))
	r.actual_qty = qty
	r.incoming_rate = (svd / qty) if qty > 0 else 0
	r.outgoing_rate = (abs(svd / qty) if qty < 0 else 0)
	r.stock_value_difference = svd
	r.stock_value = step.get("stock_value")
	r.valuation_rate = step.get("valuation_rate")
	r.qty_after_transaction = step.get("qty_after_transaction")
	return r


def _series_equal(a, b) -> bool:
	if len(a) != len(b):
		return False
	for x, y in zip(a, b, strict=False):
		for f in ("qty_after_transaction", "stock_value", "valuation_rate", "stock_value_difference"):
			if abs(flt(x.get(f)) - flt(y.get(f))) > 0.01:
				return False
	return True


def _work_orders(vouchers: list[str]) -> dict:
	if not vouchers:
		return {}
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT name, work_order FROM `tabStock Entry`
		WHERE name IN %s AND IFNULL(work_order,'')!=''
		""",
		(tuple(vouchers),),
		as_dict=True,
	)
	return {r.name: r.work_order for r in rows}
