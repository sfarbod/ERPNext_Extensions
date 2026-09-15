# Copyright (c) 2026 — apply READY Posting Order / Warehouse SAFE_GROUP
from __future__ import annotations

import json
from time import perf_counter


COMPANY = "اسپاد فارمد دارو"


def run(*, max_n=5, dry_run=0):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		READY_STATUSES,
		attach_plan,
	)
	from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
		SAFE_GROUP,
		cluster_independent_roots,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.replay import (
		apply_warehouse_repair,
	)

	t0 = perf_counter()
	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	ready = []
	for raw in scan.get("rows") or []:
		r = attach_plan(dict(raw), cache=cache)
		ps = str(r.get("planner_status") or "")
		if ps in READY_STATUSES and int(r.get("sql_updates") or 0) > 0 and r.get("eligible"):
			ready.append(r)

	if not ready:
		out = {"ok": True, "n_ready": 0, "repaired": [], "failed": [], "elapsed": round(perf_counter() - t0, 3)}
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	# Prefer independent identities for SAFE_GROUP
	for r in ready:
		r.setdefault("voucher", r.get("outbound_document") or r.get("inbound_document"))
		r.setdefault("topic", "POSTING_ORDER")
	clusters = cluster_independent_roots(ready, max_cluster=int(max_n))
	group = clusters.get("recommended_first_group") or {}
	order = group.get("roots") or group.get("repair_order") or []
	voucher_order = []
	for root in order[: int(max_n)]:
		vn = root if isinstance(root, str) else (root.get("voucher") if isinstance(root, dict) else None)
		if vn and vn not in voucher_order:
			voucher_order.append(vn)
	if not voucher_order:
		# fallback: unique outbound vouchers
		for r in ready:
			vn = r.get("outbound_document") or r.get("voucher")
			if vn and vn not in voucher_order:
				voucher_order.append(vn)
			if len(voucher_order) >= int(max_n):
				break

	repaired, failed = [], []
	seen = set()
	for vn in voucher_order:
		matches = [r for r in ready if (r.get("outbound_document") or r.get("voucher")) == vn]
		for r in matches:
			key = (r.get("inbound_document"), r.get("outbound_document"), r.get("item"), r.get("warehouse"))
			if key in seen:
				continue
			seen.add(key)
			ps = str(r.get("planner_status") or "")
			try:
				if ps == "READY_WAREHOUSE_REPLAY":
					res = apply_warehouse_repair(r, dry_run=bool(int(dry_run)))
				else:
					res = apply_repairs([r], dry_run=bool(int(dry_run)))
					if isinstance(res, dict):
						blocked = res.get("blocked") or []
						applied = res.get("applied") or []
						res = {
							"ok": bool(applied) and not blocked and not res.get("aborted"),
							"reason": (blocked[0].get("error") if blocked else None)
							or res.get("reason")
							or res.get("skip_reason"),
							**{k: res.get(k) for k in ("aborted", "applied", "blocked") if k in res},
						}
				entry = {
					"inbound": r.get("inbound_document"),
					"outbound": r.get("outbound_document"),
					"item": r.get("item"),
					"ps": ps,
					"ok": bool(res.get("ok")),
					"reason": res.get("reason") or res.get("error"),
					"planner_status": res.get("planner_status"),
				}
				if entry["ok"]:
					repaired.append(entry)
				else:
					failed.append(entry)
			except Exception as e:
				failed.append(
					{
						"inbound": r.get("inbound_document"),
						"outbound": r.get("outbound_document"),
						"item": r.get("item"),
						"ps": ps,
						"ok": False,
						"reason": f"{type(e).__name__}: {e}",
					}
				)

	out = {
		"ok": True,
		"group_class": group.get("group_class"),
		"n_ready_before": len(ready),
		"n_attempted": len(repaired) + len(failed),
		"n_repaired": len(repaired),
		"n_failed": len(failed),
		"repaired": repaired,
		"failed": failed,
		"elapsed": round(perf_counter() - t0, 3),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
