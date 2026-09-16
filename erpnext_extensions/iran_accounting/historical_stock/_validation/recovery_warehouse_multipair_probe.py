# Copyright (c) 2026 — probe multi-pair warehouse campaign on Quarantine SFG
from __future__ import annotations

import json
from collections import defaultdict


COMPANY = "اسپاد فارمد دارو"


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.simulator import (
		simulate_warehouse_replay,
	)
	from erpnext_extensions.iran_accounting.historical_stock.scope import evaluate_minimal_scope
	from frappe.utils import get_datetime

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	# Group CROSS_TIME / warehouse-escalation candidates by (item, warehouse)
	by_id = defaultdict(list)
	for raw in scan.get("rows") or []:
		r = attach_plan(dict(raw), cache=cache)
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		ps = str(r.get("planner_status") or "")
		if opt not in ("CROSS_TIME_REPAIRABLE", "REPAIRABLE_SECONDS", "SAME_TIME_REPAIRABLE"):
			continue
		if not r.get("inbound_document") or not r.get("outbound_document"):
			continue
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse")
		if not item or not wh:
			continue
		by_id[(item, wh)].append(r)

	out = {"identities": [], "multi_pair_sims": []}
	for (item, wh), rows in sorted(by_id.items(), key=lambda x: -len(x[1])):
		if len(rows) < 1:
			continue
		entry = {
			"item": item,
			"warehouse": wh,
			"n_pairs": len(rows),
			"pairs": [
				{
					"in": r.get("inbound_document"),
					"out": r.get("outbound_document"),
					"batch": r.get("batch") or r.get("batch_no"),
					"ps": r.get("planner_status"),
					"opt": r.get("optimizer_status") or r.get("status"),
					"conf": r.get("confidence"),
					"min_before": r.get("min_qty_before"),
					"min_after": r.get("min_qty_after"),
				}
				for r in rows
			],
		}
		out["identities"].append(entry)
		if len(rows) < 2:
			continue

		# Build combined proposed times from all pairs' moves
		proposed = {}
		from_candidates = []
		for r in rows:
			for m in r.get("moves") or []:
				if m.get("document") and m.get("new"):
					proposed[m["document"]] = get_datetime(m["new"])
			if r.get("inbound_document") and r.get("proposed_inbound_time"):
				proposed.setdefault(r["inbound_document"], get_datetime(r["proposed_inbound_time"]))
			if r.get("outbound_document") and r.get("proposed_outbound_time"):
				proposed.setdefault(r["outbound_document"], get_datetime(r["proposed_outbound_time"]))
			for t in (r.get("current_inbound_time"), r.get("current_outbound_time"), r.get("proposed_inbound_time")):
				if t:
					from_candidates.append(get_datetime(t))
		if not from_candidates or not proposed:
			continue
		from_dt = min(from_candidates)
		sim = simulate_warehouse_replay(item, wh, from_dt, proposed_times=proposed)
		# Also check warehouse qty min from combined times via first row scope with patched moves
		# Simplified: use sim negative_qty
		out["multi_pair_sims"].append(
			{
				"item": item,
				"warehouse": wh,
				"n_pairs": len(rows),
				"n_moves": len(proposed),
				"from_dt": str(from_dt),
				"sim_ok": sim.get("ok"),
				"final_qty_unchanged": sim.get("final_qty_unchanged"),
				"idempotent": sim.get("idempotent"),
				"neg_qty": sim.get("negative_qty_vouchers") or [],
				"neg_incoming": sim.get("negative_incoming_vouchers") or [],
				"exploded": sim.get("exploded_rate_vouchers") or [],
				"row_count": sim.get("row_count"),
				"affected_batches": (sim.get("affected_batches") or [])[:8],
				"first_divergence": sim.get("first_divergence"),
				"expected_bin": sim.get("expected_bin"),
				"reason": sim.get("reason"),
			}
		)

	out["n_identities"] = len(out["identities"])
	out["n_multi"] = len(out["multi_pair_sims"])
	out["multi_clears"] = [
		s
		for s in out["multi_pair_sims"]
		if s.get("sim_ok")
		and s.get("final_qty_unchanged")
		and s.get("idempotent")
		and not s.get("neg_qty")
		and not s.get("neg_incoming")
		and not s.get("exploded")
	]
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
