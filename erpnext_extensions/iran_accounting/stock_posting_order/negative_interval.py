# Copyright (c) 2026, ERPNext Extensions contributors
"""Detect temporary negatives caused by a later related inbound.

Same-second grouping cannot see Manufacture IN at T+71s after an MTfM OUT.
This detector walks each item+warehouse+canonical-batch series in ERPNext
order and classifies every negative running-qty interval.
"""

from __future__ import annotations

from datetime import timedelta

from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.stock_posting_order import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	STATUS_CROSS_ITEM_CONFLICT,
	STATUS_MIDNIGHT_REVIEW,
	STATUS_REAL_STOCK_SHORTAGE,
	STATUS_VALUATION_POISON,
)
from erpnext_extensions.iran_accounting.stock_posting_order.dependency import classify_edge
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import (
	minimum_seconds_label,
	simulate_running,
)
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	add_seconds,
	crosses_posting_date,
	format_datetime,
	sle_sort_key,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

# Measured windows for reporting; dependency evidence can exceed them.
SEARCH_WINDOW_SECONDS = (60, 5 * 60, 30 * 60)
QTY_EPS = D("0.0001")


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _qty(row):
	return D(_g(row, "actual_qty"))


def _dt(row):
	return get_datetime(_g(row, "posting_datetime"))


def walk_running(series: list, opening=0) -> list[dict]:
	"""ERPNext order: posting_datetime ASC, creation ASC. Uses actual_qty, not qty_after."""
	ordered = sorted(series, key=sle_sort_key)
	run = D(opening)
	out = []
	for row in ordered:
		before = run
		run += _qty(row)
		out.append(
			{
				"row": row,
				"running_qty_before": before,
				"running_qty_after": run,
			}
		)
	return out


def find_negative_intervals(series: list, opening=0) -> list[dict]:
	"""Each interval starts at the first SLE that drives running qty below 0."""
	walked = walk_running(series, opening)
	intervals = []
	i = 0
	while i < len(walked):
		step = walked[i]
		if step["running_qty_after"] >= 0 or step["running_qty_before"] < 0:
			i += 1
			continue
		start = i
		recover = None
		j = i + 1
		while j < len(walked):
			if walked[j]["running_qty_after"] >= 0:
				recover = j
				break
			j += 1
		inbounds = []
		end = recover if recover is not None else len(walked) - 1
		for k in range(start + 1, end + 1):
			if _qty(walked[k]["row"]) > 0:
				inbounds.append(walked[k])
		out_row = step["row"]
		in_row = walked[recover]["row"] if recover is not None else None
		gap = None
		if in_row is not None:
			gap = int((_dt(in_row) - _dt(out_row)).total_seconds())
		intervals.append(
			{
				"outbound": out_row,
				"inbound": in_row,
				"inbounds": [s["row"] for s in inbounds],
				"negative_start": _dt(out_row),
				"negative_amount": step["running_qty_after"],
				"previous_qty": step["running_qty_before"],
				"recovered": recover is not None,
				"gap_seconds": gap,
				"min_qty": min(s["running_qty_after"] for s in walked[start : end + 1]),
				"walked_start": start,
				"walked_end": end,
			}
		)
		i = recover + 1 if recover is not None else len(walked)
	return intervals


def _edge_for(inbound, outbound, against_map: dict | None) -> tuple[str, str]:
	against_map = against_map or {}
	against = bool(
		against_map.get((_g(outbound, "voucher_no"), _g(inbound, "voucher_no")))
	)
	same_wo = bool(_g(inbound, "work_order") and _g(inbound, "work_order") == _g(outbound, "work_order"))
	same_jc = bool(_g(inbound, "job_card") and _g(inbound, "job_card") == _g(outbound, "job_card"))
	in_batch = (_g(inbound, "canonical_batch") or _g(inbound, "batch_no") or "").strip()
	out_batch = (_g(outbound, "canonical_batch") or _g(outbound, "batch_no") or "").strip()
	same_batch = bool(in_batch) and in_batch == out_batch
	return classify_edge(
		{
			"item_code": _g(inbound, "item_code"),
			"warehouse": _g(inbound, "warehouse"),
			"batch_no": in_batch,
		},
		{
			"item_code": _g(outbound, "item_code"),
			"warehouse": _g(outbound, "warehouse"),
			"batch_no": out_batch,
		},
		same_work_order=same_wo,
		same_job_card=same_jc,
		against_stock_entry=against,
		same_batch=same_batch,
		inbound_purpose=_g(inbound, "purpose"),
		outbound_purpose=_g(outbound, "purpose"),
	)


