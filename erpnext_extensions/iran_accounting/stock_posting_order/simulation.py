# Copyright (c) 2026, ERPNext Extensions contributors
"""Quantity / valuation simulation of SLE order (no writes)."""

from __future__ import annotations

from decimal import Decimal

from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.stock_posting_order.ordering import sle_sort_key

QTY_EPS = Decimal("0.0001")


def D(x) -> Decimal:
	if x is None or x == "":
		return Decimal("0")
	if isinstance(x, Decimal):
		return x
	return Decimal(str(x))


def align_movements_to_qty_after(sles: list, *, opening: Decimal | int | float = 0) -> list:
	"""Return copies with ``actual_qty`` aligned to authoritative ``qty_after_transaction``.

	Opening Stock Reconciliation (and similar absolute-balance rows) may store
	``qty_after_transaction`` without a matching ``actual_qty`` (often 0). Summing
	raw ``actual_qty`` then falsely reports historical shortage. For chronological
	simulation we patch the effective movement to ``qty_after - qty_before``.
	"""
	ordered = sorted(sles, key=sle_sort_key)
	run = D(opening)
	out = []
	for row in ordered:
		r = dict(row) if not isinstance(row, dict) else dict(row)
		aq = D(_g(r, "actual_qty"))
		qa_raw = _g(r, "qty_after_transaction")
		if qa_raw is not None and qa_raw != "":
			qa = D(qa_raw)
			implied = qa - run
			if abs(implied - aq) > QTY_EPS:
				r["actual_qty"] = implied
				r["_original_actual_qty"] = aq
				r["_effective_qty_patched"] = True
				aq = implied
			run = qa
		else:
			run += aq
		out.append(r)
	return out


def opening_state_before(series: list, t_dt, t_creation=None) -> dict:
	"""Authoritative opening qty immediately before ``(t_dt, t_creation)``.

	Uses the last prior SLE's ``qty_after_transaction`` — never Bin.actual_qty
	and never assumes opening=0 when earlier ledger state exists.
	"""
	t_dt = get_datetime(t_dt)
	t_creation = str(t_creation or "")
	last = None
	for row in sorted(series, key=sle_sort_key):
		dt = get_datetime(_g(row, "posting_datetime"))
		cr = str(_g(row, "creation") or "")
		if dt < t_dt or (dt == t_dt and cr < t_creation):
			last = row
			continue
		if dt > t_dt:
			break
	if last is None:
		return {
			"opening_qty": D(0),
			"opening_stock_value": D(0),
			"opening_valuation_rate": D(0),
			"opening_source_voucher": None,
			"opening_source_voucher_type": None,
			"opening_source_datetime": None,
			"opening_source_sle": None,
			"opening_source": "EMPTY_LEDGER",
		}
	qa = _g(last, "qty_after_transaction")
	opening_qty = D(qa) if qa is not None and qa != "" else D(_g(last, "actual_qty"))
	# If prior rows had patched absolute balances, prefer walking aligned series.
	aligned = align_movements_to_qty_after(
		[r for r in series if _before_boundary(r, t_dt, t_creation)],
		opening=0,
	)
	if aligned:
		opening_qty = sum((D(_g(r, "actual_qty")) for r in aligned), D(0))
		# Prefer last qty_after when present (authoritative).
		last_qa = _g(aligned[-1], "qty_after_transaction")
		if last_qa is not None and last_qa != "":
			opening_qty = D(last_qa)
	sv = _g(last, "stock_value")
	vr = _g(last, "valuation_rate")
	return {
		"opening_qty": D(opening_qty),
		"opening_stock_value": D(sv) if sv is not None else D(0),
		"opening_valuation_rate": D(vr) if vr is not None else D(0),
		"opening_source_voucher": _g(last, "voucher_no"),
		"opening_source_voucher_type": _g(last, "voucher_type"),
		"opening_source_datetime": str(_g(last, "posting_datetime") or ""),
		"opening_source_sle": _g(last, "name"),
		"opening_source": _g(last, "voucher_type") or "SLE",
	}


def _before_boundary(row, t_dt, t_creation) -> bool:
	dt = get_datetime(_g(row, "posting_datetime"))
	cr = str(_g(row, "creation") or "")
	return dt < t_dt or (dt == t_dt and cr < str(t_creation or ""))


def shortage_evidence(
	*,
	opening: dict | None,
	qty_before,
	movement_qty,
	qty_after,
	min_historical_qty,
	first_negative_voucher=None,
	first_negative_datetime=None,
	identity_scope=None,
	batch=None,
	sabb=None,
	simulation_start=None,
	simulation_end=None,
) -> dict:
	"""Canonical shortage evidence fields (blocker / scan payload)."""
	op = opening or {}
	return {
		"opening_qty": str(D(op.get("opening_qty", 0))),
		"opening_source_voucher": op.get("opening_source_voucher"),
		"opening_source_voucher_type": op.get("opening_source_voucher_type"),
		"opening_source_datetime": op.get("opening_source_datetime"),
		"opening_source": op.get("opening_source"),
		"qty_before": str(D(qty_before)),
		"movement_qty": str(D(movement_qty)),
		"qty_after": str(D(qty_after)),
		"minimum_historical_qty": str(D(min_historical_qty)),
		"first_negative_voucher": first_negative_voucher,
		"first_negative_datetime": (
			str(first_negative_datetime) if first_negative_datetime is not None else None
		),
		"identity_scope": identity_scope,
		"batch": batch,
		"sabb": sabb,
		"simulation_start": str(simulation_start) if simulation_start is not None else None,
		"simulation_end": str(simulation_end) if simulation_end is not None else None,
	}


def is_authoritative_shortage(*, qty_after) -> bool:
	"""REAL_STOCK_SHORTAGE only when qty_after at the movement is negative."""
	return D(qty_after) < -QTY_EPS


def running_qty(sles: list, *, opening: Decimal | int | float = 0, align: bool = False) -> dict:
	"""Replay ``actual_qty`` in the given list order.

	When ``align=True``, patch movements to match ``qty_after_transaction`` first
	(Opening Stock RECO / absolute-balance rows).
	"""
	rows = align_movements_to_qty_after(sles, opening=opening) if align else sles
	run = D(opening)
	minimum = run
	series = []
	for row in rows:
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
