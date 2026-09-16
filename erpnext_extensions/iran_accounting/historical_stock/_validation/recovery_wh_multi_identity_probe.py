# Copyright (c) 2026 — multi-identity safety probe for warehouse campaign
from __future__ import annotations

import json


COMPANY = "اسپاد فارمد دارو"
ITEM = "30300014"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		CAMPAIGN_ELIGIBLE_OPTS,
		merge_proposed_times,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.simulator import (
		simulate_warehouse_replay,
	)
	import frappe

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	rows = []
	for raw in scan.get("rows") or []:
		if (raw.get("item") or raw.get("item_code")) != ITEM:
			continue
		if raw.get("warehouse") != WH:
			continue
		opt = str(raw.get("optimizer_status") or raw.get("status") or "")
		if opt not in CAMPAIGN_ELIGIBLE_OPTS:
			continue
		rows.append(attach_plan(dict(raw), cache=cache))

	proposed, moves, from_dt, notes = merge_proposed_times(rows)
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
		sim = simulate_warehouse_replay(i.item_code, i.warehouse, from_dt, proposed_times=proposed)
		neg = sim.get("negative_qty_vouchers") or []
		clears = bool(
			sim.get("ok")
			and sim.get("final_qty_unchanged")
			and sim.get("idempotent")
			and not neg
			and not (sim.get("negative_incoming_vouchers") or [])
			and not (sim.get("exploded_rate_vouchers") or [])
		)
		if not clears:
			all_clear = False
		sims.append(
			{
				"item": i.item_code,
				"warehouse": i.warehouse,
				"clears": clears,
				"sim_ok": sim.get("ok"),
				"final_unchanged": sim.get("final_qty_unchanged"),
				"idempotent": sim.get("idempotent"),
				"neg_qty": neg[:8],
				"neg_incoming": (sim.get("negative_incoming_vouchers") or [])[:4],
				"exploded": (sim.get("exploded_rate_vouchers") or [])[:4],
				"final_cur": sim.get("final_qty_current"),
				"final_prop": sim.get("final_qty_proposed"),
				"row_count": sim.get("row_count"),
			}
		)
	out = {
		"n_pairs": len(rows),
		"n_moves": len(moves),
		"from_dt": str(from_dt),
		"notes": notes,
		"n_identities": len(idents),
		"all_identities_clear": all_clear,
		"sims": sims,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:16000])
	return out
