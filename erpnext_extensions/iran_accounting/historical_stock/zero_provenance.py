# Copyright (c) 2026, ERPNext Extensions contributors
"""Zero-valuation provenance — current economic state, not rate==0 alone.

v5.3.4. Distinguishes a legitimately zero-valued current lot from leftover-MA
poison and from unproven/opening-state zeros.

An older depleted lot with a nonzero rate is NEVER by itself evidence that the
current lot must carry that rate.
"""

from __future__ import annotations

from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	QTY_EPS,
	RATE_EPS,
	VALUE_EPS,
	ZP_DEPLETED_OLD_VALUE_HISTORY,
	ZP_PROVEN_LEGITIMATE_ZERO,
	ZP_UNPROVEN_ZERO,
	ZP_VALUED_STOCK_ZERO_STAMP,
	ZP_ZERO_INBOUND_ON_VALUED_POSITION,
)


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def fetch_identity_chain(item, warehouse, *, as_of=None) -> list[dict]:
	"""Active SLE for item+warehouse, optionally clipped at as_of (exclusive of later)."""
	import frappe

	if not item or not warehouse:
		return []
	conds = ["item_code=%s", "warehouse=%s", "is_cancelled=0"]
	args: list = [item, warehouse]
	if as_of:
		conds.append("posting_datetime <= %s")
		args.append(get_datetime(as_of))
	rows = frappe.db.sql(
		f"""
		SELECT name, voucher_type, voucher_no, posting_datetime, actual_qty,
		       qty_after_transaction, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value, stock_value_difference, batch_no, creation
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		""",
		args,
		as_dict=True,
	)
	return list(rows or [])


def _effective_ma(qty, value) -> float:
	qty = flt(qty)
	value = flt(value)
	if abs(qty) <= QTY_EPS:
		return 0.0
	return flt(value / qty)


def _is_depleted(qty, value) -> bool:
	return abs(flt(qty)) <= QTY_EPS and abs(flt(value)) <= VALUE_EPS


