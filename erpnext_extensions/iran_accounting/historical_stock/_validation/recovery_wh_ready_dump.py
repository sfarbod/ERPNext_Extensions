# Copyright (c) 2026 — dump ready campaign pair constraints
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)

	disc = discover_warehouse_campaigns(company="اسپاد فارمد دارو")
	out = []
	for c in disc.get("ready_campaigns") or []:
		out.append(
			{
				"item": c.get("item"),
				"warehouse": c.get("warehouse"),
				"n_moves": len(c.get("moves") or []),
				"moves": c.get("moves"),
				"proposed": c.get("proposed_times"),
				"merged": c.get("n_merged"),
				"merged_items": c.get("merged_items"),
				"shared_vouchers": c.get("shared_vouchers"),
				"pairs": [
					{
						"in": p.get("inbound_document"),
						"out": p.get("outbound_document"),
						"opt": p.get("optimizer_status") or p.get("status"),
						"cur_in": str(p.get("current_inbound_time") or ""),
						"cur_out": str(p.get("current_outbound_time") or ""),
						"prop_in": str(p.get("proposed_inbound_time") or ""),
						"prop_out": str(p.get("proposed_outbound_time") or ""),
						"moves": p.get("moves"),
						"min_b": p.get("min_qty_before"),
						"min_a": p.get("min_qty_after"),
					}
					for p in (c.get("pairs") or [])
				],
				"siblings": [
					{"item": s.get("item"), "moves": s.get("moves"), "proposed": s.get("proposed_times")}
					for s in (c.get("sibling_campaigns") or [])
				],
			}
		)
	print(json.dumps({"n": len(out), "campaigns": out}, ensure_ascii=False, indent=2, default=str)[:12000])
	return out
