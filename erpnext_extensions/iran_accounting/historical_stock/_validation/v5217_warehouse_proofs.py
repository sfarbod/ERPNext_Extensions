# Copyright (c) 2026 — controlled warehouse proofs (dev only)
"""v5.2.17 — two controlled WAREHOUSE_ESCALATION proofs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from time import perf_counter

ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5217/warehouse"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name, data):
	path = os.path.join(ARTIFACT, name)
	os.makedirs(os.path.dirname(path) or ARTIFACT, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def _escalation_rows():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	scan = run_full_history_scan(company=COMPANY)
	rows = [r for r in scan.get("rows") or [] if "WAREHOUSE" in str(r.get("planner_status") or "")]
	# Prefer fewest other-batch rewrites (parse from reason)
	def score(r):
		reason = str(r.get("reason") or "")
		import re

		m = re.search(r"(\d+)\s+other-batch", reason)
		return int(m.group(1)) if m else 999

	rows.sort(key=score)
	return rows


def run_one_proof(row, *, apply=False, label="proof1"):
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner import plan_warehouse_repair
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.replay import apply_warehouse_repair
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	t0 = perf_counter()
	before = run_full_integrity_scan(company=COMPANY, include_manufacture=False).get("dashboard")
	planned = plan_warehouse_repair(row)
	dry = apply_warehouse_repair(row, dry_run=True)
	result = {
		"label": label,
		"item": row.get("item") or row.get("item_code"),
		"warehouse": row.get("warehouse"),
		"batch": row.get("batch"),
		"inbound": row.get("inbound_document"),
		"outbound": row.get("outbound_document"),
		"planned_status": planned.get("planner_status"),
		"eligible": planned.get("eligible"),
		"dry_run": dry,
		"apply": apply,
	}
	if not apply:
		result["ok"] = bool(dry.get("ok"))
		result["elapsed_seconds"] = round(perf_counter() - t0, 2)
		_dump(f"{label}_dry.json", result)
		return result

	applied = apply_warehouse_repair(row, dry_run=False)
	after = run_full_integrity_scan(company=COMPANY, include_manufacture=False).get("dashboard")
	result.update(
		{
			"applied": applied,
			"ok": bool(applied.get("ok")),
			"before": before,
			"after": after,
			"kpi_delta": {
				k: {
					"before": before.get(k),
					"after": after.get(k),
					"delta": (after.get(k) or 0) - (before.get(k) or 0),
				}
				for k in ("Posting Order", "I4 Leftover", "Zero Rate", "Broken Bin", "Broken GL", "Integrity Score")
			},
			"elapsed_seconds": round(perf_counter() - t0, 2),
		}
	)
	_dump(f"{label}_apply.json", result)
	return result


def run(apply=True):
	rows = _escalation_rows()
	if len(rows) < 1:
		return {"ok": False, "reason": "no WAREHOUSE_ESCALATION rows"}
	# Proof 1: smallest
	p1 = run_one_proof(rows[0], apply=apply, label="proof1_smallest")
	print("proof1", p1.get("ok"), p1.get("planned_status"), p1.get("item"))
	# Proof 2: next distinct item if possible
	p2 = None
	if p1.get("ok") and apply:
		rows2 = _escalation_rows()
		second = None
		for r in rows2:
			if (r.get("item") or r.get("item_code")) != p1.get("item"):
				second = r
				break
		if not second and rows2:
			second = rows2[0]
		if second:
			p2 = run_one_proof(second, apply=True, label="proof2_second")
			print("proof2", p2.get("ok"), p2.get("planned_status"), p2.get("item"))

	summary = {
		"collected_at": datetime.now(timezone.utc).isoformat(),
		"version": "5.2.17",
		"escalation_remaining_before": len(rows),
		"proof1": {k: p1.get(k) for k in ("ok", "item", "planned_status", "eligible", "kpi_delta", "elapsed_seconds")},
		"proof2": {k: (p2 or {}).get(k) for k in ("ok", "item", "planned_status", "eligible", "kpi_delta", "elapsed_seconds")}
		if p2
		else None,
		"limited_proven": bool(p1.get("ok") and p2 and p2.get("ok")),
	}
	_dump("PROOF_SUMMARY.json", summary)
	return summary