def classify_zero_provenance(
	*,
	item=None,
	warehouse=None,
	as_of=None,
	rows=None,
) -> dict:
	"""Classify CURRENT economic zero provenance from a complete SLE chain.

	``rows`` may be injected for unit tests (no DB). Live callers omit it.
	"""
	chain = list(rows or [])
	if not chain and item and warehouse:
		chain = fetch_identity_chain(item, warehouse, as_of=as_of)
	if not chain:
		return {
			"item": item,
			"warehouse": warehouse,
			"provenance": ZP_UNPROVEN_ZERO,
			"allow_zero_outgoing": False,
			"block_zero_outgoing": True,
			"depleted_old_value_history": False,
			"reason": "NO_SLE_CHAIN",
			"current_qty": 0.0,
			"current_stock_value": 0.0,
			"current_effective_ma": 0.0,
			"last_depletion_voucher": None,
			"zero_inbound_voucher": None,
			"valued_inbound_after_depletion": False,
			"confidence": "AMBIGUOUS",
		}

	last = chain[-1]
	current_qty = flt(_g(last, "qty_after_transaction"))
	current_value = flt(_g(last, "stock_value"))
	current_vr = flt(_g(last, "valuation_rate"))
	current_ma = _effective_ma(current_qty, current_value)

	last_depletion = None
	last_depletion_idx = None
	had_valued_lot = False
	for i, row in enumerate(chain):
		qty = flt(_g(row, "qty_after_transaction"))
		value = flt(_g(row, "stock_value"))
		if abs(flt(_g(row, "valuation_rate"))) > VALUE_EPS or abs(flt(_g(row, "incoming_rate"))) > VALUE_EPS:
			if not _is_depleted(qty, value):
				had_valued_lot = True
		if _is_depleted(qty, value):
			last_depletion = row
			last_depletion_idx = i

	after = chain[last_depletion_idx + 1 :] if last_depletion_idx is not None else list(chain)
	zero_inbounds = []
	valued_inbounds = []
	for row in after:
		aq = flt(_g(row, "actual_qty"))
		if aq <= QTY_EPS:
			continue
		ir = abs(flt(_g(row, "incoming_rate")))
		svd = abs(flt(_g(row, "stock_value_difference")))
		if ir <= RATE_EPS and svd <= VALUE_EPS:
			zero_inbounds.append(row)
		elif ir > VALUE_EPS or svd > VALUE_EPS:
			valued_inbounds.append(row)

	depleted_old = bool(last_depletion is not None and had_valued_lot)

	# Opening-state / incomplete: first SLE is already an issue or qty/value disagree
	# with a missing inbound. Conservative.
	first = chain[0]
	opening_incomplete = (
		flt(_g(first, "actual_qty")) < -QTY_EPS
		and abs(flt(_g(first, "qty_after_transaction"))) > QTY_EPS
		and last_depletion is None
	)

	if opening_incomplete:
		return _pack(
			item,
			warehouse,
			ZP_UNPROVEN_ZERO,
			allow=False,
			block=True,
			depleted_old=depleted_old,
			reason="OPENING_STATE_INCOMPLETE",
			current_qty=current_qty,
			current_value=current_value,
			current_ma=current_ma,
			current_vr=current_vr,
			last_depletion=last_depletion,
			zero_inbounds=zero_inbounds,
			valued_inbounds=valued_inbounds,
			confidence="AMBIGUOUS",
		)

	# Current lot is economically zero after a clean depletion + only zero inbounds.
	if (
		current_qty > QTY_EPS
		and abs(current_value) <= VALUE_EPS
		and last_depletion is not None
		and not valued_inbounds
		and zero_inbounds
	):
		return _pack(
			item,
			warehouse,
			ZP_PROVEN_LEGITIMATE_ZERO,
			allow=True,
			block=False,
			depleted_old=depleted_old,
			reason="DEPLETED_THEN_ZERO_INBOUND_ONLY",
			current_qty=current_qty,
			current_value=current_value,
			current_ma=current_ma,
			current_vr=current_vr,
			last_depletion=last_depletion,
			zero_inbounds=zero_inbounds,
			valued_inbounds=valued_inbounds,
			confidence="EXACT",
			secondary=ZP_DEPLETED_OLD_VALUE_HISTORY if depleted_old else None,
		)

	# Entire chain never had value and current value is 0.
	if current_qty > QTY_EPS and abs(current_value) <= VALUE_EPS and not had_valued_lot and not valued_inbounds:
		return _pack(
			item,
			warehouse,
			ZP_PROVEN_LEGITIMATE_ZERO,
			allow=True,
			block=False,
			depleted_old=False,
			reason="NEVER_VALUED_ZERO_CHAIN",
			current_qty=current_qty,
			current_value=current_value,
			current_ma=current_ma,
			current_vr=current_vr,
			last_depletion=last_depletion,
			zero_inbounds=zero_inbounds,
			valued_inbounds=valued_inbounds,
			confidence="EXACT",
		)

	# Zero inbound on a still-valued position (leftover-MA root).
	zero_on_valued = None
	for i, row in enumerate(chain):
		aq = flt(_g(row, "actual_qty"))
		if aq <= QTY_EPS:
			continue
		ir = abs(flt(_g(row, "incoming_rate")))
		svd = abs(flt(_g(row, "stock_value_difference")))
		if ir > RATE_EPS or svd > VALUE_EPS:
			continue
		if i == 0:
			continue
		prev = chain[i - 1]
		pq = flt(_g(prev, "qty_after_transaction"))
		pv = flt(_g(prev, "stock_value"))
		if pq > QTY_EPS and pv > VALUE_EPS:
			zero_on_valued = {
				"voucher": _g(row, "voucher_no"),
				"previous_qty": pq,
				"previous_stock_value": pv,
				"previous_ma": _effective_ma(pq, pv),
				"incoming_qty": aq,
				"expected_qty": pq + aq,
				"expected_stock_value": pv,
				"expected_ma": _effective_ma(pq + aq, pv),
			}
			break

	if current_qty > QTY_EPS and current_value > VALUE_EPS and abs(current_vr) <= RATE_EPS:
		return _pack(
			item,
			warehouse,
			ZP_VALUED_STOCK_ZERO_STAMP,
			allow=False,
			block=True,
			depleted_old=depleted_old,
			reason="QTY_AND_VALUE_BUT_ZERO_STAMP",
			current_qty=current_qty,
			current_value=current_value,
			current_ma=current_ma,
			current_vr=current_vr,
			last_depletion=last_depletion,
			zero_inbounds=zero_inbounds,
			valued_inbounds=valued_inbounds,
			confidence="EXACT",
			zero_on_valued=zero_on_valued,
			secondary=ZP_ZERO_INBOUND_ON_VALUED_POSITION if zero_on_valued else None,
		)

	if zero_on_valued:
		return _pack(
			item,
			warehouse,
			ZP_ZERO_INBOUND_ON_VALUED_POSITION,
			allow=False,
			block=True,
			depleted_old=depleted_old,
			reason="ZERO_INBOUND_ON_VALUED_STOCK",
			current_qty=current_qty,
			current_value=current_value,
			current_ma=current_ma,
			current_vr=current_vr,
			last_depletion=last_depletion,
			zero_inbounds=zero_inbounds,
			valued_inbounds=valued_inbounds,
			confidence="EXACT",
			zero_on_valued=zero_on_valued,
		)

	if current_qty > QTY_EPS and current_value > VALUE_EPS and abs(current_ma) > RATE_EPS:
		# Healthy valued stock — outgoing zero is a lost-rate case, not provenance-allow.
		return _pack(
			item,
			warehouse,
			ZP_UNPROVEN_ZERO,
			allow=False,
			block=True,
			depleted_old=depleted_old,
			reason="CURRENT_STOCK_IS_VALUED",
			current_qty=current_qty,
			current_value=current_value,
			current_ma=current_ma,
			current_vr=current_vr,
			last_depletion=last_depletion,
			zero_inbounds=zero_inbounds,
			valued_inbounds=valued_inbounds,
			confidence="EXACT",
		)

	return _pack(
		item,
		warehouse,
		ZP_UNPROVEN_ZERO,
		allow=False,
		block=True,
		depleted_old=depleted_old,
		reason="UNPROVEN_ZERO_CHAIN",
		current_qty=current_qty,
		current_value=current_value,
		current_ma=current_ma,
		current_vr=current_vr,
		last_depletion=last_depletion,
		zero_inbounds=zero_inbounds,
		valued_inbounds=valued_inbounds,
		confidence="AMBIGUOUS",
	)


