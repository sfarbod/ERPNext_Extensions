# Copyright (c) 2026, ERPNext Extensions contributors
"""Same-time stock ordering optimizer (historical repair).

HISTORICAL REPAIR: change timestamps only when the current ERPNext
``(posting_datetime, creation)`` sequence creates a temporary negative, and
then only with the minimum seconds that keep running qty >= 0 and final qty
unchanged.

FUTURE PREVENTION (see prevention.py): proven prerequisite → dependent still
gets an explicit chronological order even if opening stock would cover it.
"""

from __future__ import annotations

from itertools import product

from erpnext_extensions.iran_accounting.stock_posting_order import MAX_OFFSET_SECONDS
from erpnext_extensions.iran_accounting.stock_posting_order.dag import CycleError, topological_levels
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	add_seconds,
	crosses_posting_date,
	format_datetime,
	sle_sort_key,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D
from frappe.utils import get_datetime


def minimum_seconds_label(status: str, seconds) -> str:
	if status == "NO_REPAIR_NEEDED":
		return "Repair unnecessary"
	if seconds is None:
		return ""
	try:
		sec = int(seconds)
	except (TypeError, ValueError):
		return ""
	if sec == 0:
		return "No change"
	if sec == 1:
		return "+1 second"
	return f"+{sec} seconds"


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _qty(row):
	return D(_g(row, "actual_qty"))


def _voucher(row) -> str:
	return str(_g(row, "voucher_no") or "")


def _creation(row) -> str:
	return str(_g(row, "creation") or "")


def order_with_times(sles: list, times: dict | None = None) -> list:
	if not times:
		return sorted(sles, key=sle_sort_key)

	def key(row):
		vn = _voucher(row)
		dt = times.get(vn)
		if dt is None:
			dt = _g(row, "posting_datetime")
		return (str(dt), _creation(row), str(_g(row, "name") or ""))

	return sorted(sles, key=key)


def simulate_running(sles: list, opening=0, times: dict | None = None, *, preserve_order: bool = False) -> dict:
	ordered = list(sles) if preserve_order else order_with_times(sles, times)
	run = D(opening)
	minimum = run
	series = []
	for row in ordered:
		before = run
		qty = _qty(row)
		run += qty
		if run < minimum:
			minimum = run
		proposed = times.get(_voucher(row)) if times else _g(row, "posting_datetime")
		series.append(
			{
				"name": _g(row, "name"),
				"voucher_no": _voucher(row),
				"voucher_type": _g(row, "voucher_type") or "Stock Entry",
				"actual_qty": qty,
				"running_qty_before": before,
				"running_qty_after": run,
				"posting_datetime": str(proposed or ""),
				"creation": _creation(row),
				"current_time": str(_g(row, "posting_datetime") or ""),
				"proposed_time": str(proposed or ""),
			}
		)
	return {
		"min_qty": minimum,
		"final_qty": run,
		"series": series,
		"ordered": ordered,
	}


def greedy_inward_first(sles: list) -> list:
	"""Stock-safe permutation: all inwards before outwards, stable by creation."""
	ins = sorted([r for r in sles if _qty(r) > 0], key=_creation)
	outs = sorted([r for r in sles if _qty(r) <= 0], key=_creation)
	return ins + outs


def deps_satisfied(assignment: dict, edges: list, creation_of: dict) -> bool:
	for src, dst in edges:
		os_ = assignment.get(src, 0)
		od = assignment.get(dst, 0)
		if os_ < od:
			continue
		if os_ > od:
			return False
		if creation_of.get(src, "") >= creation_of.get(dst, "\uffff"):
			return False
	return True


def _inversions(assignment: dict, creation_of: dict) -> int:
	vouchers = list(assignment)
	inv = 0
	for i, a in enumerate(vouchers):
		for b in vouchers[i + 1 :]:
			ca, cb = creation_of.get(a, ""), creation_of.get(b, "")
			oa, ob = assignment[a], assignment[b]
			if ca <= cb and (oa, ca) > (ob, cb):
				inv += 1
			if ca > cb and (oa, ca) < (ob, cb):
				inv += 1
	return inv


def _simulate_with_collisions(sles, opening, times, collisions, assignment, base_dt) -> dict:
	"""Include only collisions that share a second with a placed voucher.

	Independent later negatives must not block a locally safe group repair.
	Running qty still walks those collisions so placement after them is honest.
	"""
	touched = set()
	for v, o in (assignment or {}).items():
		touched.add(format_datetime(add_seconds(base_dt, o)))

	window = list(sles)
	for extra in collisions or []:
		window.append(extra)
	ordered = order_with_times(window, times)
	run = D(opening)
	minimum = run
	touched_min = run
	series = []
	for row in ordered:
		before = run
		qty = _qty(row)
		run += qty
		dt = times.get(_voucher(row)) if times and _voucher(row) in (times or {}) else _g(row, "posting_datetime")
		dt_s = format_datetime(get_datetime(dt)) if dt else ""
		if run < minimum:
			minimum = run
		if dt_s in touched and run < touched_min:
			touched_min = run
		series.append(
			{
				"name": _g(row, "name"),
				"voucher_no": _voucher(row),
				"actual_qty": qty,
				"running_qty_before": before,
				"running_qty_after": run,
				"proposed_time": str(dt or ""),
			}
		)
	return {
		"min_qty": touched_min,
		"window_min_qty": minimum,
		"final_qty": run,
		"series": series,
		"ordered": ordered,
	}


