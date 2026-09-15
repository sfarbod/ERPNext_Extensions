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
			"graph_summary",
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
			"expansion_rounds": (c.get("expansion") or {}).get("rounds"),
			"n_identities": (c.get("multi_identity") or {}).get("n_identities"),
			"expected_sql": c.get("expected_sql"),
			"expected_replay": c.get("expected_replay"),
			"planner_status": c.get("planner_status"),
			"reason": (c.get("reason") or "")[:160],
		}
		for c in (out.get("ready_campaigns") or [])
	]
	summary["others"] = [
		{
			"item": c.get("item"),
			"warehouse": (c.get("warehouse") or "")[:40],
			"n_pairs": c.get("n_pairs"),
			"status": c.get("planner_status"),
			"reason": (c.get("reason") or "")[:120],
		}
		for c in (out.get("campaigns") or [])
		if c.get("planner_status") != "READY_WAREHOUSE_CAMPAIGN"
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

	disc = discover_warehouse_campaigns(company=COMPANY)
	ready = list(disc.get("ready_campaigns") or [])
	# Prefer largest multi-pair Quarantine-style campaigns first
	ready.sort(key=lambda c: (-int(c.get("n_pairs") or 0), -int(c.get("n_moves") or 0)))
	if limit is not None:
		ready = ready[: int(limit)]
	results = []
	for c in ready:
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
					"sql_updates_executed",
					"elapsed_seconds",
					"after_plan_status",
					"after_eligible",
					"error",
					"reason",
					"idempotent_hint",
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
