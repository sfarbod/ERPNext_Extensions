# Copyright (c) 2026 — apply remaining ready campaigns as one joint write set
from __future__ import annotations

import json
from datetime import timedelta


COMPANY = "اسپاد فارمد دارو"


def run():
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
		verify_touched_identities_from_times,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.campaign import (
		_update_posting_datetime,
		_selective_gl,
		_assert_outbound_non_negative,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_item_warehouse
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import sync_transfer_incoming_rates
	from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG
	from frappe.utils import get_datetime
	import frappe

	disc = discover_warehouse_campaigns(company=COMPANY)
	ready = list(disc.get("ready_campaigns") or [])
	if not ready:
		print(json.dumps({"ok": True, "n": 0, "message": "no ready campaigns"}))
		return {"ok": True, "n": 0}

	# Merge all proposed times; last-write by chronological max for conflicts
	proposed = {}
	from_candidates = []
	pair_vouchers = set()
	for c in ready:
		for m in c.get("moves") or []:
			if m.get("document") and m.get("new"):
				proposed[m["document"]] = get_datetime(m["new"])
		for t in (c.get("from_datetime"),):
			if t:
				from_candidates.append(get_datetime(t))
		pair_vouchers |= set(c.get("pair_inbounds") or []) | set(c.get("pair_outbounds") or [])

	from_dt = min(from_candidates)
	# Verify joint set
	multi = verify_touched_identities_from_times(proposed, from_dt)
	if not multi.get("all_clear"):
		out = {"ok": False, "reason": multi.get("reason"), "n_ready": len(ready), "multi": {"n_failed": multi.get("n_failed"), "n_identities": multi.get("n_identities")}}
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:4000])
		return out

	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	sp = "whjoint_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(sp)
	try:
		for doc, dt in proposed.items():
			_update_posting_datetime(doc, dt)
		replays = []
		# Primary identities first (campaign items), then sisters
		idents = list(multi.get("identities") or [])
		primary_keys = {(c.get("item"), c.get("warehouse")) for c in ready}
		idents.sort(key=lambda i: 0 if (i.get("item"), i.get("warehouse")) in primary_keys else 1)
		for ident in idents:
			if not ident.get("clears"):
				continue
			is_primary = (ident.get("item"), ident.get("warehouse")) in primary_keys
			write_set = None if is_primary or int(ident.get("row_count") or 0) <= 250 else set(proposed)
			rep = replay_item_warehouse(
				ident["item"],
				ident["warehouse"],
				from_dt,
				ignore_inversion_artifacts=True,
				write_vouchers=write_set,
				allow_unrelated_poison=True,
				trust_simulated_series=True,
			)
			replays.append({"item": ident["item"], "warehouse": ident["warehouse"], "ok": rep.get("ok"), "written": rep.get("written")})
			if not rep.get("ok"):
				frappe.db.rollback(save_point=sp)
				out = {"ok": False, "reason": f"replay failed {ident['item']}: {rep.get('reason')}", "replays": replays}
				print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:4000])
				return out
		for vn in pair_vouchers:
			if vn and frappe.db.exists("Stock Entry", vn):
				sync_transfer_incoming_rates(vn)
		for c in ready:
			for outv in c.get("pair_outbounds") or []:
				_assert_outbound_non_negative(outv, c.get("item"), c.get("warehouse"))
		gl = _selective_gl(sorted(set(proposed) | pair_vouchers))
		frappe.db.commit()
		# Re-discover
		after = discover_warehouse_campaigns(company=COMPANY)
		out = {
			"ok": True,
			"n_campaigns_merged": len(ready),
			"n_moves": len(proposed),
			"n_identities": multi.get("n_identities"),
			"replays_ok": sum(1 for r in replays if r.get("ok")),
			"gl_rebuilt": gl.get("rebuilt"),
			"after_ready": after.get("n_ready_campaigns"),
			"items": [c.get("item") for c in ready],
		}
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:4000])
		return out
	except Exception as exc:
		frappe.db.rollback(save_point=sp)
		out = {"ok": False, "error": str(exc)}
		print(json.dumps(out, ensure_ascii=False, indent=2))
		return out
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False