def _cross_item_safe(assignment, times, group_sles, cross_windows) -> bool:
	if not cross_windows or not any(assignment.values()):
		return True
	group_keys = {
		(
			_g(r, "item_code"),
			_g(r, "warehouse"),
			(_g(r, "canonical_batch") or _g(r, "batch_no") or ""),
		)
		for r in group_sles
	}
	for key, spec in (cross_windows or {}).items():
		if key in group_keys:
			continue
		rows = spec.get("sles") or []
		if not rows:
			continue
		opening = spec.get("opening") or 0
		cur = simulate_running(rows, opening)
		prop = simulate_running(rows, opening, times)
		if prop["min_qty"] < 0 and (cur["min_qty"] >= 0 or prop["min_qty"] < cur["min_qty"]):
			return False
	return True


def _stock_safe_voucher_order(sles, vouchers, edges, creation_of) -> list:
	net = {v: D(0) for v in vouchers}
	for row in sles:
		v = _voucher(row)
		if v in net:
			net[v] += _qty(row)
	ins = [v for v in vouchers if net[v] > 0]
	outs = [v for v in vouchers if v not in ins]
	ins.sort(key=lambda v: creation_of.get(v, ""))
	outs.sort(key=lambda v: creation_of.get(v, ""))
	preferred = ins + outs
	if not edges:
		return preferred
	try:
		topo = topological_levels(edges)
	except CycleError:
		return preferred
	rank = {v: i for i, v in enumerate(topo)}
	return sorted(preferred, key=lambda v: (rank.get(v, 10_000), 0 if net[v] > 0 else 1, creation_of.get(v, "")))


def _greedy_offsets(sles, vouchers, opening, edges, creation_of, max_seconds) -> dict | None:
	order = _stock_safe_voucher_order(sles, vouchers, edges, creation_of)
	offsets = {}
	prev = None
	for v in order:
		if prev is None:
			o = 0
		elif creation_of.get(prev, "") < creation_of.get(v, ""):
			o = offsets[prev]
		else:
			o = offsets[prev] + 1
		if o > int(max_seconds):
			return None
		offsets[v] = o
		prev = v
	for v in vouchers:
		offsets.setdefault(v, 0)
	if 0 not in offsets.values():
		return None
	if edges and not deps_satisfied(offsets, edges, creation_of):
		# bump dependents
		for src, dst in edges:
			need = offsets.get(src, 0)
			if offsets.get(dst, 0) < need or (
				offsets.get(dst, 0) == need and creation_of.get(src, "") >= creation_of.get(dst, "")
			):
				offsets[dst] = need + 1
				if offsets[dst] > int(max_seconds):
					return None
	return offsets


def _search_cap(n: int, max_seconds: int) -> int:
	if n <= 2:
		return int(max_seconds)
	if n == 3:
		return min(int(max_seconds), 12)
	return 0


