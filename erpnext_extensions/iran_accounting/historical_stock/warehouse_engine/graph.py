# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse dependency graph — multi-pair / multi-root campaign topology.

Never assumes whole-warehouse replay. Builds a proven graph over posting-order
pairs sharing an Item+Warehouse identity, plus MA / batch / WO / cross-WH links.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from frappe.utils import get_datetime


NODE_PAIR = "PAIR"
NODE_VOUCHER = "VOUCHER"
NODE_BATCH = "BATCH"
NODE_WORK_ORDER = "WORK_ORDER"
NODE_IDENTITY = "IDENTITY"
NODE_WAREHOUSE = "WAREHOUSE"

EDGE_PAIR_SHARE_IDENTITY = "SHARE_IDENTITY"
EDGE_REPLAY_CHAIN = "REPLAY_CHAIN"
EDGE_WAREHOUSE_MA = "WAREHOUSE_MA_CHAIN"
EDGE_CROSS_BATCH = "CROSS_BATCH"
EDGE_CROSS_WO = "CROSS_WORK_ORDER"
EDGE_CROSS_WH = "CROSS_WAREHOUSE"
EDGE_CROSS_REPLAY = "CROSS_REPLAY_DEPENDENCY"
EDGE_POSTING_ORDER = "POSTING_DATETIME_ORDER"


def build_identity_pair_graph(pairs: list[dict], *, item: str, warehouse: str) -> dict:
	"""Build dependency graph for posting-order pairs on one Item+Warehouse.

	Returns nodes, edges, and topology classifications:
	isolated_roots, shared_roots, bridge_nodes, multi_root_campaigns,
	safe_independent_clusters.
	"""
	pairs = [dict(p) for p in (pairs or []) if p]
	nodes: list[dict] = []
	edges: list[dict] = []
	node_ids: set[str] = set()

	def add_node(nid: str, ntype: str, **meta):
		if nid in node_ids:
			return
		node_ids.add(nid)
		nodes.append({"id": nid, "type": ntype, **meta})

	identity_id = f"ID:{item}|{warehouse}"
	add_node(identity_id, NODE_IDENTITY, item=item, warehouse=warehouse)
	add_node(f"WH:{warehouse}", NODE_WAREHOUSE, warehouse=warehouse)

	pair_nodes: list[str] = []
	voucher_to_pairs: dict[str, list[str]] = defaultdict(list)
	batch_to_pairs: dict[str, list[str]] = defaultdict(list)
	wo_to_pairs: dict[str, list[str]] = defaultdict(list)

	for idx, p in enumerate(pairs):
		inn = p.get("inbound_document") or p.get("inbound")
		out = p.get("outbound_document") or p.get("outbound") or p.get("negative_voucher")
		batch = p.get("batch") or p.get("batch_no") or ""
		wo = p.get("work_order") or ""
		pid = f"PAIR:{out or inn or idx}"
		pair_nodes.append(pid)
		add_node(
			pid,
			NODE_PAIR,
			inbound=inn,
			outbound=out,
			batch=batch,
			work_order=wo,
			optimizer_status=p.get("optimizer_status") or p.get("status"),
			planner_status=p.get("planner_status"),
			confidence=p.get("confidence"),
			min_qty_before=p.get("min_qty_before"),
			min_qty_after=p.get("min_qty_after"),
			index=idx,
		)
		edges.append(
			{
				"from": pid,
				"to": identity_id,
				"type": EDGE_PAIR_SHARE_IDENTITY,
				"why": "pair posts on shared Item+Warehouse SLE identity",
			}
		)
		for vn, role in ((inn, "inbound"), (out, "outbound")):
			if not vn:
				continue
			vid = f"V:{vn}"
			add_node(vid, NODE_VOUCHER, voucher=vn, role=role)
			edges.append({"from": pid, "to": vid, "type": EDGE_POSTING_ORDER, "why": role})
			voucher_to_pairs[vn].append(pid)
		if batch:
			bid = f"B:{batch}"
			add_node(bid, NODE_BATCH, batch=batch)
			edges.append({"from": pid, "to": bid, "type": EDGE_CROSS_BATCH, "why": "pair batch"})
			batch_to_pairs[batch].append(pid)
		if wo:
			wid = f"WO:{wo}"
			add_node(wid, NODE_WORK_ORDER, work_order=wo)
			edges.append({"from": pid, "to": wid, "type": EDGE_CROSS_WO, "why": "pair work order"})
			wo_to_pairs[wo].append(pid)

	# Shared-root / bridge detection via shared vouchers, batches, WOs
	shared_roots: list[dict] = []
	bridge_nodes: list[dict] = []
	for vn, pids in voucher_to_pairs.items():
		if len(set(pids)) > 1:
			shared_roots.append({"kind": "voucher", "id": vn, "pairs": sorted(set(pids))})
			bridge_nodes.append({"id": f"V:{vn}", "type": NODE_VOUCHER, "links": len(set(pids))})
	for batch, pids in batch_to_pairs.items():
		if len(set(pids)) > 1:
			shared_roots.append({"kind": "batch", "id": batch, "pairs": sorted(set(pids))})
			bridge_nodes.append({"id": f"B:{batch}", "type": NODE_BATCH, "links": len(set(pids))})
			# Cross-batch edge between pairs
			uniq = sorted(set(pids))
			for i in range(len(uniq) - 1):
				edges.append(
					{
						"from": uniq[i],
						"to": uniq[i + 1],
						"type": EDGE_CROSS_BATCH,
						"why": f"shared batch {batch}",
					}
				)
	for wo, pids in wo_to_pairs.items():
		if len(set(pids)) > 1:
			shared_roots.append({"kind": "work_order", "id": wo, "pairs": sorted(set(pids))})
			bridge_nodes.append({"id": f"WO:{wo}", "type": NODE_WORK_ORDER, "links": len(set(pids))})
			uniq = sorted(set(pids))
			for i in range(len(uniq) - 1):
				edges.append(
					{
						"from": uniq[i],
						"to": uniq[i + 1],
						"type": EDGE_CROSS_WO,
						"why": f"shared work order {wo}",
					}
				)

	# Warehouse MA chain: all pairs on same identity are MA-coupled
	if len(pair_nodes) > 1:
		ordered = _order_pairs_by_time(pairs, pair_nodes)
		for i in range(len(ordered) - 1):
			edges.append(
				{
					"from": ordered[i],
					"to": ordered[i + 1],
					"type": EDGE_WAREHOUSE_MA,
					"why": "sequential MA on shared Item+Warehouse",
				}
			)
			edges.append(
				{
					"from": ordered[i],
					"to": ordered[i + 1],
					"type": EDGE_CROSS_REPLAY,
					"why": "replay of earlier pair rewrites later SLE qty_after/stock_value",
				}
			)

	# Isolated roots = single pair with no shared voucher/batch/WO bridges
	bridged_pairs = {p for s in shared_roots for p in s["pairs"]}
	isolated_roots = [pid for pid in pair_nodes if pid not in bridged_pairs]
	# On a shared identity with 2+ pairs, MA coupling means they are NOT independent
	# even without shared batch/voucher — mark as multi_root_campaign.
	multi_root_campaigns: list[dict] = []
	safe_independent_clusters: list[dict] = []
	if len(pair_nodes) >= 2:
		multi_root_campaigns.append(
			{
				"item": item,
				"warehouse": warehouse,
				"pairs": pair_nodes,
				"n_pairs": len(pair_nodes),
				"reason": "shared Item+Warehouse MA identity — must simulate as one campaign",
				"mathematically_independent": False,
			}
		)
	elif len(pair_nodes) == 1:
		safe_independent_clusters.append(
			{
				"item": item,
				"warehouse": warehouse,
				"pairs": pair_nodes,
				"n_pairs": 1,
				"reason": "single pair on identity — independent if sim clears",
				"mathematically_independent": True,
			}
		)

	by_type = defaultdict(int)
	for e in edges:
		by_type[str(e.get("type") or "UNKNOWN")] += 1

	return {
		"item": item,
		"warehouse": warehouse,
		"identity": identity_id,
		"n_pairs": len(pair_nodes),
		"nodes": nodes,
		"edges": edges,
		"edge_count": len(edges),
		"by_edge_type": dict(by_type),
		"isolated_roots": isolated_roots,
		"shared_roots": shared_roots,
		"bridge_nodes": bridge_nodes,
		"multi_root_campaigns": multi_root_campaigns,
		"safe_independent_clusters": safe_independent_clusters,
		"dependency_depth": max(0, len(pair_nodes) - 1),
		"requires_joint_campaign": len(pair_nodes) >= 2,
	}


