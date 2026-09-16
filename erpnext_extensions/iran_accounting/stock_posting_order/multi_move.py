# Copyright (c) 2026, ERPNext Extensions contributors
"""Multi-move posting-order optimizer — clear temporary negatives with several outs."""

from __future__ import annotations

from decimal import Decimal

from erpnext_extensions.iran_accounting.stock_posting_order import (
	CONFIDENCE_LIKELY,
	STATUS_CROSS_ITEM_CONFLICT,
	STATUS_MIDNIGHT_REVIEW,
	STATUS_REAL_STOCK_SHORTAGE,
)
from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
	_cross_item_safe,
	_dt,
	_g,
	_next_free,
	_occupied_seconds,
	_unrelated,
)
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import (
	minimum_seconds_label,
	simulate_running,
)
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	add_seconds,
	format_datetime,
)

D = Decimal
STATUS_MULTI_MOVE_REPAIRABLE = "MULTI_MOVE_REPAIRABLE"
EXTERNAL_VT = ("Purchase Receipt", "Stock Reconciliation", "Purchase Invoice")


def _qty(row) -> Decimal:
	return D(str(_g(row, "actual_qty") or 0))


def collect_negative_outbounds(series: list, inbound, outbound) -> list:
	"""All Stock Entry outs that post at/before ``outbound`` while running qty is negative,
	up to (but not including) the recovering ``inbound``.
	"""
	in_dt = _dt(inbound)
	out_vn = _g(outbound, "voucher_no")
	# Walk chronologically; track running qty; gather outs during deficit until inbound.
	ordered = sorted(series, key=lambda r: (_dt(r), str(_g(r, "creation") or ""), str(_g(r, "name") or "")))
	running = D(0)
	in_deficit = False
	outs = []
	seen = set()
	for row in ordered:
		dt = _dt(row)
		if dt >= in_dt and _g(row, "voucher_no") == _g(inbound, "voucher_no"):
			break
		q = _qty(row)
		running += q
		if q < 0 and running < 0:
			in_deficit = True
			vn = _g(row, "voucher_no")
			vt = str(_g(row, "voucher_type") or "Stock Entry")
			if vt == "Stock Entry" and vn and vn not in seen:
				outs.append(row)
				seen.add(vn)
		elif running >= 0:
			in_deficit = False
	# Always include the interval outbound
	if out_vn and out_vn not in seen:
		outs.append(outbound)
	return outs


def propose_multi_move_after_inbound(interval: dict, series: list) -> dict:
	"""Move every deficit outbound onto free seconds after the recovering inbound.

	Deterministic when a single later inbound recovers the identity and all
	Stock Entry outs in the negative window can be shifted after it without
	changing final qty.
	"""
	inbound = interval.get("inbound")
	outbound = interval.get("outbound")
	if inbound is None or outbound is None:
		return {"ok": False, "status": STATUS_REAL_STOCK_SHORTAGE}
	in_dt = _dt(inbound)
	outs = collect_negative_outbounds(series, inbound, outbound)
	if not outs:
		return {"ok": False, "status": STATUS_REAL_STOCK_SHORTAGE}

	exclude = {_g(r, "voucher_no") for r in outs}
	occupied = _occupied_seconds(series, exclude)
	cursor = in_dt
	times = {_g(inbound, "voucher_no"): in_dt}
	moves = []
	for row in outs:
		proposed, midnight = _next_free(cursor, occupied, _dt(row))
		if midnight or proposed is None:
			return {"ok": False, "status": STATUS_MIDNIGHT_REVIEW, "partial_outs": len(moves)}
		vn = _g(row, "voucher_no")
		times[vn] = proposed
		occupied.add(proposed.replace(microsecond=0))
		old = _dt(row)
		moves.append(
			{
				"document": vn,
				"old": format_datetime(old),
				"new": format_datetime(proposed),
				"seconds": int((proposed - old).total_seconds()),
			}
		)
		cursor = proposed  # next out after this one

	current = simulate_running(series, 0)
	proposed_sim = simulate_running(series, 0, times)
	if proposed_sim["min_qty"] < 0:
		return {
			"ok": False,
			"status": STATUS_REAL_STOCK_SHORTAGE,
			"proposed": proposed_sim,
			"current": current,
			"moves_tried": moves,
		}
	if proposed_sim["final_qty"] != current["final_qty"]:
		return {
			"ok": False,
			"status": STATUS_REAL_STOCK_SHORTAGE,
			"proposed": proposed_sim,
			"current": current,
			"moves_tried": moves,
		}
	primary = next((m for m in moves if m["document"] == _g(outbound, "voucher_no")), moves[-1])
	return {
		"ok": True,
		"status": STATUS_MULTI_MOVE_REPAIRABLE,
		"times": times,
		"proposed_outbound": times[_g(outbound, "voucher_no")],
		"proposed_inbound": in_dt,
		"seconds_shifted": primary["seconds"],
		"docs_changed": len(moves),
		"current": current,
		"proposed": proposed_sim,
		"moves": moves,
	}


def try_external_multi_move(
	interval: dict,
	*,
	series: list,
	confidence: str,
	reason: str | None,
	by_voucher_all: dict | None = None,
	by_identity: dict | None = None,
	base: dict | None = None,
) -> dict | None:
	"""If single-move failed, attempt multi-move after a recovering inbound.

	Applies when the inbound is an external stock source (PRE/RECO/PI) or the
	edge is already EXACT/LIKELY related. Returns classify-compatible dict or None.
	"""
	inbound = interval.get("inbound")
	outbound = interval.get("outbound")
	if inbound is None or outbound is None:
		return None
	in_vt = str(_g(inbound, "voucher_type") or "")
	external = in_vt in EXTERNAL_VT
	related = not _unrelated(confidence, inbound, outbound)
	if not external and not related:
		return None

	proposal = propose_multi_move_after_inbound(interval, series)
	if not proposal.get("ok"):
		return None

	if by_voucher_all and by_identity:
		for m in proposal["moves"]:
			if not _cross_item_safe(m["document"], proposal["times"], by_voucher_all, by_identity):
				return {
					**(base or {}),
					"status": STATUS_CROSS_ITEM_CONFLICT,
					"optimizer_status": STATUS_CROSS_ITEM_CONFLICT,
					"confidence": confidence,
					"dependency_reason": reason or "multi_move_cross_item",
					"eligible": False,
				}

	return {
		**(base or {}),
		"status": STATUS_MULTI_MOVE_REPAIRABLE,
		"optimizer_status": STATUS_MULTI_MOVE_REPAIRABLE,
		"confidence": CONFIDENCE_LIKELY if confidence not in ("EXACT",) else confidence,
		"dependency_reason": reason or "multi_move_after_inbound",
		"eligible": confidence == "EXACT",
		"moves": proposal["moves"],
		"seconds_shifted": proposal["seconds_shifted"],
		"minimum_seconds_required": proposal["seconds_shifted"],
		"minimum_seconds_label": minimum_seconds_label(
			"REPAIRABLE_SECONDS", proposal["seconds_shifted"]
		),
		"proposed_outbound": format_datetime(proposal["proposed_outbound"]),
		"proposed_inbound": format_datetime(proposal["proposed_inbound"]),
		"min_qty_before": str(proposal["current"]["min_qty"]),
		"min_qty_after": str(proposal["proposed"]["min_qty"]),
		"final_qty_before": str(proposal["current"]["final_qty"]),
		"final_qty_after": str(proposal["proposed"]["final_qty"]),
		"docs_changed": proposal["docs_changed"],
		"current": proposal["current"],
		"proposed": proposal["proposed"],
	}
