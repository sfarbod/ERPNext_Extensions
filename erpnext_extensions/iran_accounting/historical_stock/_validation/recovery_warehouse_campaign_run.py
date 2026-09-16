# Copyright (c) 2026 — discover + dry-run + apply warehouse campaigns
from __future__ import annotations

import json


COMPANY = "اسپاد فارمد دارو"


def discover():
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)

	out = discover_warehouse_campaigns(company=COMPANY)
	summary = {
		k: out.get(k)
		for k in (
			"n_eligible_pairs",
			"n_identities",
			"n_ready_campaigns",
			"n_shortage",
			"n_ambiguous",
			"n_campaign_required",
			"n_oscillation_blocked",
			"graph_summary",
			"global_solver",
			"elapsed_seconds",
		)
	}
	summary["ready"] = [
		{
			"campaign_id": c.get("campaign_id"),
			"item": c.get("item"),
			"warehouse": c.get("warehouse"),
			"n_pairs": c.get("n_pairs"),
			"n_moves": len(c.get("moves") or []),
			"solver": c.get("solver"),
			"global_planner_status": c.get("global_planner_status"),
			"expansion_rounds": (c.get("expansion") or {}).get("rounds"),
			"n_identities": (c.get("multi_identity") or {}).get("n_identities") or c.get("n_identities"),
			"expected_sql": c.get("expected_sql"),
			"expected_replay": c.get("expected_replay"),
			"planner_status": c.get("planner_status"),
			"reason": (c.get("reason") or "")[:160],
			"moves": c.get("moves"),
		}
		for c in (out.get("ready_campaigns") or [])
	]
	summary["others"] = [
		{
			"item": c.get("item"),
			"warehouse": (c.get("warehouse") or "")[:40],
			"n_pairs": c.get("n_pairs"),
			"status": c.get("planner_status") or c.get("global_planner_status"),
			"solver": c.get("solver"),
			"reason": (c.get("reason") or "")[:120],
		}
		for c in (out.get("campaigns") or [])
		if not (
			c.get("eligible")
			and c.get("planner_status")
			in ("READY_WAREHOUSE_CAMPAIGN", "READY_WAREHOUSE_REPLAY", "READY_GLOBAL_WAREHOUSE_SOLVER")
		)
	]
	print(json.dumps(summary, ensure_ascii=False, indent=2, default=str)[:12000])
	return out


def dry_run_ready():
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.campaign import (
		apply_warehouse_campaign,
	)

	disc = discover_warehouse_campaigns(company=COMPANY)
	results = []
	for c in disc.get("ready_campaigns") or []:
		results.append(apply_warehouse_campaign(c, dry_run=True))
	out = {"n": len(results), "results": results}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out


def apply_ready(limit=None):
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.campaign import (
		apply_warehouse_campaign,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.global_solver import (
		READY_GLOBAL_WAREHOUSE_SOLVER,
		apply_global_solution,
	)

	disc = discover_warehouse_campaigns(company=COMPANY)
	ready = list(disc.get("ready_campaigns") or [])
	ready.sort(key=lambda c: (-int(c.get("n_pairs") or 0), -int(c.get("n_moves") or 0)))
	if limit is not None:
		ready = ready[: int(limit)]

	results = []
	for c in ready:
		# Global shared-voucher solutions apply via the global solver path
		if c.get("solver") == "global_shared_voucher" or c.get("global_planner_status") == READY_GLOBAL_WAREHOUSE_SOLVER:
			# Restore global status for apply_global_solution eligibility check
			sol = dict(c)
			sol["planner_status"] = READY_GLOBAL_WAREHOUSE_SOLVER
			sol["eligible"] = True
			results.append(apply_global_solution(sol, dry_run=False))
		else:
			results.append(apply_warehouse_campaign(c, dry_run=False))

	out = {
		"n": len(results),
		"results": [
			{
				k: r.get(k)
				for k in (
					"ok",
					"aborted",
					"planner_status",
					"campaign_id",
					"item",
					"warehouse",
					"n_moves",
					"n_pairs",
					"n_identities",
					"sql_updates_executed",
					"elapsed_seconds",
					"after_plan_status",
					"after_eligible",
					"idempotent",
					"error",
					"reason",
				)
			}
			for r in results
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out


def apply_one(item=None, warehouse=None):
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		discover_warehouse_campaigns,
	)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.campaign import (
		apply_warehouse_campaign,
	)

	disc = discover_warehouse_campaigns(company=COMPANY)
	for c in disc.get("ready_campaigns") or []:
		if item and c.get("item") != item:
			continue
		if warehouse and c.get("warehouse") != warehouse:
			continue
		r = apply_warehouse_campaign(c, dry_run=False)
		print(json.dumps({k: r.get(k) for k in (
			"ok", "aborted", "planner_status", "campaign_id", "item", "warehouse",
			"n_moves", "n_pairs", "sql_updates_executed", "elapsed_seconds",
			"after_plan_status", "error", "reason",
		)}, ensure_ascii=False, indent=2, default=str)[:4000])
		return r
	print(json.dumps({"ok": False, "reason": "no matching ready campaign"}))
	return {"ok": False}
