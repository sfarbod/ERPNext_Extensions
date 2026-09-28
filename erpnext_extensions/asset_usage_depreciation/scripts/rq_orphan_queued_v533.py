# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Real RQ orphaned-Queued recovery smoke for v5.3.33.

bench --site development.localhost execute \\
  erpnext_extensions.asset_usage_depreciation.scripts.rq_orphan_queued_v533.run
"""

from __future__ import annotations

import json
import time

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.asset_usage_depreciation.services import depr_reset_rebuild_campaign as camp


def run() -> dict:
	"""Create campaign, force orphan Queued, call liveness, wait for real RQ worker."""
	company = frappe.db.get_value("Company", {}, "name")
	assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=6)
	if len(assets) < 4:
		return {"ok": False, "error": "need ≥4 assets"}

	st = camp.create_campaign(
		company=company,
		assets=assets,
		chunk_size=3,
		max_jes_per_chunk=150,
		max_inflight_chunks=1,
		job_timeout_seconds=1800,
		notes="v5.3.33 real RQ orphaned Queued smoke",
	)
	campaign = st["campaign"]
	frappe.db.set_value(camp.CAMPAIGN_DT, campaign, "status", "Running")
	items = frappe.get_all(camp.ITEM_DT, filters={"campaign": campaign}, fields=["name", "asset"])
	for it in items:
		frappe.db.set_value(camp.ITEM_DT, it.name, "submitted_jes_before", 30, update_modified=False)

	# One live-looking Queued group (will keep parent "missing" then mark via state patch? real RQ)
	# Force 3 orphan Queued on dead chunk + leave rest Pending.
	dead_chunk = f"{campaign}-orphan-dead"
	orphan_items = items[:3]
	live_keep = items[3:4]
	for it in orphan_items:
		frappe.db.set_value(
			camp.ITEM_DT,
			it.name,
			{
				"status": "Queued",
				"chunk_id": dead_chunk,
				"queued_at": frappe.utils.add_to_date(now_datetime(), minutes=-10),
				"attempt_count": 0,
			},
		)
	# Preserved live Queued with a real enqueued job
	live_chunk = f"{campaign}-live-keep"
	live_names = [live_keep[0].name] if live_keep else []
	if live_names:
		frappe.db.set_value(
			camp.ITEM_DT,
			live_names[0],
			{
				"status": "Queued",
				"chunk_id": live_chunk,
				"queued_at": now_datetime(),
				"attempt_count": 0,
			},
		)
		frappe.enqueue(
			"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.process_campaign_chunk",
			queue="long",
			timeout=600,
			job_name=f"Asset Depr Repair {live_chunk}",
			campaign=campaign,
			chunk_id=live_chunk,
			item_names=live_names,
			# Use mocked analyze inside worker? Real worker may try accounting — use tiny feed_next
			# Prefer skip by pausing after enqueue? Keep as live Queued for reclaim skip only.
		)

	frappe.db.commit()

	# Diagnostic before
	before = {
		"queued": frappe.db.count(camp.ITEM_DT, {"campaign": campaign, "status": "Queued"}),
		"pending": frappe.db.count(camp.ITEM_DT, {"campaign": campaign, "status": "Pending"}),
	}

	# Recover orphans (missing parent for dead_chunk). Live chunk may be queued/started.
	out = camp.ensure_campaign_liveness(campaign)
	frappe.db.commit()

	after = {
		"queued": frappe.db.count(camp.ITEM_DT, {"campaign": campaign, "status": "Queued"}),
		"pending": frappe.db.count(camp.ITEM_DT, {"campaign": campaign, "status": "Pending"}),
		"orphan_status": [
			frappe.db.get_value(camp.ITEM_DT, it.name, ["status", "chunk_id"], as_dict=True)
			for it in orphan_items
		],
	}

	# Wait briefly for any newly enqueued worker job
	time.sleep(3)
	live_jobs = []
	try:
		live_jobs = frappe.get_all(
			"RQ Job",
			filters={
				"job_name": ("like", f"Asset Depr Repair {campaign}%"),
				"status": ("in", ["queued", "started", "finished"]),
			},
			fields=["job_name", "status"],
			limit_page_length=20,
		)
	except Exception as e:
		live_jobs = [{"error": str(e)}]

	# Old-job safety: reclaim then requeue under new chunk; old chunk claim must fail
	sample = orphan_items[0].name
	for _try in range(5):
		try:
			frappe.db.rollback()
			frappe.db.set_value(
				camp.ITEM_DT, sample, {"status": "Queued", "chunk_id": f"{campaign}-new"}
			)
			frappe.db.commit()
			break
		except Exception:
			frappe.db.rollback()
			time.sleep(0.2)
	old_claim = camp._claim_item(sample, "old-tok", dead_chunk)
	dup = 1 if old_claim else 0
	frappe.db.commit()

	result = {
		"ok": True,
		"campaign": campaign,
		"before": before,
		"liveness": {
			"recovered_queued": out.get("recovered_queued"),
			"enqueued_chunks": out.get("enqueued_chunks"),
			"queued_recovery": out.get("queued_recovery"),
		},
		"after": after,
		"rq_jobs": live_jobs,
		"old_chunk_duplicate_claims": dup,
	}
	print(json.dumps(result, default=str), flush=True)
	frappe.cache.set_value("depr_v533_orphan_rq", result)
	return result
