# Copyright (c) 2026, ERPNext Extensions contributors
"""Directed dependency graph, cycle detection, minimum +1s offsets."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from erpnext_extensions.iran_accounting.stock_posting_order import (
	MINIMUM_DEPENDENT_SECONDS,
	STATUS_CYCLE,
	STATUS_MIDNIGHT,
)
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	add_seconds,
	combine_posting,
	crosses_posting_date,
	format_datetime,
)


class CycleError(Exception):
	def __init__(self, nodes):
		self.nodes = list(nodes)
		super().__init__(f"dependency cycle: {self.nodes}")


def topological_levels(edges: list[tuple[str, str]]) -> list[str]:
	"""Return nodes in producer-before-consumer order. Raises CycleError."""
	nodes: set[str] = set()
	incoming: dict[str, int] = defaultdict(int)
	outgoing: dict[str, list[str]] = defaultdict(list)
	for src, dst in edges:
		nodes.add(src)
		nodes.add(dst)
		outgoing[src].append(dst)
		incoming[dst] += 1
		incoming.setdefault(src, incoming[src])
	ready = deque(n for n in nodes if incoming[n] == 0)
	order = []
	while ready:
		n = ready.popleft()
		order.append(n)
		for nxt in outgoing[n]:
			incoming[nxt] -= 1
			if incoming[nxt] == 0:
				ready.append(nxt)
	if len(order) != len(nodes):
		cycled = [n for n in nodes if incoming[n] > 0]
		raise CycleError(cycled)
	return order


def assign_minimum_offsets(
	nodes: dict[str, datetime],
	edges: list[tuple[str, str]],
	*,
	occupied: dict[str, set[datetime]] | None = None,
	stock_key_of: dict[str, tuple] | None = None,
	minimum_seconds: int = MINIMUM_DEPENDENT_SECONDS,
) -> dict:
	"""Preserve producer times; move dependents the least amount that keeps order.

	``occupied[stock_key]`` holds datetimes already used by *unrelated* documents
	on that item/warehouse/batch. Dependents skip those slots (T+1 occupied → T+2).
	Never shifts the unrelated document.
	"""
	try:
		order = topological_levels(edges)
	except CycleError as exc:
		return {
			"ok": False,
			"status": STATUS_CYCLE,
			"cycle": exc.nodes,
			"proposed": {},
			"moves": [],
		}

	proposed = dict(nodes)
	children: dict[str, list[str]] = defaultdict(list)
	for src, dst in edges:
		children[src].append(dst)

	moves = []
	midnight = []
	for node in order:
		preds = [src for src, dst in edges if dst == node]
		if not preds:
			continue
		need = max(add_seconds(proposed[p], minimum_seconds) for p in preds)
		current = proposed[node]
		if current > max(proposed[p] for p in preds):
			# already strictly after every prerequisite
			continue
		candidate = need
		key = (stock_key_of or {}).get(node)
		taken = (occupied or {}).get(key, set()) if key else set()
		# skip slots taken by unrelated docs; also skip other assigned nodes on same key
		while candidate in taken or _slot_taken_by_other(candidate, node, proposed, stock_key_of, key):
			candidate = add_seconds(candidate, minimum_seconds)
		if crosses_posting_date(current, candidate):
			midnight.append(node)
			continue
		if candidate != current:
			moves.append(
				{
					"document": node,
					"old": format_datetime(current),
					"new": format_datetime(candidate),
				}
			)
			proposed[node] = candidate

	if midnight:
		return {
			"ok": False,
			"status": STATUS_MIDNIGHT,
			"midnight": midnight,
			"proposed": {k: format_datetime(v) for k, v in proposed.items()},
			"moves": moves,
		}

	return {
		"ok": True,
		"status": "OK",
		"proposed": {k: format_datetime(v) for k, v in proposed.items()},
		"proposed_dt": proposed,
		"moves": moves,
	}


def _slot_taken_by_other(candidate, node, proposed, stock_key_of, key) -> bool:
	if not key or not stock_key_of:
		return False
	for other, dt in proposed.items():
		if other == node:
			continue
		if stock_key_of.get(other) == key and dt == candidate:
			return True
	return False
