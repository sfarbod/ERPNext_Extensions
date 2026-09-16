# Copyright (c) 2026 — iterative cross-identity campaign expansion probe
from __future__ import annotations

import json
from datetime import timedelta


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
	from frappe.utils import get_datetime
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
	rounds = []
	cleared = False
	for round_i in range(10):
		vouchers = list(proposed.keys())
		idents = frappe.db.sql(
			"""
			SELECT DISTINCT item_code, warehouse FROM `tabStock Ledger Entry`
			WHERE voucher_no IN %s AND is_cancelled=0
			""",
			(tuple(vouchers),),
			as_dict=True,
		)
		new_negs = []
		round_sims = []
		all_clear = True
		for i in idents:
			base = simulate_warehouse_replay(i.item_code, i.warehouse, from_dt, proposed_times=None)
			prop = simulate_warehouse_replay(i.item_code, i.warehouse, from_dt, proposed_times=proposed)
			base_neg = set(base.get("negative_qty_vouchers") or [])
			prop_neg = set(prop.get("negative_qty_vouchers") or [])
			introduced = sorted(prop_neg - base_neg)
			poison = (prop.get("negative_incoming_vouchers") or []) or (prop.get("exploded_rate_vouchers") or [])
			clears = bool(
				prop.get("ok")
				and prop.get("final_qty_unchanged")
				and prop.get("idempotent")
				and not introduced
				and not poison
			)
			if not clears:
				all_clear = False
				for vn in introduced:
					new_negs.append((i.item_code, i.warehouse, vn))
			round_sims.append(
				{
					"item": i.item_code,
					"warehouse": i.warehouse,
					"clears": clears,
					"introduced": introduced,
					"poison": poison[:4],
				}
			)
		rounds.append({"round": round_i, "n_moves": len(proposed), "all_clear": all_clear, "sims": round_sims})
		if all_clear:
			cleared = True
			break
		if not new_negs:
			break
		bump = 0
		for it, wh, vn in new_negs:
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
			bump += 1
			proposed[vn] = latest + timedelta(seconds=1 + bump)

	out = {
		"cleared": cleared,
		"n_rounds": len(rounds),
		"final_n_moves": len(proposed),
		"from_dt": str(from_dt),
		"rounds": rounds,
		"final_proposed_sample": {k: str(v) for k, v in list(sorted(proposed.items(), key=lambda x: x[1]))[:20]},
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:16000])
	return out
