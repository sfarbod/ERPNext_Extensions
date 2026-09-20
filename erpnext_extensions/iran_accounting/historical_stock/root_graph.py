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


def build_zero_wrong_root_graph(
	zero_rows: list[dict] | None = None,
	wrong_rows: list[dict] | None = None,
) -> dict:
	"""Unified Zero+Wrong root chains for Phase 4 (cross-KPI dependency).

	Returns chain counts, independent reconstructable roots, downstream symptoms,
	and MATCHED_BUT_CORRUPT prioritisation.
	"""
	zero_rows = list(zero_rows or [])
	wrong_rows = list(wrong_rows or [])
	tagged = []
	for r in zero_rows:
		tagged.append({**r, "topic": r.get("topic") or "ZERO_RATE", "_kpi": "ZERO_RATE"})
	for r in wrong_rows:
		tagged.append({**r, "topic": r.get("topic") or "WRONG_RATE", "_kpi": "WRONG_RATE"})

	zero_g = group_root_vs_downstream(zero_rows, repair_class="ZERO_RATE")
	wrong_g = group_root_vs_downstream(wrong_rows, repair_class="WRONG_RATE")
	cross = build_root_cause_graph({"ZERO_RATE": zero_rows, "WRONG_RATE": wrong_rows})

	def _is_downstream(row) -> bool:
		pz = _patient(row)
		v = _voucher(row)
		return bool(pz and v and pz != v)

	def _is_exact_recon(row) -> bool:
		st = str(row.get("status") or row.get("planner_status") or "")
		return bool(
			row.get("eligible")
			or st in ("RECONSTRUCTABLE", "READY_RATE", "READY")
			or str(row.get("confidence") or "") == "EXACT"
			and "WAITING" not in st
		)

	independent_roots = []
	downstream_symptoms = []
	waiting_upstream = []
	user_blocked = []
	tool_limit = []
	matched_but_corrupt = []

	for row in tagged:
		flags = row.get("flags") or []
		if "MATCHED_BUT_CORRUPT" in flags or row.get("flag") == "MATCHED_BUT_CORRUPT":
			matched_but_corrupt.append(row)
		st = str(row.get("status") or row.get("planner_status") or "")
		kb = str(row.get("kpi_bucket") or "")
		if row.get("user_action_required") or "USER" in st or "MATERIAL_RECEIPT_ZERO" in st:
			user_blocked.append(row)
		elif kb == "ZERO_RATE_TOOL_LIMIT" or "TOOL_LIMIT" in st:
			tool_limit.append(row)
		elif _is_downstream(row) or "WAITING" in st or "DEPENDENCY" in st or "POISON" in st:
			if _is_downstream(row):
				downstream_symptoms.append(row)
			else:
				waiting_upstream.append(row)
		elif _is_exact_recon(row) and not _is_downstream(row):
			independent_roots.append(row)
		elif "WAITING" in st:
			waiting_upstream.append(row)

	# Deduplicate root chains by patient-zero / self voucher.
	chain_keys: dict[str, dict] = {}
	for row in tagged:
		pz = _patient(row) or _voucher(row)
		if not pz:
			continue
		entry = chain_keys.setdefault(
			pz,
			{
				"root": pz,
				"findings": 0,
				"topics": set(),
				"earliest": None,
				"purposes": set(),
				"exact_ready": 0,
			},
		)
		entry["findings"] += 1
		entry["topics"].add(row.get("_kpi") or row.get("topic") or "?")
		purpose = row.get("purpose")
		if purpose:
			entry["purposes"].add(purpose)
		pd = str(row.get("posting_datetime") or row.get("posting_date") or "")
		if pd and (entry["earliest"] is None or pd < entry["earliest"]):
			entry["earliest"] = pd
		if _is_exact_recon(row) and not _is_downstream(row):
			entry["exact_ready"] += 1

	chains = []
	for k, v in chain_keys.items():
		chains.append(
			{
				"root": k,
				"findings": v["findings"],
				"topics": sorted(v["topics"]),
				"cross_kpi": len(v["topics"]) > 1,
				"earliest": v["earliest"],
				"purposes": sorted(v["purposes"]),
				"exact_ready": v["exact_ready"],
			}
		)
	chains.sort(key=lambda c: (c.get("earliest") or "9999", -c["exact_ready"], -c["findings"]))

	cross_kpi_chains = [c for c in chains if c.get("cross_kpi")]
	independent_chain_roots = [
		c for c in chains if c.get("exact_ready") and c["root"] in {_voucher(r) for r in independent_roots}
	]

	return {
		"ZERO_RAW_FINDINGS": len(zero_rows),
		"WRONG_RAW_FINDINGS": len(wrong_rows),
		"ZERO_ROOT_CHAINS": zero_g.get("root_count"),
		"WRONG_ROOT_CHAINS": wrong_g.get("root_count"),
		"ZERO_DOWNSTREAM_SYMPTOMS": zero_g.get("downstream_count"),
		"WRONG_DOWNSTREAM_SYMPTOMS": wrong_g.get("downstream_count"),
		"ZERO_INDEPENDENT_ROOTS": sum(
			1 for r in zero_rows if _is_exact_recon(r) and not _is_downstream(r)
		),
		"WRONG_INDEPENDENT_ROOTS": sum(
			1 for r in wrong_rows if _is_exact_recon(r) and not _is_downstream(r)
		),
		"CROSS_KPI_ROOT_CHAINS": len(cross_kpi_chains),
		"UNIFIED_CHAIN_COUNT": len(chains),
		"INDEPENDENT_REPAIRABLE_ROOTS": len(independent_roots),
		"WAITING_UPSTREAM_ROOTS": len(waiting_upstream),
		"USER_BLOCKED_ROOTS": len(user_blocked),
		"TOOL_LIMIT_ROOTS": len(tool_limit),
		"DOWNSTREAM_FINDINGS": len(downstream_symptoms),
		"MATCHED_BUT_CORRUPT": len(matched_but_corrupt),
		"cycles": cross.get("cycles") or [],
		"zero_group": zero_g,
		"wrong_group": wrong_g,
		"earliest_independent_chains": independent_chain_roots[:20] or chains[:20],
		"cross_kpi_sample": cross_kpi_chains[:20],
		"independent_root_sample": [
			{
				"voucher": _voucher(r),
				"kpi": r.get("_kpi"),
				"purpose": r.get("purpose"),
				"item": _identity(r)[0],
				"warehouse": _identity(r)[1],
				"status": r.get("status") or r.get("planner_status"),
				"confidence": r.get("confidence"),
				"proposed_rate": r.get("proposed_rate") or r.get("expected_rate"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"posting_date": r.get("posting_date"),
			}
			for r in independent_roots[:30]
		],
	}