def _unrelated(confidence: str, inbound, outbound) -> bool:
	if confidence in (CONFIDENCE_EXACT, CONFIDENCE_LIKELY):
		return False
	if _g(inbound, "work_order") and _g(inbound, "work_order") == _g(outbound, "work_order"):
		return False
	in_b = (_g(inbound, "canonical_batch") or _g(inbound, "batch_no") or "").strip()
	out_b = (_g(outbound, "canonical_batch") or _g(outbound, "batch_no") or "").strip()
	if in_b and in_b == out_b:
		return False
	return True


_CONF_RANK = {CONFIDENCE_EXACT: 0, CONFIDENCE_LIKELY: 1, CONFIDENCE_AMBIGUOUS: 2}


def _pick_inbound(interval: dict, against_map: dict | None):
	"""Prefer a causally related inbound over whichever row first restores qty."""
	outbound = interval["outbound"]
	recovery = interval.get("inbound")
	candidates = list(interval.get("inbounds") or [])
	if recovery is not None and recovery not in candidates:
		candidates.append(recovery)
	if not candidates:
		return None, CONFIDENCE_AMBIGUOUS, "no_inbound"
	best = None
	for inb in candidates:
		conf, reason = _edge_for(inb, outbound, against_map)
		rank = _CONF_RANK.get(conf, 9)
		key = (rank, 0 if inb is recovery else 1)
		if best is None or key < best[0]:
			best = (key, inb, conf, reason)
	return best[1], best[2], best[3]


def search_window_label(gap_seconds) -> str:
	if gap_seconds is None:
		return "unrecovered"
	gap = int(gap_seconds)
	if gap <= 60:
		return "60s"
	if gap <= 5 * 60:
		return "5min"
	if gap <= 30 * 60:
		return "30min"
	return "beyond_30min"


def _occupied_seconds(series: list, exclude_vouchers: set) -> set:
	out = set()
	for row in series:
		if _g(row, "voucher_no") in exclude_vouchers:
			continue
		dt = _dt(row)
		out.add(dt.replace(microsecond=0))
	return out


def _next_free(inbound_dt, occupied: set, outbound_dt):
	"""Outbound must be strictly after inbound; same-second would still invert on creation.

	Moving outbound onto the inbound's later calendar date is allowed — that is the
	classic CROSS_TIME repair when the inbound already posts on a later day.

	True MIDNIGHT_REVIEW only when searching free seconds would leave the *inbound*
	posting date (e.g. inbound at 23:59:59 with no free second that day).
	"""
	dt = add_seconds(inbound_dt.replace(microsecond=0), 1)
	if crosses_posting_date(inbound_dt, dt):
		return None, True
	while dt in occupied:
		dt = add_seconds(dt, 1)
		if crosses_posting_date(inbound_dt, dt):
			return None, True
	return dt, False


def propose_outbound_after_inbound(interval: dict, series: list) -> dict:
	"""Keep prerequisite inbound time; move dependent outbound to inbound+min seconds."""
	inbound = interval["inbound"]
	outbound = interval["outbound"]
	in_dt = _dt(inbound)
	out_dt = _dt(outbound)
	occupied = _occupied_seconds(series, {_g(outbound, "voucher_no")})
	proposed, midnight = _next_free(in_dt, occupied, out_dt)
	if midnight or proposed is None:
		return {"ok": False, "status": STATUS_MIDNIGHT_REVIEW}
	seconds = int((proposed - out_dt).total_seconds())
	times = {
		_g(inbound, "voucher_no"): in_dt,
		_g(outbound, "voucher_no"): proposed,
	}
	current = simulate_running(series, 0)
	proposed_sim = simulate_running(series, 0, times)
	if proposed_sim["min_qty"] < 0:
		return {"ok": False, "status": STATUS_REAL_STOCK_SHORTAGE, "proposed": proposed_sim, "current": current}
	if proposed_sim["final_qty"] != current["final_qty"]:
		return {"ok": False, "status": STATUS_REAL_STOCK_SHORTAGE, "proposed": proposed_sim, "current": current}
	return {
		"ok": True,
		"times": times,
		"proposed_outbound": proposed,
		"proposed_inbound": in_dt,
		"seconds_shifted": seconds,
		"docs_changed": 1,
		"current": current,
		"proposed": proposed_sim,
		"moves": [
			{
				"document": _g(outbound, "voucher_no"),
				"old": format_datetime(out_dt),
				"new": format_datetime(proposed),
				"seconds": seconds,
			}
		],
	}


def _cross_item_safe(outbound_voucher, times: dict, by_voucher_all: dict, by_identity: dict) -> bool:
	"""Moving a parent voucher must not create a new negative on another identity."""
	if not by_voucher_all or outbound_voucher not in by_voucher_all:
		return True
	touched = by_voucher_all.get(outbound_voucher) or []
	for row in touched:
		key = (
			_g(row, "item_code"),
			_g(row, "warehouse"),
			(_g(row, "canonical_batch") or _g(row, "batch_no") or ""),
		)
		series = by_identity.get(key) or []
		if not series:
			continue
		cur = simulate_running(series, 0)
		prop = simulate_running(series, 0, times)
		if prop["min_qty"] < 0 and (cur["min_qty"] >= 0 or prop["min_qty"] < cur["min_qty"]):
			return False
		if prop["final_qty"] != cur["final_qty"]:
			return False
	return True


