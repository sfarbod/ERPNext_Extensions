# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Real RQ load test for Asset Depreciation Repair Campaign v5.3.32.

bench --site development.localhost execute \\
  erpnext_extensions.asset_usage_depreciation.scripts.rq_load_test_v532.create_and_start \\
  --kwargs '{"label":"1w","max_inflight":1}'

bench --site development.localhost execute \\
  erpnext_extensions.asset_usage_depreciation.scripts.rq_load_test_v532.monitor \\
  --kwargs '{"campaign":"AUD-ADRC-..."}'
"""

from __future__ import annotations

import json
import time

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.asset_usage_depreciation.services import depr_reset_rebuild_campaign as camp


# Avoid assets already repaired by baseline_timing / prior runs.
LIGHT = ["2398", "2244", "2268", "3669", "2315"]  # ~45 JEs
MEDIUM = ["1608", "2668", "1642", "1643", "1644"]  # ~85 JEs
HEAVY = ["3626", "3655", "3659", "3622", "3631"]  # ~120 JEs (Dev max)
# Highest-JE eligible already included in HEAVY (120 is max on Dev)


def _company_for(asset: str) -> str:
	return frappe.db.get_value("Asset", asset, "company")


def create_and_start(label: str = "1w", max_inflight: int = 1, assets: str | list | None = None) -> dict:
	if assets:
		if isinstance(assets, str):
			assets = json.loads(assets) if assets.strip().startswith("[") else [a.strip() for a in assets.split(",") if a.strip()]
	else:
		assets = LIGHT + MEDIUM + HEAVY
	# Drop assets already Success/Already Repaired in prior campaigns if still eligible
	company = _company_for(assets[0])
	st = camp.create_campaign(
		company=company,
		assets=assets,
		chunk_size=3,
		max_jes_per_chunk=150,
		max_inflight_chunks=cint(max_inflight),
		job_timeout_seconds=1800,
		notes=f"v5.3.32 RQ load test label={label} max_inflight={max_inflight}",
	)
	campaign = st["campaign"]
	# Seed submitted_jes_before for weighted packing without waiting for analyze
	for item in frappe.get_all(
		camp.ITEM_DT, filters={"campaign": campaign}, fields=["name", "asset"]
	):
		jes = frappe.db.sql(
			"""
			select count(distinct je.name)
			from `tabJournal Entry` je
			inner join `tabJournal Entry Account` jea on jea.parent = je.name
			inner join `tabAccount` acc on acc.name = jea.account
			where je.voucher_type = 'Depreciation Entry'
			  and je.docstatus = 1
			  and jea.reference_type = 'Asset'
			  and jea.reference_name = %s
			  and jea.debit > 0
			  and acc.root_type = 'Expense'
			""",
			(item.asset,),
		)[0][0]
		frappe.db.set_value(
			camp.ITEM_DT, item.name, "submitted_jes_before", cint(jes), update_modified=False
		)
	frappe.db.commit()
	out = camp.start_campaign(campaign, run_inline=0)
	result = {
		"label": label,
		"campaign": campaign,
		"assets": assets,
		"start": out,
		"started_at": str(now_datetime()),
	}
	frappe.cache.set_value(f"depr_rq_load_{label}", result)
	print(json.dumps(result, default=str), flush=True)
	return result


def monitor(campaign: str, wait_s: int = 0, poll_s: int = 15) -> dict:
	t0 = time.time()
	deadline = t0 + cint(wait_s) if cint(wait_s) else None
	while True:
		for _attempt in range(5):
			try:
				camp.refresh_campaign_counts(campaign)
				break
			except Exception as e:
				frappe.db.rollback()
				if "1020" in str(e) or "Deadlock" in type(e).__name__:
					time.sleep(0.3 * (_attempt + 1))
					continue
				raise
		doc = frappe.get_doc(camp.CAMPAIGN_DT, campaign)
		items = frappe.get_all(
			camp.ITEM_DT,
			filters={"campaign": campaign},
			fields=[
				"asset",
				"status",
				"submitted_jes_before",
				"jes_cancelled",
				"attempt_count",
				"started_at",
				"completed_at",
				"error_type",
				"error_message",
				"g0_g12_pass",
				"chunk_id",
			],
		)
		durs = []
		for i in items:
			if i.started_at and i.completed_at and i.status in ("Success", "Already Repaired"):
				durs.append((i.completed_at - i.started_at).total_seconds())
		durs.sort()

		def pct(p):
			if not durs:
				return None
			idx = min(len(durs) - 1, max(0, int(round((p / 100.0) * (len(durs) - 1)))))
			return round(durs[idx], 2)

		status_counts = {}
		for i in items:
			status_counts[i.status] = status_counts.get(i.status, 0) + 1
		live_jobs = 0
		try:
			live_jobs = len(
				frappe.get_all(
					"RQ Job",
					filters={
						"job_name": ("like", f"Asset Depr Repair {campaign}%"),
						"status": ("in", ["queued", "started"]),
					},
					limit_page_length=50,
				)
			)
		except Exception:
			pass
		elapsed = round(time.time() - t0, 1)
		success = status_counts.get("Success", 0) + status_counts.get("Already Repaired", 0)
		jes_cancelled = sum(cint(i.jes_cancelled) for i in items)
		summary = {
			"campaign": campaign,
			"status": doc.status,
			"elapsed_s": elapsed,
			"counts": status_counts,
			"success_like": success,
			"jes_cancelled": jes_cancelled,
			"assets_per_min": round(success / (elapsed / 60.0), 2) if elapsed > 5 else None,
			"jes_per_min": round(jes_cancelled / (elapsed / 60.0), 2) if elapsed > 5 else None,
			"asset_runtime": {
				"n": len(durs),
				"avg": round(sum(durs) / len(durs), 2) if durs else None,
				"median": pct(50),
				"p95": pct(95),
				"max": round(durs[-1], 2) if durs else None,
			},
			"live_rq_jobs": live_jobs,
			"chunk_size": doc.chunk_size,
			"max_jes_per_chunk": getattr(doc, "max_jes_per_chunk", None),
			"max_inflight": doc.max_inflight_chunks,
		}
		print(json.dumps(summary, default=str), flush=True)
		terminal = doc.status in (
			"Completed",
			"Completed With Exceptions",
			"Failed",
			"Stopped",
			"Paused",
		)
		pending_work = any(
			status_counts.get(s, 0)
			for s in ("Pending", "Queued", "Processing", "Retryable Failed")
		)
		if terminal or (not pending_work and live_jobs == 0 and success > 0):
			frappe.cache.set_value(f"depr_rq_load_result_{campaign}", summary)
			return summary
		if deadline and time.time() >= deadline:
			frappe.cache.set_value(f"depr_rq_load_result_{campaign}", summary)
			return summary
		time.sleep(max(5, cint(poll_s)))


def explain_indexes() -> dict:
	plans = {}
	for label, sql in [
		(
			"depr_sched_je",
			"EXPLAIN SELECT name FROM `tabDepreciation Schedule` WHERE journal_entry=%s LIMIT 1",
		),
		(
			"stock_recog",
			"EXPLAIN SELECT name FROM `tabStock Entry` WHERE custom_consignment_recognition_je=%s LIMIT 1",
		),
		(
			"stock_settle",
			"EXPLAIN SELECT name FROM `tabStock Entry` WHERE custom_consignment_settlement_je=%s LIMIT 1",
		),
	]:
		sample = "ACC-JV-PLACEHOLDER"
		plans[label] = frappe.db.sql(sql, (sample,), as_dict=True)
	print(json.dumps(plans, default=str), flush=True)
	return plans
