# Copyright (c) 2026, ERPNext Extensions contributors
"""Repair dependency graph for a selected identity (read-only)."""

from __future__ import annotations

import frappe
from frappe.utils import flt


def repair_graph(item=None, batch=None, work_order=None, voucher=None, warehouse=None, limit=80, row=None) -> dict:
	if row:
		from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies, tree_as_graph

		resolution = resolve_dependencies(row if isinstance(row, dict) else {})
		out = tree_as_graph(resolution)
		out["item"] = item or (row or {}).get("item") or (row or {}).get("item_code")
		out["batch"] = batch or (row or {}).get("batch")
		out["work_order"] = work_order or (row or {}).get("work_order")
		out["warehouse"] = warehouse or (row or {}).get("warehouse")
		out["warning"] = None
		return out
	if not any((item, batch, work_order, voucher)):
		return {
			"nodes": [],
			"edges": [],
			"count": 0,
			"replay_depth": 0,
			"item": item,
			"batch": batch,
			"work_order": work_order,
			"warning": "Graph requires item, batch, work order, or voucher",
		}
	conds = ["sle.is_cancelled=0"]
	args: list = []
	join = "JOIN `tabStock Entry` se ON se.name=sle.voucher_no"
	if item:
		conds.append("sle.item_code=%s")
		args.append(item)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if work_order:
		conds.append("se.work_order=%s")
		args.append(work_order)
	if voucher:
		conds.append("sle.voucher_no=%s")
		args.append(voucher)
	if batch:
		join += " JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle"
		conds.append("sbe.batch_no=%s")
		args.append(batch)
	rows = frappe.db.sql(
		f"""
		SELECT sle.voucher_no, se.purpose, sle.item_code, sle.warehouse, sle.actual_qty,
		       sle.incoming_rate, sle.outgoing_rate, sle.stock_value_difference,
		       sle.posting_datetime, se.work_order, se.docstatus
		FROM `tabStock Ledger Entry` sle
		{join}
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	nodes = []
	seen = set()
	edges = []
	prev = None
	for r in rows:
		if r.voucher_no not in seen:
			seen.add(r.voucher_no)
			nodes.append(
				{
					"id": r.voucher_no,
					"voucher": r.voucher_no,
					"purpose": r.purpose,
					"item": r.item_code,
					"warehouse": r.warehouse,
					"batch": batch,
					"qty": r.actual_qty,
					"rate": r.outgoing_rate if flt(r.actual_qty) < 0 else r.incoming_rate,
					"incoming_rate": r.incoming_rate,
					"outgoing_rate": r.outgoing_rate,
					"work_order": r.work_order,
					"posting_datetime": str(r.posting_datetime),
					"status": "SUBMITTED" if r.docstatus == 1 else r.docstatus,
					"repair_status": "SUBMITTED" if r.docstatus == 1 else r.docstatus,
					"replay_order": len(nodes) + 1,
					"replay_depth": 0,
					"estimated_replay_count": 1,
				}
			)
			if prev:
				edges.append({"from": prev, "to": r.voucher_no})
			prev = r.voucher_no
	depth = max(0, len(nodes) - 1)
	for i, node in enumerate(nodes):
		node["replay_order"] = i + 1
		node["replay_depth"] = depth - i
		node["estimated_replay_count"] = depth - i
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	cache = {}
	for node in nodes:
		planned = attach_plan(
			{
				"voucher": node.get("voucher"),
				"item": node.get("item"),
				"warehouse": node.get("warehouse"),
				"batch": node.get("batch") or batch,
			},
			cache=cache,
		)
		node["planner_status"] = planned.get("planner_status")
		node["eligible"] = planned.get("eligible")
		node["blocked"] = planned.get("blocked")
		node["blocker"] = planned.get("blocker")
		node["reason"] = planned.get("reason")
		node["sql_updates"] = planned.get("sql_updates")
		node["replay_count"] = planned.get("replay_count")
		node["rebuild_count"] = planned.get("rebuild_count")
		node["patient_zero"] = planned.get("patient_zero")
		node["required_prerequisite"] = planned.get("required_prerequisite")
		node["dependency"] = planned.get("planner", {}).get("dependency")
		node["immediate_blocker"] = planned.get("immediate_blocker")
		node["root_blocker"] = planned.get("root_blocker")
		node["dependency_depth"] = planned.get("dependency_depth")
		node["repair_order"] = planned.get("repair_order")
		node["required_action"] = planned.get("required_action")
		node["tree_text"] = planned.get("tree_text")
		node["dependency_tree"] = planned.get("dependency_tree")
	return {
		"nodes": nodes,
		"edges": edges,
		"count": len(nodes),
		"replay_depth": depth,
		"item": item,
		"batch": batch,
		"work_order": work_order,
	}