def classify_interval(
	interval: dict,
	*,
	series: list,
	against_map: dict | None = None,
	by_voucher_all: dict | None = None,
	by_identity: dict | None = None,
	poisoned: bool = False,
) -> dict:
	outbound = interval["outbound"]
	inbound, confidence, reason = _pick_inbound(interval, against_map)
	interval = dict(interval)
	interval["inbound"] = inbound
	if inbound is not None:
		interval["gap_seconds"] = int((_dt(inbound) - _dt(outbound)).total_seconds())
	same_second = False
	if inbound is not None:
		same_second = format_datetime(_dt(inbound)) == format_datetime(_dt(outbound))

	base = {
		"detection": "SAME_TIME" if same_second else "CROSS_TIME",
		"negative_start": format_datetime(interval["negative_start"]),
		"negative_voucher": _g(outbound, "voucher_no"),
		"negative_amount": str(interval["negative_amount"]),
		"previous_qty": str(interval["previous_qty"]),
		"time_gap_seconds": interval.get("gap_seconds"),
		"later_inbound": _g(inbound, "voucher_no") if inbound else None,
		"inbound_count": len(interval.get("inbounds") or []),
		"search_window": search_window_label(interval.get("gap_seconds")),
	}
	if poisoned:
		return {**base, "status": STATUS_VALUATION_POISON, "confidence": CONFIDENCE_AMBIGUOUS, "eligible": False}

	if not interval["recovered"] or inbound is None:
		return {
			**base,
			"status": STATUS_REAL_STOCK_SHORTAGE,
			"confidence": CONFIDENCE_EXACT,
			"eligible": False,
			"dependency_reason": "never_recovered",
		}

	if _unrelated(confidence, inbound, outbound):
		# Ambiguous relationship — still attempt deterministic quantity repair when
		# the recovering inbound is an external stock source (Purchase / RECO).
		in_vt = str(_g(inbound, "voucher_type") or "")
		external = in_vt in ("Purchase Receipt", "Stock Reconciliation", "Purchase Invoice")
		if external:
			proposal = propose_outbound_after_inbound(interval, series)
			if proposal.get("ok"):
				if by_voucher_all and by_identity:
					if not _cross_item_safe(_g(outbound, "voucher_no"), proposal["times"], by_voucher_all, by_identity):
						return {
							**base,
							"status": STATUS_CROSS_ITEM_CONFLICT,
							"confidence": confidence,
							"dependency_reason": reason,
							"eligible": False,
						}
				return {
					**base,
					"status": "CROSS_TIME_REPAIRABLE",
					"optimizer_status": "CROSS_TIME_REPAIRABLE",
					"confidence": CONFIDENCE_LIKELY,
					"dependency_reason": reason or "external_inbound_recovers",
					"eligible": False,
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
		return {
			**base,
			"status": "LATER_INBOUND_UNRELATED",
			"confidence": confidence,
			"dependency_reason": reason,
			"eligible": False,
		}

	if confidence == CONFIDENCE_AMBIGUOUS:
		return {
			**base,
			"status": "AMBIGUOUS_DEPENDENCY",
			"confidence": confidence,
			"dependency_reason": reason,
			"eligible": False,
		}

	proposal = propose_outbound_after_inbound(interval, series)
	if not proposal.get("ok"):
		status = proposal.get("status") or STATUS_REAL_STOCK_SHORTAGE
		return {
			**base,
			"status": status,
			"confidence": confidence,
			"dependency_reason": reason,
			"eligible": False,
			"current": proposal.get("current"),
			"proposed": proposal.get("proposed"),
		}

	times = proposal["times"]
	if by_voucher_all and by_identity:
		if not _cross_item_safe(_g(outbound, "voucher_no"), times, by_voucher_all, by_identity):
			return {
				**base,
				"status": STATUS_CROSS_ITEM_CONFLICT,
				"confidence": confidence,
				"dependency_reason": reason,
				"eligible": False,
			}

	status = "SAME_TIME_REPAIRABLE" if same_second else "CROSS_TIME_REPAIRABLE"
	eligible = confidence == CONFIDENCE_EXACT
	return {
		**base,
		"status": status,
		"optimizer_status": status,
		"confidence": confidence,
		"dependency_reason": reason,
		"eligible": eligible,
		"moves": proposal["moves"],
		"seconds_shifted": proposal["seconds_shifted"],
		"minimum_seconds_required": proposal["seconds_shifted"],
		"minimum_seconds_label": minimum_seconds_label("REPAIRABLE_SECONDS", proposal["seconds_shifted"]),
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


def interval_to_scan_row(interval: dict, classified: dict) -> dict:
	outbound = interval["outbound"]
	wanted = classified.get("later_inbound")
	inbound = interval.get("inbound")
	if wanted:
		for r in list(interval.get("inbounds") or []) + [interval.get("inbound")]:
			if r is not None and _g(r, "voucher_no") == wanted:
				inbound = r
				break
	out_dt = _dt(outbound)
	in_dt = _dt(inbound) if inbound is not None else None
	ui_status = classified.get("status")
	if classified.get("eligible") and ui_status in ("CROSS_TIME_REPAIRABLE", "SAME_TIME_REPAIRABLE"):
		ui_status = "ELIGIBLE"
	payload = {
		"topic": "POSTING_ORDER",
		"detection": classified.get("detection"),
		"inbound_document": _g(inbound, "voucher_no") if inbound else None,
		"outbound_document": _g(outbound, "voucher_no"),
		"inbound_purpose": _g(inbound, "purpose") if inbound else None,
		"outbound_purpose": _g(outbound, "purpose"),
		"item": _g(outbound, "item_code"),
		"warehouse": _g(outbound, "warehouse"),
		"batch": _g(outbound, "canonical_batch") or _g(outbound, "batch_no"),
		"sabb_inbound": _g(inbound, "serial_and_batch_bundle") if inbound else None,
		"sabb_outbound": _g(outbound, "serial_and_batch_bundle"),
		"negative_start": classified.get("negative_start"),
		"negative_voucher": classified.get("negative_voucher"),
		"later_inbound": classified.get("later_inbound"),
		"time_gap_seconds": classified.get("time_gap_seconds"),
		"current_inbound_time": format_datetime(in_dt) if in_dt else "",
		"current_outbound_time": format_datetime(out_dt),
		"proposed_inbound_time": classified.get("proposed_inbound") or (format_datetime(in_dt) if in_dt else ""),
		"proposed_outbound_time": classified.get("proposed_outbound") or "",
		"opening_qty": str(interval["previous_qty"]),
		"min_qty_before": classified.get("min_qty_before") or str(interval["min_qty"]),
		"min_qty_after": classified.get("min_qty_after") or "",
		"final_qty_before": classified.get("final_qty_before") or "",
		"final_qty_after": classified.get("final_qty_after") or "",
		"minimum_seconds_required": classified.get("minimum_seconds_required"),
		"minimum_seconds_label": classified.get("minimum_seconds_label") or "",
		"seconds_shifted": classified.get("seconds_shifted"),
		"moves": classified.get("moves") or [],
		"valuation_impact": "QUANTITY-ONLY",
		"gl_impact": "NONE",
		"confidence": classified.get("confidence"),
		"dependency_reason": classified.get("dependency_reason"),
		"status": ui_status,
		"optimizer_status": classified.get("optimizer_status") or classified.get("status"),
		"inbound_modified": str(_g(inbound, "modified") or "") if inbound else "",
		"outbound_modified": str(_g(outbound, "modified") or ""),
		"work_order": _g(inbound, "work_order") or _g(outbound, "work_order"),
		"inbound_job_card": _g(inbound, "job_card") if inbound else None,
		"outbound_job_card": _g(outbound, "job_card"),
		"company": _g(outbound, "company"),
		"eligible": bool(classified.get("eligible")),
		"chain": f"{_g(inbound, 'voucher_no') if inbound else '?'} → {_g(outbound, 'voucher_no')}",
		"posting_date": str(_g(outbound, "posting_date") or ""),
		"has_batch": bool(_g(outbound, "canonical_batch") or _g(outbound, "batch_no")),
		"negative_amount": classified.get("negative_amount"),
		"inbound_count": classified.get("inbound_count"),
		"search_window": classified.get("search_window"),
		"search_windows_s": list(SEARCH_WINDOW_SECONDS),
	}
	return payload


def scan_series(
	series: list,
	*,
	against_map: dict | None = None,
	by_voucher_all: dict | None = None,
	by_identity: dict | None = None,
	poisoned: bool = False,
	skip_same_second: bool = True,
) -> list[dict]:
	rows = []
	for interval in find_negative_intervals(series, 0):
		outbound = interval["outbound"]
		if _g(outbound, "voucher_type") not in (None, "", "Stock Entry"):
			continue
		classified = classify_interval(
			interval,
			series=series,
			against_map=against_map,
			by_voucher_all=by_voucher_all,
			by_identity=by_identity,
			poisoned=poisoned,
		)
		if skip_same_second and classified.get("detection") == "SAME_TIME":
			continue
		row = interval_to_scan_row(interval, classified)
		# signature filled by scanner
		rows.append(row)
	return rows
