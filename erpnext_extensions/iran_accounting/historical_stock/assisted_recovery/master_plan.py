# Copyright (c) 2026, ERPNext Extensions contributors
"""Master Assisted Recovery Plan — rank residuals by unlock impact."""

from __future__ import annotations

from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company

from collections import defaultdict
from datetime import datetime
from time import perf_counter

from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	BUCKET_ASSISTED,
	BUCKET_AUTO,
	BUCKET_NO_EVIDENCE,
	BUCKET_OPERATOR,
	BUCKET_REAL_SHORTAGE,
	DEFAULT_ASSISTED_THRESHOLD,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.engine import (
	run_assisted_campaign,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.impact_rank import rank_roots


def build_assisted_master_plan(
	company: str | None = None,
	*,
	threshold: float = DEFAULT_ASSISTED_THRESHOLD,
	campaign: dict | None = None,
) -> dict:
	"""Produce AUTO / ASSISTED / OPERATOR / NO_EVIDENCE plan ranked by impact."""
	company = resolve_company(company)
	t0 = perf_counter()
	campaign = campaign or run_assisted_campaign(company=company, threshold=threshold)
	results = list(campaign.get("results") or [])

	buckets = {
		BUCKET_AUTO: [],
		BUCKET_ASSISTED: [],
		BUCKET_OPERATOR: [],
		BUCKET_NO_EVIDENCE: [],
		BUCKET_REAL_SHORTAGE: [],
	}
	for r in results:
		b = r.get("bucket") or BUCKET_OPERATOR
		buckets.setdefault(b, []).append(r)

	# Rank each bucket by impact score against the scan universe (WAITING fan-out)
	universe = campaign.get("universe") or results
	ranked = {}
	for b, rows in buckets.items():
		ranked[b] = rank_roots(rows, universe)

	summary = {
		b: {
			"count": len(rows),
			"total_unlock_estimate": sum(int((r.get("impact") or {}).get("unlocked_estimate") or 0) for r in rows),
			"top": [
				{
					"voucher": r.get("voucher"),
					"item": r.get("item"),
					"topic": r.get("topic"),
					"assisted_status": r.get("assisted_status"),
					"reason": (r.get("reason") or "")[:160],
					"unlock": (r.get("impact") or {}).get("unlocked_estimate"),
					"score": (r.get("impact") or {}).get("score"),
				}
				for r in ranked[b][:15]
			],
		}
		for b, rows in ranked.items()
	}

	# Recommended operator queue: ASSISTED first (high confidence), then high-impact OPERATOR
	operator_queue = ranked.get(BUCKET_ASSISTED, []) + ranked.get(BUCKET_OPERATOR, [])
	operator_queue = sorted(
		operator_queue,
		key=lambda r: (-int((r.get("impact") or {}).get("score") or 0), r.get("voucher") or ""),
	)

	return {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"version": "5.2.19",
		"phase": "ASSISTED_RECOVERY",
		"threshold": threshold,
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"campaign_elapsed": campaign.get("elapsed_seconds"),
		"n_residuals": campaign.get("n_residuals"),
		"n_auto_ready": campaign.get("n_auto_ready"),
		"by_bucket": {b: summary[b]["count"] for b in summary},
		"unlock_potential": {b: summary[b]["total_unlock_estimate"] for b in summary},
		"buckets": summary,
		"auto_ready_sample": [
			{"voucher": r.get("voucher"), "item": r.get("item"), "expected": r.get("expected") or r.get("proposed_rate")}
			for r in (campaign.get("ready_rows") or [])[:20]
		],
		"operator_queue_top": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"topic": r.get("topic"),
				"bucket": r.get("bucket"),
				"assisted_status": r.get("assisted_status"),
				"unlock": (r.get("impact") or {}).get("unlocked_estimate"),
				"score": (r.get("impact") or {}).get("score"),
				"suggested": (r.get("decision_card") or {}).get("suggested_choice"),
				"confidence": (r.get("decision_card") or {}).get("confidence"),
				"what_is_wrong": (r.get("decision_card") or {}).get("what_is_wrong"),
			}
			for r in operator_queue[:25]
		],
		"message": (
			"ASSISTED_READY requires operator confirm before apply. "
			"AUTO rows may be drained by existing repair campaigns. "
			"Stop only when every residual is OPERATOR/NO_EVIDENCE/REAL_STOCK_SHORTAGE."
		),
	}