def build_warehouse_universe_graph(rows: list[dict]) -> dict:
	"""Group rows by identity and build per-identity pair graphs + cross-WH links."""
	by_id: dict[tuple[str, str], list[dict]] = defaultdict(list)
	for r in rows or []:
		item = r.get("item") or r.get("item_code") or ""
		wh = r.get("warehouse") or ""
		if not item or not wh:
			continue
		by_id[(item, wh)].append(r)

	identities = []
	cross_wh_items: dict[str, set[str]] = defaultdict(set)
	for (item, wh), pairs in sorted(by_id.items(), key=lambda x: (-len(x[1]), x[0][0])):
		g = build_identity_pair_graph(pairs, item=item, warehouse=wh)
		identities.append(g)
		cross_wh_items[item].add(wh)

	cross_wh_edges = []
	for item, whs in cross_wh_items.items():
		if len(whs) < 2:
			continue
		ordered = sorted(whs)
		for i in range(len(ordered) - 1):
			cross_wh_edges.append(
				{
					"from": f"ID:{item}|{ordered[i]}",
					"to": f"ID:{item}|{ordered[i + 1]}",
					"type": EDGE_CROSS_WH,
					"why": f"item {item} posts in multiple warehouses",
					"item": item,
				}
			)

	multi = [g for g in identities if g.get("requires_joint_campaign")]
	independent = [g for g in identities if g.get("safe_independent_clusters")]
	return {
		"n_identities": len(identities),
		"n_multi_root": len(multi),
		"n_independent": len(independent),
		"identities": identities,
		"cross_warehouse_edges": cross_wh_edges,
		"multi_root_campaigns": [c for g in identities for c in g.get("multi_root_campaigns") or []],
		"safe_independent_clusters": [
			c for g in identities for c in g.get("safe_independent_clusters") or []
		],
		"bridge_nodes": [b for g in identities for b in g.get("bridge_nodes") or []],
		"shared_roots": [s for g in identities for s in g.get("shared_roots") or []],
	}


def _order_pairs_by_time(pairs: list[dict], pair_nodes: list[str]) -> list[str]:
	keyed: list[tuple[Any, str]] = []
	for p, pid in zip(pairs, pair_nodes, strict=False):
		ts = (
			p.get("current_outbound_time")
			or p.get("current_inbound_time")
			or p.get("proposed_outbound_time")
			or p.get("posting_datetime")
			or ""
		)
		try:
			keyed.append((get_datetime(ts) if ts else None, pid))
		except Exception:
			keyed.append((None, pid))
	keyed.sort(key=lambda x: (x[0] is None, x[0] or 0, x[1]))
	return [pid for _, pid in keyed]
