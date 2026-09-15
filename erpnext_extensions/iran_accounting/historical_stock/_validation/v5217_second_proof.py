# Copyright (c) 2026 — find second READY warehouse proof
from __future__ import annotations

import json

from erpnext_extensions.iran_accounting.historical_stock._validation.v5217_warehouse_proofs import (
	_dump,
	_escalation_rows,
	run_one_proof,
)


def run(apply=True):
	rows = _escalation_rows()
	print("remaining escalation", len(rows))
	chosen = None
	plans = []
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner import plan_warehouse_repair

	for r in rows:
		p = plan_warehouse_repair(r)
		plans.append(
			{
				"item": r.get("item") or r.get("item_code"),
				"batch": r.get("batch"),
				"status": p.get("planner_status"),
				"eligible": p.get("eligible"),
				"reason": str(p.get("reason") or "")[:120],
			}
		)
		if p.get("eligible") and p.get("planner_status") == "READY_WAREHOUSE_REPLAY" and not chosen:
			chosen = r
	_dump("remaining_plans.json", {"count": len(plans), "plans": plans})
	print(json.dumps(plans, ensure_ascii=False, indent=2))
	if not chosen:
		return {"ok": False, "reason": "no second READY_WAREHOUSE_REPLAY", "plans": plans}
	result = run_one_proof(chosen, apply=apply, label="proof2_ready")
	print("proof2", result.get("ok"), result.get("planned_status"), result.get("item"))
	return result
