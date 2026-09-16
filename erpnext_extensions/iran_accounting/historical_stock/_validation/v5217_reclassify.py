# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.17 — reclassify Posting Order / I4 MANUAL with Warehouse Engine."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5217"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name, data):
	path = os.path.join(ARTIFACT, name)
	os.makedirs(os.path.dirname(path) or ARTIFACT, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def reclassify_posting_order():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner import plan_warehouse_repair
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	scan = run_full_history_scan(company=COMPANY)
	rows = scan.get("rows") or []
	buckets = defaultdict(list)
	for r in rows:
		ps = str(r.get("planner_status") or "")
		st = str(r.get("status") or "")
		bucket = "MANUAL"
		if "WAREHOUSE" in ps or st == "ELIGIBLE":
			planned = plan_warehouse_repair(r)
			wps = planned.get("planner_status")
			r["warehouse_planner_status"] = wps
			r["warehouse_eligible"] = planned.get("eligible")
			if planned.get("eligible"):
				bucket = "READY_WAREHOUSE_REPLAY"
			elif wps == "WAREHOUSE_REAL_SHORTAGE":
				bucket = "REAL_STOCK_SHORTAGE"
			elif "AMBIGUOUS" in str(wps):
				bucket = "AMBIGUOUS"
			elif "POISON" in str(wps):
				bucket = "MANUAL"
			else:
				bucket = "MANUAL"
		elif r.get("eligible") or ps in READY_STATUSES or "READY_BATCH" in ps:
			bucket = "READY_BATCH_SCOPED" if "BATCH" in ps else "TRUE_REPAIRABLE"
		elif "READY_IDENTITY" in ps:
			bucket = "READY_IDENTITY_SCOPED"
		elif "SHORTAGE" in st:
			bucket = "REAL_STOCK_SHORTAGE"
		elif "NO_REPAIR" in ps or st == "NO_REPAIR_NEEDED":
			bucket = "NO_REPAIR_PATH"
		elif "AMBIGUOUS" in ps or st in ("LATER_INBOUND_UNRELATED", "AMBIGUOUS_DEPENDENCY"):
			bucket = "AMBIGUOUS"
		buckets[bucket].append(r)

	out = {
		"collected_at": datetime.now(timezone.utc).isoformat(),
		"total": len(rows),
		"by_bucket": {k: len(v) for k, v in buckets.items()},
		"ready_warehouse_replay": len(buckets.get("READY_WAREHOUSE_REPLAY") or []),
		"formerly_escalation_now_ready": len(buckets.get("READY_WAREHOUSE_REPLAY") or []),
		"formerly_escalation_now_shortage": sum(
			1
			for r in rows
			if "WAREHOUSE" in str(r.get("planner_status") or "")
			and r.get("warehouse_planner_status") == "WAREHOUSE_REAL_SHORTAGE"
		),
	}
	_dump("posting_order_reclassify.json", out)
	return out


def analyze_manual_i4():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from frappe.utils import nowdate, flt
	import frappe

	scan = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date=nowdate(), limit=5000)
	manual = [r for r in scan.get("rows") or [] if r.get("i4_status") == "MANUAL"]
	patterns = Counter()
	details = []
	for r in manual:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			r.get("sle"),
			["actual_qty", "qty_after_transaction", "stock_value", "incoming_rate"],
			as_dict=True,
		)
		blocker = "AMBIGUOUS_HISTORY"
		if r.get("previous_healthy") is False:
			blocker = "POISONED_OPENING"
		elif sle and abs(flt(sle.actual_qty)) > 0.0001 and flt(sle.actual_qty) < 0:
			# outbound with qty_after 0 residual — qty chain usually broken
			blocker = "QTY_CHAIN_BREAK"
		elif sle and abs(flt(sle.actual_qty)) <= 0.0001:
			blocker = "MISSING_RATE_SOURCE"
		elif not r.get("pz_residual_clears_in_sim"):
			blocker = "QTY_CHAIN_BREAK"
		patterns[blocker] += 1
		details.append(
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"blocker": blocker,
				"prev_ok": r.get("previous_healthy"),
				"clears": r.get("pz_residual_clears_in_sim"),
			}
		)
	waiting = [r for r in scan.get("rows") or [] if r.get("i4_status") == "WAITING_I4"]
	out = {
		"manual_count": len(manual),
		"waiting_count": len(waiting),
		"manual_patterns": dict(patterns),
		"details": details,
		"warehouse_promotable": 0,  # qty-chain / poisoned openings — not MA-only
		"note": "MANUAL I4 are qty-chain or poisoned openings; Warehouse Engine does not unlock them",
	}
	_dump("i4_manual_analysis.json", out)
	return out


def run():
	po = reclassify_posting_order()
	i4 = analyze_manual_i4()
	return {"posting_order": po, "i4": i4}
