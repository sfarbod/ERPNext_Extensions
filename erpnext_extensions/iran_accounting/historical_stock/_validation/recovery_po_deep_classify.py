# Copyright (c) 2026 — deep classify remaining Posting Order for deterministic unlocks
from __future__ import annotations

import json
from collections import Counter, defaultdict


COMPANY = "اسپاد فارمد دارو"


def run(*, sample=8):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.shortage import (
		search_hidden_inbound,
	)
	import frappe

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	by_opt = defaultdict(list)
	for raw in scan.get("rows") or []:
		r = attach_plan(dict(raw), cache=cache)
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		by_opt[opt].append(r)

	out = {"counts": {k: len(v) for k, v in by_opt.items()}, "classes": {}}

	# MIDNIGHT — can simulation clear with same-day second bump?
	midnight = by_opt.get("MIDNIGHT_REVIEW") or by_opt.get("MANUAL_REVIEW_MIDNIGHT") or []
	mid_samples = []
	for r in midnight[: int(sample)]:
		mid_samples.append(
			{
				"in": r.get("inbound_document"),
				"out": r.get("outbound_document"),
				"item": r.get("item"),
				"wh": r.get("warehouse"),
				"ps": r.get("planner_status"),
				"conf": r.get("confidence"),
				"reason": (r.get("reason") or "")[:140],
				"cur_in": r.get("current_inbound_time") or r.get("inbound_posting_datetime"),
				"cur_out": r.get("current_outbound_time") or r.get("outbound_posting_datetime"),
				"prop_in": r.get("proposed_inbound_time"),
				"prop_out": r.get("proposed_outbound_time"),
				"min_before": r.get("min_qty_before") or r.get("minimum_qty_before"),
				"moves": r.get("moves") or r.get("preview_moves"),
			}
		)
	out["classes"]["MIDNIGHT_REVIEW"] = {"n": len(midnight), "samples": mid_samples}

	# LATER_INBOUND — is later inbound same batch / transferable?
	later = by_opt.get("LATER_INBOUND_UNRELATED") or []
	later_samples = []
	for r in later[: int(sample)]:
		item, wh = r.get("item"), r.get("warehouse")
		batch = r.get("batch") or r.get("batch_no")
		out_v = r.get("outbound_document")
		# Find later positive SLE after outbound
		later_in = frappe.db.sql(
			"""
			SELECT voucher_no, actual_qty, posting_datetime, batch_no, voucher_type
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND actual_qty>0
			  AND posting_datetime > (
			    SELECT posting_datetime FROM `tabStock Ledger Entry`
			    WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			    ORDER BY posting_datetime LIMIT 1
			  )
			ORDER BY posting_datetime ASC
			LIMIT 5
			""",
			(item, wh, out_v, item, wh),
			as_dict=True,
		) if out_v and item and wh else []
		later_samples.append(
			{
				"out": out_v,
				"in": r.get("inbound_document"),
				"item": item,
				"wh": (wh or "")[:40],
				"batch": batch,
				"ps": r.get("planner_status"),
				"conf": r.get("confidence"),
				"later_inbounds": [
					{
						"v": x.voucher_no,
						"qty": float(x.actual_qty or 0),
						"dt": str(x.posting_datetime),
						"batch": x.batch_no,
						"vt": x.voucher_type,
						"same_batch": bool(batch and x.batch_no == batch),
					}
					for x in later_in
				],
			}
		)
	out["classes"]["LATER_INBOUND_UNRELATED"] = {"n": len(later), "samples": later_samples}

	# NO_REPAIR_NEEDED still stamped AMBIGUOUS — why in dashboard count?
	norepair = by_opt.get("NO_REPAIR_NEEDED") or []
	nr_ps = Counter(str(r.get("planner_status") or "") for r in norepair)
	nr_conf = Counter(str(r.get("confidence") or "") for r in norepair)
	out["classes"]["NO_REPAIR_NEEDED"] = {
		"n": len(norepair),
		"by_ps": dict(nr_ps),
		"by_conf": dict(nr_conf),
		"sample_reasons": [
			{"out": r.get("outbound_document"), "ps": r.get("planner_status"), "conf": r.get("confidence"), "reason": (r.get("reason") or "")[:120]}
			for r in norepair[:6]
		],
	}

	# REAL_STOCK_SHORTAGE — hidden inbound probe on sample
	shortage = by_opt.get("REAL_STOCK_SHORTAGE") or []
	hid = Counter()
	hid_samples = []
	for r in shortage[:30]:
		res = search_hidden_inbound(r)
		hid[res.get("outcome")] += 1
		if res.get("actionable") and len(hid_samples) < 6:
			hid_samples.append(
				{
					"out": r.get("outbound_document"),
					"item": r.get("item"),
					"outcome": res.get("outcome"),
					"actionable_n": len(res.get("actionable") or []),
					"actionable": (res.get("actionable") or [])[:3],
					"reason": (res.get("reason") or "")[:120],
				}
			)
	out["classes"]["REAL_STOCK_SHORTAGE"] = {
		"n": len(shortage),
		"hidden_probe_n": min(30, len(shortage)),
		"hidden_outcomes": dict(hid),
		"actionable_samples": hid_samples,
	}

	# CROSS_ITEM
	cross = by_opt.get("CROSS_ITEM_CONFLICT") or []
	out["classes"]["CROSS_ITEM_CONFLICT"] = {
		"n": len(cross),
		"samples": [
			{
				"in": r.get("inbound_document"),
				"out": r.get("outbound_document"),
				"item": r.get("item"),
				"wh": (r.get("warehouse") or "")[:40],
				"ps": r.get("planner_status"),
				"conf": r.get("confidence"),
				"reason": (r.get("reason") or "")[:140],
			}
			for r in cross
		],
	}

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
