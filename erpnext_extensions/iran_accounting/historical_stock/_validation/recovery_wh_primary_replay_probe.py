# Copyright (c) 2026 — primary-only replay probe after campaign timestamps
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.campaign import (
		_update_posting_datetime,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_item_warehouse
	from frappe.utils import get_datetime
	import frappe

	disc = discover_warehouse_campaigns(company="اسپاد فارمد دارو")
	c = next(x for x in disc["ready_campaigns"] if x["item"] == "30300014")
	sp = "probe_" + frappe.generate_hash(length=6)
	frappe.db.savepoint(sp)
	try:
		for m in c["moves"]:
			_update_posting_datetime(m["document"], get_datetime(m["new"]))
		rep = replay_item_warehouse(
			c["item"],
			c["warehouse"],
			get_datetime(c["from_datetime"]),
			ignore_inversion_artifacts=True,
			write_vouchers=None,
			allow_unrelated_poison=True,
			trust_simulated_series=True,
		)
		out = {
			"ok": rep.get("ok"),
			"status": rep.get("status"),
			"reason": rep.get("reason"),
			"written": rep.get("written"),
			"final_qty": rep.get("final_qty"),
			"poison": rep.get("poison"),
			"keys": sorted(rep.keys()),
		}
	finally:
		frappe.db.rollback(save_point=sp)
		out["rolled_back"] = True
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:4000])
	return out
