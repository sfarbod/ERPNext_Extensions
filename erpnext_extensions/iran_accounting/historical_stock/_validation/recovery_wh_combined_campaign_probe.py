# Copyright (c) 2026 — probe combined CROSS_TIME + MIDNIGHT warehouse campaign
from __future__ import annotations

import json
from collections import defaultdict


COMPANY = "اسپاد فارمد دارو"
ITEM = "30300014"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"
INCLUDE_OPTS = (
	"CROSS_TIME_REPAIRABLE",
	"REPAIRABLE_SECONDS",
	"SAME_TIME_REPAIRABLE",
	"MIDNIGHT_REVIEW",
)


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.simulator import (
		simulate_warehouse_replay,
	)
	from frappe.utils import get_datetime

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	rows = []
	for raw in scan.get("rows") or []:
		if (raw.get("item") or raw.get("item_code")) != ITEM:
			continue
		if raw.get("warehouse") != WH:
			continue
		r = attach_plan(dict(raw), cache=cache)
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		if opt not in INCLUDE_OPTS:
			continue
		rows.append(r)

	# Subset simulations: CROSS only, MIDNIGHT only, ALL, and progressive add
	def sim_subset(label, subset):
		proposed = {}
		from_candidates = []
		pair_info = []
		for r in subset:
			for m in r.get("moves") or []:
				if m.get("document") and m.get("new"):
					proposed[m["document"]] = get_datetime(m["new"])
			if r.get("inbound_document") and r.get("proposed_inbound_time"):
				proposed.setdefault(r["inbound_document"], get_datetime(r["proposed_inbound_time"]))
			if r.get("outbound_document") and r.get("proposed_outbound_time"):
				proposed.setdefault(r["outbound_document"], get_datetime(r["proposed_outbound_time"]))
			for t in (
				r.get("current_inbound_time"),
				r.get("current_outbound_time"),
				r.get("proposed_inbound_time"),
				r.get("proposed_outbound_time"),
			):
				if t:
					from_candidates.append(get_datetime(t))
			pair_info.append(
				{
					"opt": r.get("optimizer_status") or r.get("status"),
					"in": r.get("inbound_document"),
					"out": r.get("outbound_document"),
					"moves": [
						{"doc": m.get("document"), "old": str(m.get("old")), "new": str(m.get("new"))}
						for m in (r.get("moves") or [])
					],
					"prop_in": str(r.get("proposed_inbound_time") or ""),
					"prop_out": str(r.get("proposed_outbound_time") or ""),
					"min_b": r.get("min_qty_before"),
					"min_a": r.get("min_qty_after"),
				}
			)
		if not from_candidates or not proposed:
			return {
				"label": label,
				"n_pairs": len(subset),
				"n_moves": 0,
				"ok": False,
				"reason": "no proposed times",
				"pairs": pair_info,
			}
		from_dt = min(from_candidates)
		sim = simulate_warehouse_replay(ITEM, WH, from_dt, proposed_times=proposed)
		return {
			"label": label,
			"n_pairs": len(subset),
			"n_moves": len(proposed),
			"from_dt": str(from_dt),
			"sim_ok": sim.get("ok"),
			"final_qty_unchanged": sim.get("final_qty_unchanged"),
			"idempotent": sim.get("idempotent"),
			"neg_qty": sim.get("negative_qty_vouchers") or [],
			"neg_incoming": sim.get("negative_incoming_vouchers") or [],
			"exploded": sim.get("exploded_rate_vouchers") or [],
			"row_count": sim.get("row_count"),
			"first_divergence": sim.get("first_divergence"),
			"expected_bin": sim.get("expected_bin"),
			"clears": bool(
				sim.get("ok")
				and sim.get("final_qty_unchanged")
				and sim.get("idempotent")
				and not (sim.get("negative_qty_vouchers") or [])
				and not (sim.get("negative_incoming_vouchers") or [])
				and not (sim.get("exploded_rate_vouchers") or [])
			),
			"pairs": pair_info,
		}

	cross = [r for r in rows if str(r.get("optimizer_status") or r.get("status")) == "CROSS_TIME_REPAIRABLE"]
	midnight = [r for r in rows if str(r.get("optimizer_status") or r.get("status")) == "MIDNIGHT_REVIEW"]
	out = {
		"n_rows": len(rows),
		"sims": [
			sim_subset("cross_only", cross),
			sim_subset("midnight_only", midnight),
			sim_subset("cross_plus_midnight", cross + midnight),
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:16000])
	return out
