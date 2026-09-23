# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5 reclass probe after foreign-PZ waiting upgrade."""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"


def run(*, wrong_limit: int = 5000):
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import (
		count_wrong_rate_buckets,
		wrong_rate_bucket,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manual_reason import summarize_manual_groups
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.root_graph import build_zero_wrong_root_graph

	w = scan_wrong_rates(company=COMPANY, limit=wrong_limit)
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	wb = count_wrong_rate_buckets(wrows)
	manual = [r for r in wrows if wrong_rate_bucket(r) == "manual"]
	waiting = [r for r in wrows if wrong_rate_bucket(r) == "waiting"]
	ready = [
		r
		for r in wrows
		if wrong_rate_bucket(r) == "ready"
		or (
			(r.get("eligible") or str(r.get("planner_status") or "").startswith("READY"))
			and int(r.get("sql_updates") or 0) > 0
		)
	]
	ms = summarize_manual_groups(manual)

	z = scan_zero_rate_rows(company=COMPANY, limit=8000)
	i4 = scan_i4_leftover(company=COMPANY, limit=5000)
	po = run_full_history_scan(company=COMPANY)
	porows = [attach_plan(dict(r)) for r in (po.get("rows") or [])]
	po_act = [
		r for r in porows if (r.get("optimizer_status") or r.get("status")) not in ("NO_REPAIR_NEEDED",)
	]
	# PO EXACT but not READY — tool limit candidates
	po_exact_blocked = [
		r
		for r in po_act
		if str(r.get("confidence") or "") == "EXACT"
		and not (
			str(r.get("planner_status") or "").startswith("READY")
			or r.get("planner_status") in READY_STATUSES
		)
	]
	po_exact_why = Counter(
		f"{r.get('planner_status')}|{r.get('optimizer_status') or r.get('status')}"
		for r in po_exact_blocked
	)

	g = build_zero_wrong_root_graph(z.get("rows") or [], wrows)

	out = {
		"wrong": {
			"buckets": wb,
			"ready_n": len(ready),
			"waiting_n": len(waiting),
			"manual_n": len(manual),
			"manual_by_reason": ms.get("by_reason"),
			"manual_by_lane": ms.get("by_lane"),
			"manual_root_chains": ms.get("root_chains"),
			"manual_identities": ms.get("unique_item_warehouse"),
			"ready_sample": [
				{
					"v": r.get("voucher"),
					"p": r.get("purpose"),
					"ps": r.get("planner_status"),
					"conf": r.get("confidence"),
					"sql": r.get("sql_updates"),
				}
				for r in ready[:10]
			],
		},
		"zero": {
			"raw": z.get("raw_count"),
			"recon": z.get("reconstructable_count"),
			"waiting": z.get("waiting_upstream_count"),
			"by_status": z.get("by_status"),
		},
		"i4": {
			"count": i4.get("count"),
			"raw": i4.get("raw_count"),
			"roots": i4.get("root_identity_count"),
			"by_status": i4.get("by_status"),
			"ready": i4.get("ready_count"),
		},
		"posting_order": {
			"raw": len(porows),
			"actionable": len(po_act),
			"exact_blocked_n": len(po_exact_blocked),
			"exact_blocked_why": dict(po_exact_why.most_common(15)),
			"by_optimizer": dict(Counter(str(r.get("optimizer_status") or r.get("status")) for r in porows)),
			"ready_n": sum(
				1
				for r in po_act
				if str(r.get("planner_status") or "").startswith("READY")
				and int(r.get("sql_updates") or 0) > 0
			),
		},
		"graph": {k: g[k] for k in g if not isinstance(g[k], (list, dict))},
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
