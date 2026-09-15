# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse campaign optimizer — discover SAFE_GROUP multi-pair campaigns.

Single-root warehouse repairs are often exhausted while a joint campaign on the
same Item+Warehouse identity still clears mathematically. This module:

1. Groups posting-order candidates by identity
2. Builds the dependency graph
3. Merges proposed timestamp moves across all pairs
4. Simulates the joint warehouse replay
5. Emits READY_WAREHOUSE_CAMPAIGN only when every safety gate passes
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from time import perf_counter

from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	READY_WAREHOUSE_CAMPAIGN,
	READY_WAREHOUSE_REPLAY,
	UNSAFE_CROSS_IDENTITY,
	WAREHOUSE_AMBIGUOUS,
	WAREHOUSE_CAMPAIGN_COMPLETE,
	WAREHOUSE_CAMPAIGN_REQUIRED,
	WAREHOUSE_REAL_SHORTAGE,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.graph import (
	build_identity_pair_graph,
	build_warehouse_universe_graph,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.simulator import (
	simulate_warehouse_replay,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.validator import (
	validate_warehouse_simulation,
)

# Optimizer statuses that can contribute timestamp moves to a warehouse campaign.
CAMPAIGN_ELIGIBLE_OPTS = (
	"CROSS_TIME_REPAIRABLE",
	"REPAIRABLE_SECONDS",
	"SAME_TIME_REPAIRABLE",
	"MIDNIGHT_REVIEW",
	"CROSS_ITEM_CONFLICT",
)

COMPANY_DEFAULT = "اسپاد فارمد دارو"


def discover_warehouse_campaigns(
	rows: list[dict] | None = None,
	*,
	company: str | None = None,
	scan: dict | None = None,
) -> dict:
	"""Discover and classify warehouse SAFE_GROUP campaigns (read-only)."""
	t0 = perf_counter()
	company = company or COMPANY_DEFAULT
	if rows is None:
		rows = _load_posting_order_rows(company, scan=scan)

	# Keep EXACT (or blank) candidates with an eligible optimizer status.
	eligible = []
	for r in rows or []:
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		if opt not in CAMPAIGN_ELIGIBLE_OPTS:
			continue
		conf = str(r.get("confidence") or "")
		if conf and conf not in ("EXACT", ""):
			continue
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse")
		if not item or not wh:
			continue
		if not (r.get("inbound_document") and r.get("outbound_document")):
			continue
		eligible.append(r)

	graph = build_warehouse_universe_graph(eligible)
	by_id: dict[tuple[str, str], list[dict]] = defaultdict(list)
	for r in eligible:
		by_id[(r.get("item") or r.get("item_code"), r.get("warehouse"))].append(r)

	campaigns = []
	for (item, wh), pairs in sorted(by_id.items(), key=lambda x: (-len(x[1]), x[0][0])):
		campaigns.append(optimize_identity_campaign(pairs, item=item, warehouse=wh))

	# Merge campaigns that share moved vouchers (prevents ±2s oscillation).
	campaigns = _merge_shared_voucher_campaigns(campaigns)

	ready = [
		c
		for c in campaigns
		if c.get("planner_status") in (READY_WAREHOUSE_CAMPAIGN, READY_WAREHOUSE_REPLAY)
		and c.get("eligible")
	]
	shortage = [c for c in campaigns if c.get("planner_status") == WAREHOUSE_REAL_SHORTAGE]
	ambiguous = [c for c in campaigns if c.get("planner_status") == WAREHOUSE_AMBIGUOUS]
	required = [c for c in campaigns if c.get("planner_status") == WAREHOUSE_CAMPAIGN_REQUIRED]

	return {
		"company": company,
		"elapsed_seconds": round(perf_counter() - t0, 3),
		"n_eligible_pairs": len(eligible),
		"n_identities": len(campaigns),
		"n_ready_campaigns": len(ready),
		"n_shortage": len(shortage),
		"n_ambiguous": len(ambiguous),
		"n_campaign_required": len(required),
		"graph_summary": {
			"n_identities": graph.get("n_identities"),
			"n_multi_root": graph.get("n_multi_root"),
			"n_independent": graph.get("n_independent"),
			"n_cross_wh_edges": len(graph.get("cross_warehouse_edges") or []),
			"n_bridge_nodes": len(graph.get("bridge_nodes") or []),
		},
		"campaigns": campaigns,
		"ready_campaigns": ready,
		"safe_groups": [_as_safe_group(c) for c in ready],
		"message": (
			"Warehouse campaigns require joint simulation proof. "
			"Never replay whole warehouse blindly."
		),
	}


def optimize_identity_campaign(pairs: list[dict], *, item: str, warehouse: str) -> dict:
	"""Simulate joint multi-pair reorder for one Item+Warehouse identity.

	When primary identity clears but sister identities (shared transfer vouchers)
	gain new negatives, iteratively delay starved outbounds until every touched
	identity clears — or classify UNSAFE_CROSS_IDENTITY / REAL_SHORTAGE.
	"""
	pairs = list(pairs or [])
	graph = build_identity_pair_graph(pairs, item=item, warehouse=warehouse)
	proposed, move_rows, from_dt, synth_notes = merge_proposed_times(pairs)
	analysis = {
		"item": item,
		"warehouse": warehouse,
		"required_replay_scope": "WAREHOUSE_VALUATION_SCOPED",
		"warehouse_still_negative": False,
		"cyclic": False,
		"reason": "WAREHOUSE_CAMPAIGN multi-pair",
		"affected_vouchers": sorted({m["document"] for m in move_rows if m.get("document")}),
		"moves": move_rows,
		"cross_warehouse_movements": [],
		"warehouse_qty": {},
		"n_pairs": len(pairs),
		"from_datetime": str(from_dt) if from_dt else None,
	}

	if not from_dt or not proposed:
		return _campaign_result(
			item,
			warehouse,
			pairs,
			graph,
			analysis,
			sim={"ok": False, "reason": "missing from_datetime or proposed_times"},
			decision={
				"planner_status": WAREHOUSE_AMBIGUOUS,
				"eligible": False,
				"reason": "campaign lacks proposed times",
				"required_action": "Inspect pair moves / midnight synthesis",
				"ok": False,
				"estimated_sql": 0,
				"estimated_runtime_seconds": 0,
				"affected_vouchers": [],
				"affected_sle": [],
				"checks": [],
			},
			proposed=proposed,
			move_rows=move_rows,
			synth_notes=synth_notes,
			expansion=None,
		)

	expansion = expand_cross_identity_times(proposed, from_dt, max_rounds=10)
	proposed = expansion["proposed_times"]
	move_rows = _moves_from_proposed(proposed, move_rows)
	# Drop no-op moves already matching live posting_datetime (±0.5s)
	proposed, move_rows, noop_notes = _drop_noop_moves(proposed, move_rows)
	synth_notes = list(synth_notes) + list(expansion.get("notes") or []) + noop_notes
	analysis["affected_vouchers"] = sorted(proposed.keys())
	analysis["moves"] = move_rows

	if not proposed:
		return _campaign_result(
			item,
			warehouse,
			pairs,
			graph,
			analysis,
			sim={"ok": True, "reason": "already_ordered", "row_count": 0, "final_qty_unchanged": True, "idempotent": True, "negative_qty_vouchers": [], "negative_incoming_vouchers": [], "exploded_rate_vouchers": [], "expected_bin": {}, "expected_gl_impact": "none"},
			decision={
				"planner_status": "WAREHOUSE_CAMPAIGN_COMPLETE",
				"eligible": False,
				"ok": False,
				"reason": "proposed times already match live ledger — no warehouse campaign write needed",
				"required_action": "None",
				"estimated_sql": 0,
				"estimated_runtime_seconds": 0,
				"affected_vouchers": [],
				"affected_sle": [],
				"checks": [],
			},
			proposed=proposed,
			move_rows=move_rows,
			synth_notes=synth_notes,
			expansion=expansion,
		)

	sim = simulate_warehouse_replay(item, warehouse, from_dt, proposed_times=proposed)
	neg = sim.get("negative_qty_vouchers") or []
	analysis["warehouse_still_negative"] = bool(neg)
	analysis["warehouse_qty"] = {
		"proposed_min": -1 if neg else 0,
		"final_unchanged": sim.get("final_qty_unchanged"),
	}
	decision = validate_warehouse_simulation(analysis, sim)

	multi = expansion.get("verification") or verify_touched_identities_from_times(proposed, from_dt)
	if decision.get("eligible") and multi.get("all_clear"):
		decision = {
			**decision,
			"planner_status": READY_WAREHOUSE_CAMPAIGN
			if (len(pairs) >= 2 or expansion.get("rounds", 0) > 0 or synth_notes)
			else READY_WAREHOUSE_REPLAY,
			"required_action": (
				"Apply warehouse SAFE_GROUP campaign: joint timestamp reorder + multi-identity MA replay"
			),
			"reason": (
				f"READY_WAREHOUSE_CAMPAIGN — {len(pairs)} pairs, "
				f"{multi.get('n_identities', 0)} identities, "
				f"{len(proposed)} moves, expansion_rounds={expansion.get('rounds', 0)}"
			),
			"eligible": True,
			"ok": True,
		}
	elif decision.get("eligible") and not multi.get("all_clear"):
		decision = {
			**decision,
			"planner_status": UNSAFE_CROSS_IDENTITY,
			"eligible": False,
			"ok": False,
			"reason": multi.get("reason") or "primary clears but sister identity fails",
			"required_action": "Expand cross-identity delays or operator review",
		}
	elif decision.get("planner_status") == WAREHOUSE_REAL_SHORTAGE and len(pairs) >= 2:
		decision = {
			**decision,
			"reason": (
				f"WAREHOUSE_REAL_SHORTAGE after joint {len(pairs)}-pair campaign sim — "
				f"neg_qty={neg[:8]}"
			),
		}
	elif not decision.get("eligible") and len(pairs) >= 2 and not neg:
		decision = {
			**decision,
			"planner_status": WAREHOUSE_CAMPAIGN_REQUIRED,
			"reason": decision.get("reason") or "joint campaign did not clear all gates",
		}

	result = _campaign_result(
		item,
		warehouse,
		pairs,
		graph,
		analysis,
		sim=sim,
		decision=decision,
		proposed=proposed,
		move_rows=move_rows,
		synth_notes=synth_notes,
		expansion=expansion,
	)
	result["multi_identity"] = multi
	return result


def expand_cross_identity_times(proposed: dict, from_dt, *, max_rounds: int = 10) -> dict:
	"""Delay vouchers that become newly negative on sister identities after reorder."""
	import frappe

	proposed = {k: get_datetime(v) for k, v in (proposed or {}).items()}
	from_dt = get_datetime(from_dt)
	notes: list[str] = []
	round_logs: list[dict] = []
	verification = None

	for round_i in range(max_rounds):
		verification = verify_touched_identities_from_times(proposed, from_dt)
		round_logs.append(
			{
				"round": round_i,
				"n_moves": len(proposed),
				"all_clear": verification.get("all_clear"),
				"failed": [
					{"item": s["item"], "warehouse": s["warehouse"], "introduced": s.get("introduced")}
					for s in (verification.get("identities") or [])
					if not s.get("clears")
				],
			}
		)
		if verification.get("all_clear"):
			return {
				"ok": True,
				"rounds": round_i,
				"proposed_times": proposed,
				"notes": notes,
				"round_logs": round_logs,
				"verification": verification,
			}

		introduced = []
		for s in verification.get("identities") or []:
			for vn in s.get("introduced") or []:
				introduced.append((s["item"], s["warehouse"], vn))
		if not introduced:
			break

		bump = 0
		for it, wh, vn in introduced:
			pos = frappe.db.sql(
				"""
				SELECT voucher_no FROM `tabStock Ledger Entry`
				WHERE item_code=%s AND warehouse=%s AND voucher_no IN %s
				  AND is_cancelled=0 AND actual_qty>0
				""",
				(it, wh, tuple(proposed.keys())),
				as_dict=True,
			)
			latest = max(proposed.values())
			for p in pos:
				if p.voucher_no in proposed:
					latest = max(latest, proposed[p.voucher_no])
			# Skip no-op delays: voucher already at/after supply in proposed map
			# or already at/after supply in the live ledger.
			existing = proposed.get(vn)
			if existing and existing >= latest:
				continue
			cur_row = frappe.db.sql(
				"""
				SELECT posting_datetime FROM `tabStock Ledger Entry`
				WHERE voucher_no=%s AND is_cancelled=0
				ORDER BY posting_datetime DESC LIMIT 1
				""",
				(vn,),
			)
			if cur_row:
				cur_dt = get_datetime(cur_row[0][0])
				if cur_dt >= latest:
					proposed[vn] = cur_dt
					continue
			bump += 1
			new_t = latest + timedelta(seconds=1 + bump)
			notes.append(f"R{round_i}: delay {vn} on {it}/{wh} → {new_t}")
			proposed[vn] = new_t

	verification = verify_touched_identities_from_times(proposed, from_dt)
	return {
		"ok": bool(verification.get("all_clear")),
		"rounds": len(round_logs),
		"proposed_times": proposed,
		"notes": notes,
		"round_logs": round_logs,
		"verification": verification,
	}


def verify_touched_identities(campaign: dict) -> dict:
	"""Verify every Item+Warehouse touched by campaign moves."""
	proposed = {k: get_datetime(v) for k, v in (campaign.get("proposed_times") or {}).items()}
	from_dt = get_datetime(campaign.get("from_datetime")) if campaign.get("from_datetime") else None
	if not proposed or not from_dt:
		return {"all_clear": False, "reason": "missing proposed_times/from_datetime", "identities": []}
	return verify_touched_identities_from_times(proposed, from_dt)


def verify_touched_identities_from_times(proposed: dict, from_dt) -> dict:
	"""Simulate each distinct identity touched by ``proposed`` voucher keys."""
	import frappe

	if not proposed:
		return {"all_clear": False, "reason": "no proposed times", "identities": [], "n_identities": 0}
	from_dt = get_datetime(from_dt)
	vouchers = list(proposed.keys())
	idents = frappe.db.sql(
		"""
		SELECT DISTINCT item_code, warehouse FROM `tabStock Ledger Entry`
		WHERE voucher_no IN %s AND is_cancelled=0
		""",
		(tuple(vouchers),),
		as_dict=True,
	)
	sims = []
	all_clear = True
	for i in idents:
		base = simulate_warehouse_replay(i.item_code, i.warehouse, from_dt, proposed_times=None)
		prop = simulate_warehouse_replay(i.item_code, i.warehouse, from_dt, proposed_times=proposed)
		base_neg = set(base.get("negative_qty_vouchers") or [])
		prop_neg = set(prop.get("negative_qty_vouchers") or [])
		introduced = sorted(prop_neg - base_neg)
		poison = list(prop.get("negative_incoming_vouchers") or []) + list(
			prop.get("exploded_rate_vouchers") or []
		)
		clears = bool(
			prop.get("ok")
			and prop.get("final_qty_unchanged")
			and prop.get("idempotent")
			and not introduced
			and not poison
		)
		if not clears:
			all_clear = False
		sims.append(
			{
				"item": i.item_code,
				"warehouse": i.warehouse,
				"clears": clears,
				"introduced": introduced,
				"cleared_negatives": sorted(base_neg - prop_neg),
				"poison": poison[:8],
				"final_qty_unchanged": prop.get("final_qty_unchanged"),
				"idempotent": prop.get("idempotent"),
				"row_count": prop.get("row_count"),
				"expected_bin": prop.get("expected_bin"),
			}
		)

	failed = [s for s in sims if not s["clears"]]
	return {
		"all_clear": all_clear,
		"n_identities": len(sims),
		"n_failed": len(failed),
		"identities": sims,
		"reason": None
		if all_clear
		else (
			"cross-identity simulation failed: "
			+ ", ".join(f"{s['item']}/{s['warehouse']}" for s in failed[:5])
		),
		"blocker_status": None if all_clear else UNSAFE_CROSS_IDENTITY,
	}


def _merge_shared_voucher_campaigns(campaigns: list[dict]) -> list[dict]:
	"""Union-find merge of eligible campaigns that share proposed voucher keys."""
	eligible = [
		c
		for c in campaigns
		if c.get("eligible")
		and c.get("planner_status") in (READY_WAREHOUSE_CAMPAIGN, READY_WAREHOUSE_REPLAY)
	]
	others = [c for c in campaigns if c not in eligible]
	if len(eligible) <= 1:
		return campaigns

	parent = {i: i for i in range(len(eligible))}

	def find(i):
		while parent[i] != i:
			parent[i] = parent[parent[i]]
			i = parent[i]
		return i

	def union(a, b):
		ra, rb = find(a), find(b)
		if ra != rb:
			parent[rb] = ra

	voucher_owner: dict[str, int] = {}
	for i, c in enumerate(eligible):
		for doc in (c.get("proposed_times") or {}):
			if doc in voucher_owner:
				union(i, voucher_owner[doc])
			else:
				voucher_owner[doc] = i

	groups: dict[int, list[dict]] = defaultdict(list)
	for i, c in enumerate(eligible):
		groups[find(i)].append(c)

	merged = []
	for members in groups.values():
		if len(members) == 1:
			merged.append(members[0])
			continue
		# Rebuild from combined pairs
		all_pairs = []
		for m in members:
			all_pairs.extend(m.get("pairs") or [])
		# Use earliest identity as anchor; optimize_identity_campaign on combined
		# pairs spanning multiple identities is not supported — keep members but
		# mark as needing joint apply and attach shared voucher set.
		shared = sorted({doc for m in members for doc in (m.get("proposed_times") or {})})
		anchor = members[0]
		anchor = dict(anchor)
		anchor["merged_from"] = [m.get("campaign_id") for m in members]
		anchor["merged_items"] = [m.get("item") for m in members]
		anchor["shared_vouchers"] = shared
		anchor["n_merged"] = len(members)
		anchor["reason"] = (
			(anchor.get("reason") or "")
			+ f" | merged {len(members)} campaigns sharing vouchers"
		)
		# Attach sibling campaigns for joint apply
		anchor["sibling_campaigns"] = members[1:]
		merged.append(anchor)
		# Drop siblings from top-level ready list (they're under anchor)
	return merged + others


def _drop_noop_moves(proposed: dict, move_rows: list[dict]) -> tuple[dict, list[dict], list[str]]:
	"""Remove proposed times that already match the live ledger."""
	import frappe

	kept = {}
	notes = []
	for doc, dt in (proposed or {}).items():
		dt = get_datetime(dt)
		cur = frappe.db.sql(
			"""
			SELECT posting_datetime FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND is_cancelled=0
			ORDER BY posting_datetime LIMIT 1
			""",
			(doc,),
		)
		if cur:
			cur_dt = get_datetime(cur[0][0])
			if abs((cur_dt - dt).total_seconds()) < 0.5:
				notes.append(f"noop {doc} already at {cur_dt}")
				continue
		kept[doc] = dt
	by_doc = {m.get("document"): m for m in (move_rows or []) if m.get("document")}
	moves = []
	for doc, dt in kept.items():
		row = dict(by_doc.get(doc) or {"document": doc, "old": None, "synthesized": True})
		row["document"] = doc
		row["new"] = dt
		moves.append(row)
	return kept, moves, notes


def _moves_from_proposed(proposed: dict, prior_moves: list[dict]) -> list[dict]:
	by_doc = {m.get("document"): dict(m) for m in (prior_moves or []) if m.get("document")}
	out = []
	for doc, dt in proposed.items():
		row = by_doc.get(doc) or {"document": doc, "old": None, "synthesized": True}
		row["document"] = doc
		row["new"] = get_datetime(dt)
		out.append(row)
	return out


def merge_proposed_times(pairs: list[dict]) -> tuple[dict, list[dict], object, list[str]]:
	"""Merge pair moves into one proposed_times map; synthesize midnight/cross-item when needed."""
	proposed: dict = {}
	move_rows: list[dict] = []
	from_candidates = []
	synth_notes: list[str] = []

	for r in pairs:
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		moves = list(r.get("moves") or [])
		inn = r.get("inbound_document")
		out = r.get("outbound_document")

		# Prefer explicit optimizer moves
		if not moves and inn and out:
			in_t = r.get("proposed_inbound_time") or r.get("current_inbound_time")
			out_t = r.get("proposed_outbound_time")
			if not out_t and in_t:
				out_t = get_datetime(in_t) + timedelta(seconds=1)
				synth_notes.append(f"synthesize outbound-after-inbound for {out} ({opt})")
			if in_t and out_t:
				# Ensure outbound after inbound
				if get_datetime(out_t) <= get_datetime(in_t):
					out_t = get_datetime(in_t) + timedelta(seconds=1)
					synth_notes.append(f"bump outbound after inbound for {out}")
				moves = [
					{
						"document": out,
						"old": r.get("current_outbound_time"),
						"new": out_t,
						"synthesized": True,
						"optimizer_status": opt,
					}
				]

		for m in moves:
			doc = m.get("document")
			new = m.get("new")
			if not doc or not new:
				continue
			proposed[doc] = get_datetime(new)
			move_rows.append(
				{
					"document": doc,
					"old": m.get("old"),
					"new": get_datetime(new),
					"pair_outbound": out,
					"pair_inbound": inn,
					"optimizer_status": opt,
					"synthesized": bool(m.get("synthesized")),
				}
			)

		# Always pin inbound/outbound proposed times when present (order anchors)
		if inn and r.get("proposed_inbound_time"):
			proposed.setdefault(inn, get_datetime(r["proposed_inbound_time"]))
		if out and r.get("proposed_outbound_time"):
			proposed.setdefault(out, get_datetime(r["proposed_outbound_time"]))

		# If outbound is proposed but inbound has no entry, pin inbound at current
		# so order_with_times keeps inbound before outbound when current inbound
		# is already earlier — and when inbound is later, use outbound-1s.
		if out and out in proposed and inn and inn not in proposed:
			in_cur = r.get("current_inbound_time")
			out_prop = proposed[out]
			if in_cur and get_datetime(in_cur) < out_prop:
				proposed[inn] = get_datetime(in_cur)
			else:
				proposed[inn] = out_prop - timedelta(seconds=1)
				synth_notes.append(f"pin inbound before outbound for {inn}")
				move_rows.append(
					{
						"document": inn,
						"old": in_cur,
						"new": proposed[inn],
						"pair_outbound": out,
						"pair_inbound": inn,
						"optimizer_status": opt,
						"synthesized": True,
					}
				)

		for t in (
			r.get("current_inbound_time"),
			r.get("current_outbound_time"),
			r.get("proposed_inbound_time"),
			r.get("proposed_outbound_time"),
		):
			if t:
				try:
					from_candidates.append(get_datetime(t))
				except Exception:
					pass
		for m in move_rows:
			if m.get("old"):
				try:
					from_candidates.append(get_datetime(m["old"]))
				except Exception:
					pass
			if m.get("new"):
				try:
					from_candidates.append(get_datetime(m["new"]))
				except Exception:
					pass

	from_dt = min(from_candidates) if from_candidates else None
	# Deduplicate move_rows by document (last write wins, matching proposed map)
	by_doc = {}
	for m in move_rows:
		by_doc[m["document"]] = m
	# Ensure every proposed doc appears as a move for apply
	for doc, dt in proposed.items():
		if doc not in by_doc:
			by_doc[doc] = {"document": doc, "old": None, "new": dt, "synthesized": True}
		else:
			by_doc[doc]["new"] = dt
	return proposed, list(by_doc.values()), from_dt, synth_notes


def _campaign_result(
	item,
	warehouse,
	pairs,
	graph,
	analysis,
	*,
	sim,
	decision,
	proposed,
	move_rows,
	synth_notes,
	expansion=None,
):
	ps = decision.get("planner_status")
	n_pairs = len(pairs)
	sql = int(decision.get("estimated_sql") or 0) or max(1, len(move_rows) + int(sim.get("row_count") or 0))
	runtime = float(decision.get("estimated_runtime_seconds") or 0) or round(
		0.05 * max(1, sim.get("row_count") or 1), 2
	)
	risk = (
		"LOW"
		if ps == READY_WAREHOUSE_CAMPAIGN
		else ("CRITICAL" if ps == WAREHOUSE_REAL_SHORTAGE else "HIGH")
	)
	return {
		"campaign_id": f"WHCAM-{item}-{abs(hash(warehouse)) % 10_000_000:07d}-{n_pairs}",
		"item": item,
		"warehouse": warehouse,
		"n_pairs": n_pairs,
		"pair_outbounds": [p.get("outbound_document") for p in pairs],
		"pair_inbounds": [p.get("inbound_document") for p in pairs],
		"optimizer_statuses": sorted(
			{str(p.get("optimizer_status") or p.get("status") or "") for p in pairs}
		),
		"graph": {
			"n_nodes": len(graph.get("nodes") or []),
			"n_edges": graph.get("edge_count"),
			"by_edge_type": graph.get("by_edge_type"),
			"requires_joint_campaign": graph.get("requires_joint_campaign"),
			"dependency_depth": graph.get("dependency_depth"),
			"bridge_nodes": graph.get("bridge_nodes"),
			"shared_roots": graph.get("shared_roots"),
			"isolated_roots": graph.get("isolated_roots"),
			"multi_root_campaigns": graph.get("multi_root_campaigns"),
		},
		"proposed_times": {k: str(v) for k, v in proposed.items()},
		"moves": [
			{
				"document": m.get("document"),
				"old": str(m.get("old")) if m.get("old") else None,
				"new": str(m.get("new")) if m.get("new") else None,
				"synthesized": m.get("synthesized"),
				"optimizer_status": m.get("optimizer_status"),
			}
			for m in move_rows
		],
		"synth_notes": synth_notes,
		"expansion": {
			"rounds": (expansion or {}).get("rounds"),
			"ok": (expansion or {}).get("ok"),
			"notes": (expansion or {}).get("notes"),
		}
		if expansion
		else None,
		"from_datetime": analysis.get("from_datetime"),
		"simulation": {
			k: sim.get(k)
			for k in (
				"ok",
				"row_count",
				"first_divergence",
				"final_qty_unchanged",
				"final_qty_current",
				"final_qty_proposed",
				"idempotent",
				"expected_bin",
				"negative_qty_vouchers",
				"negative_incoming_vouchers",
				"exploded_rate_vouchers",
				"affected_batches",
				"reason",
			)
		},
		"validation": decision,
		"planner_status": ps,
		"eligible": bool(decision.get("eligible"))
		and ps in (READY_WAREHOUSE_CAMPAIGN, READY_WAREHOUSE_REPLAY),
		"reason": decision.get("reason"),
		"required_action": decision.get("required_action"),
		"expected_replay": int(sim.get("row_count") or 0),
		"expected_sql": sql,
		"expected_runtime_seconds": runtime,
		"expected_risk": risk,
		"rollback_scope": "savepoint_identity_campaign",
		"dependency_depth": int(graph.get("dependency_depth") or 0),
		"group_class": "SAFE_GROUP" if ps == READY_WAREHOUSE_CAMPAIGN else "UNSAFE_GROUP",
		"pairs": pairs,
	}


def _as_safe_group(campaign: dict) -> dict:
	return {
		"group_id": campaign.get("campaign_id"),
		"group_class": "SAFE_GROUP",
		"n_roots": campaign.get("n_pairs"),
		"repair_order": campaign.get("pair_outbounds"),
		"affected_identities": [{"item": campaign.get("item"), "warehouse": campaign.get("warehouse")}],
		"estimated_sql_updates": campaign.get("expected_sql"),
		"estimated_sle_replay": campaign.get("expected_replay"),
		"estimated_runtime_seconds": campaign.get("expected_runtime_seconds"),
		"risk": campaign.get("expected_risk"),
		"rollback_scope": campaign.get("rollback_scope"),
		"dependency_depth": campaign.get("dependency_depth"),
		"planner_status": campaign.get("planner_status"),
	}


def _load_posting_order_rows(company: str, *, scan: dict | None = None) -> list[dict]:
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	scan = scan or run_full_history_scan(company=company)
	cache: dict = {}
	out = []
	for raw in scan.get("rows") or []:
		out.append(attach_plan(dict(raw), cache=cache))
	return out
