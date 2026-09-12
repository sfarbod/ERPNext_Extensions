# Copyright (c) 2026, ERPNext Extensions contributors
"""Quantity / valuation simulation of SLE order (no writes)."""

from __future__ import annotations

from decimal import Decimal

from erpnext_extensions.iran_accounting.stock_posting_order.ordering import sle_sort_key


def D(x) -> Decimal:
	if x is None or x == "":
		return Decimal("0")
	if isinstance(x, Decimal):
		return x
	return Decimal(str(x))


def running_qty(sles: list, *, opening: Decimal | int | float = 0) -> dict:
	"""Replay ``actual_qty`` in the given list order.

	Returns min running qty, final qty, and per-row running series.
	"""
	run = D(opening)
	minimum = run
	series = []
	for row in sles:
		qty = D(_g(row, "actual_qty"))
		run += qty
		if run < minimum:
			minimum = run
		series.append(
			{
				"voucher_no": _g(row, "voucher_no"),
				"actual_qty": qty,
				"running_qty": run,
				"posting_datetime": str(_g(row, "posting_datetime") or ""),
				"creation": str(_g(row, "creation") or ""),
			}
		)
	return {
		"min_qty": minimum,
		"final_qty": run,
		"series": series,
	}


def order_sles(sles: list, *, proposed_times: dict | None = None) -> list:
	"""Sort by proposed posting datetime when provided, else native SLE key."""
	if not proposed_times:
		return sorted(sles, key=sle_sort_key)

	def key(row):
		voucher = _g(row, "voucher_no")
		dt = proposed_times.get(voucher)
		if dt is None:
			dt = _g(row, "posting_datetime")
		return (str(dt), str(_g(row, "creation") or ""), str(_g(row, "name") or ""))

	return sorted(sles, key=key)


def simulate_repair(sles: list, proposed_times: dict, *, opening: Decimal | int | float = 0) -> dict:
	"""Compare current vs proposed running qty.

	Eligible only if:
	- current min qty < 0 due to ordering
	- proposed min qty >= 0
	- final qty unchanged
	"""
	current = running_qty(order_sles(sles), opening=opening)
	proposed = running_qty(order_sles(sles, proposed_times=proposed_times), opening=opening)
	final_unchanged = current["final_qty"] == proposed["final_qty"]
	qty_only_negative = current["min_qty"] < 0
	proposed_ok = proposed["min_qty"] >= 0
	insufficient = (not proposed_ok) and final_unchanged
	eligible = bool(qty_only_negative and proposed_ok and final_unchanged)
	return {
		"current": current,
		"proposed": proposed,
		"final_qty_unchanged": final_unchanged,
		"eligible": eligible,
		"insufficient_stock": bool(insufficient and current["min_qty"] < 0 and proposed["min_qty"] < 0),
		"no_qty_deficit": not qty_only_negative,
	}


def classify_valuation_impact(current_sles: list, proposed_sles: list) -> str:
	"""QUANTITY-ONLY when the signed qty sequence per voucher is the only change
	and incoming rates of inbound rows match. VALUATION-IMPACTING otherwise.
	"""
	cur_order = [(_g(r, "voucher_no"), D(_g(r, "actual_qty"))) for r in current_sles]
	prop_order = [(_g(r, "voucher_no"), D(_g(r, "actual_qty"))) for r in proposed_sles]
	if cur_order == prop_order:
		return "QUANTITY-ONLY"
	# Reorder of inbound vs outbound on a moving-average warehouse is valuation-impacting
	# unless every inbound incoming_rate equals the previous valuation_rate.
	rates = [D(_g(r, "incoming_rate")) for r in current_sles if D(_g(r, "actual_qty")) > 0]
	vals = [D(_g(r, "valuation_rate")) for r in current_sles if D(_g(r, "actual_qty")) < 0]
	if rates and vals and all(r == vals[0] for r in rates) and len(set(vals)) == 1 and rates[0] == vals[0]:
		return "QUANTITY-ONLY"
	return "VALUATION-IMPACTING"


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)