def _pack(
	item,
	warehouse,
	provenance,
	*,
	allow,
	block,
	depleted_old,
	reason,
	current_qty,
	current_value,
	current_ma,
	current_vr,
	last_depletion,
	zero_inbounds,
	valued_inbounds,
	confidence,
	zero_on_valued=None,
	secondary=None,
) -> dict:
	return {
		"item": item,
		"warehouse": warehouse,
		"provenance": provenance,
		"secondary_provenance": secondary,
		"allow_zero_outgoing": bool(allow),
		"block_zero_outgoing": bool(block),
		"depleted_old_value_history": bool(depleted_old),
		"reason": reason,
		"current_qty": flt(current_qty),
		"current_stock_value": flt(current_value),
		"current_valuation_rate": flt(current_vr),
		"current_effective_ma": flt(current_ma),
		"last_depletion_voucher": _g(last_depletion, "voucher_no") if last_depletion else None,
		"zero_inbound_voucher": _g(zero_inbounds[0], "voucher_no") if zero_inbounds else None,
		"zero_inbound_vouchers": [_g(r, "voucher_no") for r in zero_inbounds],
		"valued_inbound_after_depletion": bool(valued_inbounds),
		"zero_on_valued": zero_on_valued,
		"confidence": confidence,
	}


def provenance_as_of(item, warehouse, posting_date, posting_time) -> dict:
	as_of = None
	if posting_date:
		as_of = f"{posting_date} {posting_time or '00:00:00'}"
	return classify_zero_provenance(item=item, warehouse=warehouse, as_of=as_of)