def optimize_group(
	*,
	sles: list,
	opening,
	base_t,
	edges: list | None = None,
	collisions: list | None = None,
	cross_windows: dict | None = None,
	cross_item_sles: dict | None = None,
	cross_openings: dict | None = None,
	max_seconds: int = MAX_OFFSET_SECONDS,
	creation_of: dict | None = None,
) -> dict:
	"""Classify a same-time group and search the minimum-second assignment."""
	edges = list(edges or [])
	collisions = list(collisions or [])
	sles = list(sles)
	vouchers = []
	for row in sles:
		v = _voucher(row)
		if v and v not in vouchers:
			vouchers.append(v)
	creation_of = dict(creation_of or {})
	for row in sles:
		creation_of.setdefault(_voucher(row), _creation(row))

	if cross_item_sles and not cross_windows:
		cross_windows = {}
		for v, rows in cross_item_sles.items():
			for row in rows or []:
				key = (
					_g(row, "item_code"),
					_g(row, "warehouse"),
					(_g(row, "canonical_batch") or _g(row, "batch_no") or ""),
				)
				cross_windows.setdefault(key, {"sles": [], "opening": (cross_openings or {}).get(key, 0)})
				cross_windows[key]["sles"].append(row)

	current = simulate_running(sles, opening)
	final = current["final_qty"]
	if current["min_qty"] >= 0:
		return {
			"status": "NO_REPAIR_NEEDED",
			"minimum_seconds_required": 0,
			"moves": [],
			"assignment": {v: 0 for v in vouchers},
			"current": current,
			"proposed": current,
			"opening": D(opening),
			"final_qty_unchanged": True,
		}

	greedy_ordered = greedy_inward_first(sles)
	greedy = simulate_running(greedy_ordered, opening, preserve_order=True)
	greedy_ok = greedy["min_qty"] >= 0 and greedy["final_qty"] == final

	if not greedy_ok:
		return {
			"status": "REAL_STOCK_SHORTAGE",
			"minimum_seconds_required": None,
			"moves": [],
			"current": current,
			"proposed": greedy,
			"opening": D(opening),
			"final_qty_unchanged": greedy["final_qty"] == final,
		}

	if edges:
		try:
			topological_levels(edges)
		except CycleError:
			return {
				"status": "DEPENDENCY_CONFLICT",
				"minimum_seconds_required": None,
				"moves": [],
				"current": current,
				"opening": D(opening),
				"cycle": True,
			}
		net = {v: D(0) for v in vouchers}
		for row in sles:
			v = _voucher(row)
			if v in net:
				net[v] += _qty(row)
		for src, dst in edges:
			if net.get(src, 0) < 0 and net.get(dst, 0) > 0:
				return {
					"status": "DEPENDENCY_CONFLICT",
					"minimum_seconds_required": None,
					"moves": [],
					"current": current,
					"opening": D(opening),
				}

	base_dt = get_datetime(base_t)
	n = len(vouchers)
	if n == 0:
		return {"status": "NO_REPAIR_NEEDED", "minimum_seconds_required": 0, "current": current}

	best = None
	saw_dep_ok = False
	saw_midnight = False
	saw_cross = False
	cap = _search_cap(n, max_seconds)

	def consider(assignment):
		nonlocal best, saw_dep_ok, saw_midnight, saw_cross
		if edges and not deps_satisfied(assignment, edges, creation_of):
			return
		saw_dep_ok = True
		times = {v: add_seconds(base_dt, o) for v, o in assignment.items()}
		if any(crosses_posting_date(base_dt, times[v]) for v in vouchers if assignment[v]):
			saw_midnight = True
			return
		group_only = simulate_running(sles, opening, times)
		if group_only["final_qty"] != final:
			return
		if group_only["min_qty"] < 0:
			return
		window = _simulate_with_collisions(sles, opening, times, collisions, assignment, base_dt)
		if window["min_qty"] < 0:
			return
		if not _cross_item_safe(assignment, times, sles, cross_windows):
			saw_cross = True
			return
		offs = [assignment[v] for v in vouchers]
		score = (
			sum(1 for o in offs if o),
			sum(offs),
			max(offs) if offs else 0,
			_inversions(assignment, creation_of),
		)
		if best is None or score < best[0]:
			best = (score, assignment, times, window, group_only)

	if cap > 0:
		for max_s in range(0, cap + 1):
			for offs in product(range(0, max_s + 1), repeat=n):
				if max(offs) != max_s:
					continue
				if 0 not in offs:
					continue
				consider({vouchers[i]: offs[i] for i in range(n)})
			if best:
				# keep searching this max_s only; then continue to higher max_s
				# because fewer docs_changed at a higher max can score better.
				pass
	else:
		g = _greedy_offsets(sles, vouchers, opening, edges, creation_of, max_seconds)
		if g:
			consider(g)

	if best is None:
		g = _greedy_offsets(sles, vouchers, opening, edges, creation_of, max_seconds)
		if g:
			consider(g)

	if best:
		score, assignment, times, window, group_only = best
		moves = []
		for v, o in assignment.items():
			if o:
				moves.append(
					{
						"document": v,
						"old": format_datetime(base_dt),
						"new": format_datetime(times[v]),
						"seconds": o,
					}
				)
		return {
			"status": "REPAIRABLE_SECONDS",
			"minimum_seconds_required": score[2],
			"moves": moves,
			"assignment": assignment,
			"times": {v: format_datetime(t) for v, t in times.items()},
			"current": current,
			"proposed": group_only,
			"window": window,
			"opening": D(opening),
			"final_qty_unchanged": True,
			"docs_changed": score[0],
			"total_seconds_shifted": score[1],
		}

		if edges and not saw_dep_ok:
			return {
				"status": "DEPENDENCY_CONFLICT",
				"minimum_seconds_required": None,
				"moves": [],
				"current": current,
				"opening": D(opening),
			}
	if saw_cross:
		return {
			"status": "CROSS_ITEM_CONFLICT",
			"minimum_seconds_required": None,
			"moves": [],
			"current": current,
			"opening": D(opening),
		}
	if saw_midnight:
		return {
			"status": "MIDNIGHT_REVIEW",
			"minimum_seconds_required": None,
			"moves": [],
			"current": current,
			"opening": D(opening),
		}
	return {
		"status": "AMBIGUOUS_RELATIONSHIP",
		"minimum_seconds_required": None,
		"moves": [],
		"current": current,
		"opening": D(opening),
	}
