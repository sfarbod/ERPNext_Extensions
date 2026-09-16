# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.18 Wrong Rate controlled campaign — single → second → SAFE_GROUP ≤10."""

from __future__ import annotations

import json
import os
from datetime import datetime
from time import perf_counter

from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
	SAFE_GROUP,
	cluster_independent_roots,
)
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import apply_wrong_rate_root
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
	classify_wrong_rate_row,
	classify_wrong_rate_universe,
)


from erpnext_extensions.iran_accounting.historical_stock.util import dump_artifact, resolve_company




def inventory(company=None, limit=2000) -> dict:
	out = classify_wrong_rate_universe(company=resolve_company(company), limit=limit)
	dump_artifact("wrong_rate", "inventory.json", {k: v for k, v in out.items() if k != "rows"})
	# Prefer smallest sql among READY with sql>0
	ready = sorted(
		[r for r in (out.get("ready_rows") or []) if int(r.get("sql_updates") or 0) > 0],
		key=lambda r: (int(r.get("sql_updates") or 99), str(r.get("voucher") or "")),
	)
	out["ready_rows"] = ready
	out["ready_wrong_rate"] = len(ready)
	out["ordered_ready"] = [
		{
			"voucher": r.get("voucher"),
			"item": r.get("item"),
			"warehouse": r.get("warehouse"),
			"sql_updates": r.get("sql_updates"),
			"current_value": r.get("current_value"),
			"expected_value": r.get("expected_value"),
			"expected_source": r.get("expected_source"),
			"rate_bucket": r.get("rate_bucket"),
			"rate_status": r.get("rate_status"),
			"surface": r.get("surface"),
			"sle": r.get("sle"),
		}
		for r in ready[:30]
	]
	dump_artifact("wrong_rate", "ordered_ready.json", out["ordered_ready"])
	return out


def prove_single_root(row: dict, *, dry_run=False) -> dict:
	"""Dry-run → apply → residual verify → second apply idempotency."""
	t0 = perf_counter()
	classified = classify_wrong_rate_row(row)
	dry = apply_wrong_rate_root(classified, dry_run=True)
	if not dry.get("ok"):
		return {"ok": False, "stage": "dry_run", "dry": dry}
	if dry_run:
		return {"ok": True, "stage": "dry_run_only", "dry": dry, "elapsed": round(perf_counter() - t0, 3)}
	applied = apply_wrong_rate_root(classified, dry_run=False)
	if not applied.get("ok"):
		return {"ok": False, "stage": "apply", "dry": dry, "applied": applied}
	# Idempotent second run — expect residual already cleared / not READY
	second = apply_wrong_rate_root(classified, dry_run=False)
	# After success, second may abort as not READY or residual_not_cleared with same rate — treat as idempotent if after_rate matches
	idempotent = (not second.get("ok") and "not READY" in str(second.get("reason") or "")) or (
		second.get("ok") and abs(float(second.get("after_rate") or 0) - float(applied.get("expected") or 0)) <= 1.0
	)
	out = {
		"ok": True,
		"stage": "proven",
		"voucher": applied.get("voucher"),
		"item": applied.get("item"),
		"warehouse": applied.get("warehouse"),
		"expected": applied.get("expected"),
		"after_rate": applied.get("after_rate"),
		"replay": applied.get("replay"),
		"gl": applied.get("gl"),
		"idempotent": bool(idempotent),
		"second": {k: second.get(k) for k in ("ok", "reason", "after_rate", "aborted")},
		"elapsed": round(perf_counter() - t0, 3),
	}
	dump_artifact("wrong_rate", f"proof_{applied.get('voucher')}.json", out)
	return out


def prove_safe_group(rows: list[dict], *, max_n=8) -> dict:
	"""Apply independent READY roots (SAFE_GROUP ≤ max_n). Stop on first failure."""
	t0 = perf_counter()
	for r in rows:
		r.setdefault("topic", "WRONG_RATE")
	clusters = cluster_independent_roots(rows[: max_n * 3], max_cluster=max_n)
	group = clusters.get("recommended_first_group") or {}
	if group.get("group_class") != SAFE_GROUP:
		return {"ok": False, "reason": "no SAFE_GROUP", "clusters": clusters}
	order = group.get("roots") or group.get("repair_order") or []
	results = []
	for root in order[:max_n]:
		if isinstance(root, str):
			match = next((r for r in rows if r.get("voucher") == root), None)
			if not match:
				results.append({"ok": False, "stage": "missing_row", "voucher": root})
				break
		else:
			match = next(
				(
					r
					for r in rows
					if r.get("voucher") == root.get("voucher") and r.get("item") == root.get("item")
				),
				root,
			)
		res = prove_single_root(match, dry_run=False)
		results.append(res)
		if not res.get("ok"):
			out = {
				"ok": False,
				"stage": "safe_group_abort",
				"n_attempted": len(results),
				"results": results,
				"elapsed": round(perf_counter() - t0, 3),
			}
			dump_artifact("wrong_rate", "safe_group.json", out)
			return out
	out = {
		"ok": True,
		"stage": "SAFE_GROUP_PROVEN",
		"n_roots": len(results),
		"vouchers": [r.get("voucher") for r in results],
		"promotion_status": "PRODUCTION_PROVEN_SMALL_CLUSTER",
		"results": results,
		"elapsed": round(perf_counter() - t0, 3),
	}
	dump_artifact("wrong_rate", "safe_group.json", out)
	return out


def run_controlled_campaign(company=None, *, max_group=8) -> dict:
	"""Inventory → proof#1 → proof#2 → SAFE_GROUP → checkpoint."""
	inv = inventory(company=company)
	ready = inv.get("ready_rows") or []
	ready = sorted(ready, key=lambda r: (int(r.get("sql_updates") or 99), str(r.get("voucher") or "")))
	if not ready:
		out = {"ok": False, "reason": "no READY_WRONG_RATE", "inventory": {k: inv.get(k) for k in ("count", "by_rate_status", "ready_wrong_rate")}}
		dump_artifact("wrong_rate", "campaign.json", out)
		return out
	proof1 = prove_single_root(ready[0], dry_run=False)
	proof2 = None
	group = None
	if proof1.get("ok"):
		# Distinct second root (different voucher)
		second = next((r for r in ready[1:] if r.get("voucher") != ready[0].get("voucher")), None)
		if second:
			proof2 = prove_single_root(second, dry_run=False)
		if proof2 and proof2.get("ok"):
			# Remaining independent for SAFE_GROUP
			used = {proof1.get("voucher"), proof2.get("voucher")}
			rest = [r for r in ready if r.get("voucher") not in used]
			group = prove_safe_group(rest, max_n=max_group)
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": resolve_company(company),
		"ready_wrong_rate": inv.get("ready_wrong_rate"),
		"by_rate_status": inv.get("by_rate_status"),
		"by_bucket": inv.get("by_bucket"),
		"proof1": proof1,
		"proof2": proof2,
		"safe_group": group,
		"promotion_status": (
			"PRODUCTION_PROVEN_SMALL_CLUSTER"
			if (proof1 or {}).get("ok") and (proof2 or {}).get("ok") and (group or {}).get("ok")
			else "PARTIAL" if (proof1 or {}).get("ok") else "NOT_PROVEN"
		),
	}
	dump_artifact("wrong_rate", "campaign.json", out)
	return out
