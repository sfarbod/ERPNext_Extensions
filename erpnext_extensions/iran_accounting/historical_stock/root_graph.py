# Copyright (c) 2026, ERPNext Extensions contributors
"""Root-cause vs downstream issue grouping for Master Plan V2 (5.3.0)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _voucher(row: dict) -> str | None:
	return row.get("voucher") or row.get("voucher_no") or row.get("outbound_document") or row.get("name")


def _identity(row: dict) -> tuple:
	item = row.get("item") or row.get("item_code")
	wh = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	return (item, wh)


def _patient(row: dict) -> str | None:
	p = row.get("patient_zero") or row.get("root_patient_zero")
	if isinstance(p, dict):
		return p.get("voucher_no") or p.get("voucher")
	if p:
		return str(p)
	return row.get("required_prerequisite") or row.get("prerequisite")


def group_root_vs_downstream(rows: list[dict], *, repair_class: str) -> dict:
	"""Partition scan rows into root patients vs downstream dependents.

	A row is a *root* when:
	- it has no patient_zero / prerequisite, or
	- patient_zero points at itself, or
	- it is referenced as patient_zero by other rows.

	Downstream rows point at a different patient_zero.
	"""
	rows = list(rows or [])
	by_voucher: dict[str, dict] = {}
	referenced: set[str] = set()
	for row in rows:
		v = _voucher(row)
		if v:
			by_voucher[v] = row
		pz = _patient(row)
		if pz:
			referenced.add(pz)

	roots = []
	downstream = []
	orphans = []
	for row in rows:
		v = _voucher(row)
		pz = _patient(row)
		if not pz or pz == v or v in referenced:
			# Self-root or referenced as someone else's root.
			if pz and pz != v and v not in referenced and pz not in by_voucher:
				orphans.append(row)
			else:
				roots.append(row)
		else:
			downstream.append(row)

	by_root: dict[str, list] = defaultdict(list)
	for row in downstream:
		by_root[str(_patient(row))].append(
			{
				"voucher": _voucher(row),
				"item": _identity(row)[0],
				"warehouse": _identity(row)[1],
				"status": row.get("planner_status") or row.get("status") or row.get("drift_status"),
			}
		)

	return {
		"repair_class": repair_class,
		"total": len(rows),
		"root_count": len(roots),
		"downstream_count": len(downstream),
		"orphan_dependency_count": len(orphans),
		"roots": [
			{
				"voucher": _voucher(r),
				"item": _identity(r)[0],
				"warehouse": _identity(r)[1],
				"status": r.get("planner_status") or r.get("status") or r.get("drift_status"),
				"confidence": r.get("confidence"),
				"downstream_n": len(by_root.get(str(_voucher(r)) or "", [])),
			}
			for r in roots[:100]
		],
		"downstream_by_root": {k: v[:40] for k, v in list(by_root.items())[:50]},
		"message": (
			"Repair roots first. Downstream findings often heal via controlled "
			"min-scope repost after the root is authoritative."
		),
	}


def detect_dependency_cycle(rows: list[dict]) -> list[dict]:
	"""Detect A→B→A style patient-zero / prerequisite cycles."""
	edges: dict[str, str] = {}
	for row in rows or []:
		v = _voucher(row)
		pz = _patient(row)
		if v and pz and v != pz:
			edges[v] = pz
	cycles = []
	seen = set()
	for start in edges:
		if start in seen:
			continue
		path = []
		node = start
		guard = set()
		while node and node not in guard:
			guard.add(node)
			path.append(node)
			node = edges.get(node)
			if node in path:
				cycle = path[path.index(node) :] + [node]
				cycles.append({"cycle": cycle, "length": len(cycle) - 1})
				seen.update(cycle)
				break
		seen.update(guard)
	return cycles


def build_root_cause_graph(class_rows: dict[str, list[dict]]) -> dict:
	"""Cross-class root-cause graph summary for Master Plan V2."""
	graph = {
		"classes": {},
		"cycles": [],
		"cross_class_edges": [],
	}
	all_rows = []
	for repair_class, rows in (class_rows or {}).items():
		grouped = group_root_vs_downstream(rows, repair_class=repair_class)
		graph["classes"][repair_class] = grouped
		all_rows.extend(rows or [])
		for row in rows or []:
			pz = _patient(row)
			v = _voucher(row)
			if pz and v and pz != v:
				graph["cross_class_edges"].append(
					{
						"from": v,
						"to": pz,
						"repair_class": repair_class,
						"topic": row.get("topic"),
					}
				)
	graph["cycles"] = detect_dependency_cycle(all_rows)
	graph["edge_count"] = len(graph["cross_class_edges"])
	graph["cycle_count"] = len(graph["cycles"])
	return graph
