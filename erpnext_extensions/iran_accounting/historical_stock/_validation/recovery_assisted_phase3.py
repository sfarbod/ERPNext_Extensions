# Copyright (c) 2026 — Phase 3 Assisted Recovery campaign + master plan probe
from __future__ import annotations

import json


def run(*, threshold=0.95, apply_auto=0, max_apply=10):
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.engine import (
		run_assisted_campaign,
	)
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.master_plan import (
		build_assisted_master_plan,
	)

	campaign = run_assisted_campaign(threshold=float(threshold))
	plan = build_assisted_master_plan(campaign=campaign, threshold=float(threshold))

	applied = []
	if int(apply_auto) and campaign.get("ready_rows"):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
			apply_wrong_rate_root,
		)
		import frappe

		for row in campaign["ready_rows"][: int(max_apply)]:
			# Prefer Wrong Rate apply when topic WRONG_RATE / ZERO_RATE
			res = apply_wrong_rate_root(row, dry_run=False)
			ok = bool(res.get("ok"))
			if ok:
				frappe.db.commit()
			applied.append(
				{
					"voucher": row.get("voucher"),
					"item": row.get("item"),
					"ok": ok,
					"after_rate": res.get("after_rate"),
					"expected": res.get("expected"),
					"reason": res.get("reason") or res.get("error"),
				}
			)

	out = {
		"by_bucket": plan.get("by_bucket"),
		"unlock_potential": plan.get("unlock_potential"),
		"n_auto_ready": plan.get("n_auto_ready"),
		"n_residuals": plan.get("n_residuals"),
		"operator_queue_top": plan.get("operator_queue_top"),
		"auto_ready_sample": plan.get("auto_ready_sample"),
		"assisted_cards_n": len(campaign.get("assisted_cards") or []),
		"assisted_sample": (campaign.get("assisted_cards") or [])[:8],
		"applied": applied,
		"elapsed": plan.get("elapsed_seconds"),
		"campaign_elapsed": campaign.get("elapsed_seconds"),
		"message": plan.get("message"),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:16000])
	return out
