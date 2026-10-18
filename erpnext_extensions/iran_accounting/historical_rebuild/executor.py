# Copyright (c) 2026, ERPNext Extensions contributors
"""Dependency-aware, resumable Historical Rebuild Campaign executor (v5.6.1 Phase 3).

Properties: deterministic ordering, idempotent RIV linking, checkpointing via
campaign item status, failure isolation, controlled concurrency (no parallel
conflicting identities), safe restart.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	DEFAULT_INTERNAL_BATCH,
	ITEM_BLOCKED,
	ITEM_COMPLETED,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_RIV_LINKED,
	ITEM_SKIPPED,
	MAX_OUTSTANDING_RIV,
	STATUS_PAUSED,
	STATUS_RUNNING,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	apply_verified_skip,
	verify_voucher_coverage,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	process_campaign_item,
)


def _campaign_flags(campaign: str) -> dict:
	return frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		["status", "pause_requested", "stop_requested"],
		as_dict=True,
	) or {}


def _pending_batch(campaign: str, limit: int) -> list[dict]:
	"""Deterministic ordinal order — dependency-aware via chronological manifest."""
	return (
		frappe.get_all(
			ITEM_DOCTYPE,
			filters={"campaign": campaign, "item_status": ITEM_PENDING},
			fields=["name", "voucher_type", "voucher_no", "ordinal", "posting_date", "posting_time"],
			order_by="posting_date asc, posting_time asc, ordinal asc",
			limit_page_length=cint(limit) or DEFAULT_INTERNAL_BATCH,
		)
		or []
	)


def _identity_keys_for_voucher(voucher_type: str, voucher_no: str) -> set[tuple[str, str]]:
	rows = frappe.db.sql(
		"""
		select distinct item_code, warehouse
		from `tabStock Ledger Entry`
		where is_cancelled=0 and voucher_type=%s and voucher_no=%s
		  and item_code is not null and warehouse is not null
		""",
		(voucher_type, voucher_no),
	)
	return {(r[0], r[1]) for r in (rows or [])}


def _filter_nonconflicting(batch: list[dict]) -> list[dict]:
	"""Within one batch, skip vouchers that share Item/WH with an earlier pick."""
	selected: list[dict] = []
	locked: set[tuple[str, str]] = set()
	for row in batch:
		keys = _identity_keys_for_voucher(row.voucher_type, row.voucher_no)
		if keys & locked:
			continue
		selected.append(row)
		locked |= keys
	return selected


def _outstanding_riv_count() -> int:
	return cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		)
	)


def run_pilot(
	campaign: str = "HRC-00001",
	*,
	limit: int = 5,
	verify_only_first: bool = False,
) -> dict[str, Any]:
	"""Small controlled pilot before full Phase 3 drain."""
	return run_batch(campaign, limit=limit, max_items=limit, verify_only_first=verify_only_first)


def run_batch(
	campaign: str,
	*,
	limit: int = DEFAULT_INTERNAL_BATCH,
	max_items: int | None = None,
	verify_only_first: bool = False,
	stop_on_failure: bool = False,
) -> dict[str, Any]:
	"""Process up to max_items Pending entries with conflict isolation."""
	t0 = time.time()
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(f"Campaign {campaign} not found")

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"status": STATUS_RUNNING, "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()

	processed = 0
	completed = 0
	failed = 0
	skipped = 0
	blocked = 0
	results: list[dict] = []
	cap = cint(max_items) if max_items is not None else None

	while True:
		flags = _campaign_flags(campaign)
		if cint(flags.get("pause_requested")) or cint(flags.get("stop_requested")):
			break
		if flags.get("status") == STATUS_PAUSED:
			break
		if _outstanding_riv_count() >= MAX_OUTSTANDING_RIV:
			results.append({"detail": "backpressure", "outstanding": _outstanding_riv_count()})
			break

		batch = _pending_batch(campaign, limit)
		if not batch:
			break
		batch = _filter_nonconflicting(batch)
		if not batch:
			# All pending conflict — take one to make progress
			batch = _pending_batch(campaign, 1)

		for row in batch:
			if cap is not None and processed >= cap:
				break
			# Idempotent: if already verified clean somehow, skip
			if verify_only_first or True:
				pre = verify_voucher_coverage(row.voucher_type, row.voucher_no)
				if pre.get("ok"):
					apply_verified_skip(row.name, pre)
					skipped += 1
					processed += 1
					results.append(
						{
							"item": row.name,
							"voucher": f"{row.voucher_type}/{row.voucher_no}",
							"final_status": ITEM_SKIPPED,
							"detail": "pre_verified",
						}
					)
					continue

			out = process_campaign_item(row.name)
			processed += 1
			st = out.get("final_status")
			if st == ITEM_COMPLETED:
				# Post-process accounting verification — Completed RIV ≠ verified
				post = verify_voucher_coverage(row.voucher_type, row.voucher_no)
				if post.get("ok"):
					apply_verified_skip(row.name, post)
					skipped += 1
					out["final_status"] = ITEM_SKIPPED
					out["verified"] = True
				else:
					# Leave Completed but mark detail — may need another RIV pass
					frappe.db.set_value(
						ITEM_DOCTYPE,
						row.name,
						{
							"error_message": f"pending_verification:{post.get('reason')}"[:1000],
						},
						update_modified=False,
					)
					completed += 1
					out["verified"] = False
			elif st == ITEM_SKIPPED:
				skipped += 1
			elif st == ITEM_FAILED:
				failed += 1
				if stop_on_failure:
					results.append(out)
					frappe.db.commit()
					break
			elif st == ITEM_BLOCKED:
				blocked += 1
			results.append(out)
			frappe.db.commit()

		if cap is not None and processed >= cap:
			break
		if not batch:
			break

	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import (
		_refresh_counters,
	)

	counters = _refresh_counters(campaign)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"status": STATUS_PAUSED, "last_activity": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()

	return {
		"campaign": campaign,
		"runtime_seconds": round(time.time() - t0, 3),
		"processed": processed,
		"completed": completed,
		"skipped_verified": skipped,
		"failed": failed,
		"blocked": blocked,
		"campaign_counters": counters,
		"results_sample": results[:50],
		"result_count": len(results),
	}


def run_until_idle(
	campaign: str = "HRC-00001",
	*,
	batch_size: int = 10,
	max_batches: int = 2000,
	pilot_first: bool = True,
) -> dict[str, Any]:
	"""Drain Pending with increasing batches after optional pilot."""
	summary: dict[str, Any] = {"batches": [], "pilot": None}
	if pilot_first:
		summary["pilot"] = run_pilot(campaign, limit=min(5, batch_size))
		summary["batches"].append(summary["pilot"])

	for i in range(cint(max_batches) or 1):
		pending = cint(
			frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_PENDING})
		)
		if pending == 0:
			summary["stopped_reason"] = "no_pending"
			break
		flags = _campaign_flags(campaign)
		if cint(flags.get("stop_requested")):
			summary["stopped_reason"] = "stop_requested"
			break
		# Grow cautiously
		lim = min(batch_size * (1 + i // 10), 50)
		batch_out = run_batch(campaign, limit=lim, max_items=lim)
		summary["batches"].append(
			{
				"i": i,
				"processed": batch_out.get("processed"),
				"failed": batch_out.get("failed"),
				"pending_after": cint(
					frappe.db.count(
						ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_PENDING}
					)
				),
			}
		)
		if batch_out.get("processed", 0) == 0:
			summary["stopped_reason"] = "no_progress"
			break
	summary["final_counters"] = frappe.db.sql(
		"""
		select item_status, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s
		group by item_status
		""",
		campaign,
		as_dict=True,
	)
	return summary
