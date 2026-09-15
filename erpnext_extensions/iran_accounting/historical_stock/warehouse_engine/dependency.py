# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse dependency graph helpers."""

from __future__ import annotations

from collections import defaultdict


def build_dependency_report(analysis: dict) -> dict:
	"""Summarize MA / batch / WO / cross-WH dependencies from a scope analysis."""
	edges = list(analysis.get("edges") or [])
	for hit in analysis.get("moving_average_dependencies") or []:
		edges.append(
			{
				"from": analysis.get("outbound_document") or analysis.get("selected_voucher"),
				"to": hit.get("voucher"),
				"type": "WAREHOUSE_MA_DEPENDENCY",
				"why": f"MA rewrite fields={hit.get('fields')} batch={hit.get('batch')}",
			}
		)
	by_type = defaultdict(int)
	for e in edges:
		by_type[str(e.get("type") or "UNKNOWN")] += 1
	return {
		"item": analysis.get("item"),
		"warehouse": analysis.get("warehouse"),
		"required_scope": analysis.get("required_replay_scope"),
		"edge_count": len(edges),
		"by_type": dict(by_type),
		"edges": edges,
		"affected_vouchers": analysis.get("affected_vouchers") or [],
		"other_batches": analysis.get("other_batches_in_window") or [],
		"other_work_orders": analysis.get("other_work_orders_in_window") or [],
		"cross_warehouse": analysis.get("cross_warehouse_movements") or [],
		"gl_dependencies": "selective_after_valuation_change",
		"bin_dependencies": "rebuild_from_last_sle",
		"riv_dependencies": "NOT_INVOKED",
	}
