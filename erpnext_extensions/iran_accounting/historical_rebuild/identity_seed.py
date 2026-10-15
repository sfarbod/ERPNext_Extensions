# Copyright (c) 2026, ERPNext Extensions contributors
"""Identity-level Core RIV seeding for complete historical rebuild.

Creates at most one Item+Warehouse RIV per SLE identity from its earliest
posting boundary, then lets Core workers execute. Campaign documents remain
the audit list; Skipped Covered / process_campaign_item consume the results.
"""

from __future__ import annotations

import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	enqueue_reposting_entry,
	execute_reposting_entry,
)

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_BLOCKED,
	ITEM_COMPLETED,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_RIV_LINKED,
	ITEM_SKIPPED,
	MAX_OUTSTANDING_RIV,
	STATUS_COMPLETED,
	STATUS_PAUSED,
	STATUS_RUNNING,
	STATUS_STOPPED,
)


def _outstanding_count() -> int:
	return cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"status": ("in", ["Queued", "In Progress"]), "docstatus": 1},
		)
	)


def uncovered_identities(company: str, limit: int = 200) -> list[dict[str, Any]]:
	"""Earliest SLE boundary per item+warehouse still lacking a Completed cover.

	Uses a set-oriented query: identities whose earliest SLE is not already
	covered by a Completed Item+WH RIV at an earlier/equal posting datetime,
	and that have no open Queued/In Progress RIV.
	"""
	rows = frappe.db.sql(
		"""
		select s.item_code, s.warehouse,
		       s.posting_date, s.posting_time
		from (
			select item_code, warehouse,
			       min(timestamp(posting_date, posting_time)) as earliest_ts,
			       substring_index(group_concat(posting_date order by posting_date, posting_time), ',', 1) as posting_date,
			       substring_index(group_concat(posting_time order by posting_date, posting_time), ',', 1) as posting_time
			from `tabStock Ledger Entry`
			where is_cancelled=0 and company=%s
			  and item_code is not null and item_code!=''
			  and warehouse is not null and warehouse!=''
			group by item_code, warehouse
		) s
		where not exists (
			select 1 from `tabRepost Item Valuation` r
			where r.docstatus=1
			  and r.based_on='Item and Warehouse'
			  and r.item_code=s.item_code and r.warehouse=s.warehouse
			  and r.status='Completed'
			  and (
			    r.posting_date < s.posting_date
			    or (r.posting_date = s.posting_date and r.posting_time <= s.posting_time)
			  )
		)
		and not exists (
			select 1 from `tabRepost Item Valuation` r2
			where r2.docstatus=1
			  and r2.based_on='Item and Warehouse'
			  and r2.item_code=s.item_code and r2.warehouse=s.warehouse
			  and r2.status in ('Queued','In Progress')
		)
		and not exists (
			-- Block auto-seed when Failed exists AND no Completed cover yet.
			select 1 from `tabRepost Item Valuation` r3
			where r3.docstatus=1
			  and r3.based_on='Item and Warehouse'
			  and r3.item_code=s.item_code and r3.warehouse=s.warehouse
			  and r3.status='Failed'
			  and not exists (
			    select 1 from `tabRepost Item Valuation` r4
			    where r4.docstatus=1
			      and r4.based_on='Item and Warehouse'
			      and r4.item_code=s.item_code and r4.warehouse=s.warehouse
			      and r4.status='Completed'
			  )
		)
		order by s.posting_date asc, s.posting_time asc, s.item_code asc, s.warehouse asc
		limit %s
		""",
		(company, cint(limit)),
		as_dict=True,
	)
	return list(rows or [])


def seed_identities(campaign: str, limit: int | None = None, enqueue: bool = True) -> dict:
	"""Create+enqueue Core Item+WH RIVs for uncovered identities (bounded)."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	doc = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	cap = cint(limit) or MAX_OUTSTANDING_RIV
	outstanding = _outstanding_count()
	room = max(0, MAX_OUTSTANDING_RIV - outstanding)
	if room <= 0:
		return {
			"campaign": campaign,
			"seeded": 0,
			"outstanding": outstanding,
			"detail": "backpressure: no room",
		}
	todo = uncovered_identities(doc.company, limit=min(cap, room))
	created = []
	errors = []
	for r in todo:
		try:
			create_repost_item_valuation_entry(
				frappe._dict(
					based_on="Item and Warehouse",
					item_code=r.item_code,
					warehouse=r.warehouse,
					posting_date=r.posting_date,
					posting_time=r.posting_time,
					company=doc.company,
					allow_zero_rate=False,
				)
			)
			name = frappe.db.get_value(
				"Repost Item Valuation",
				{
					"based_on": "Item and Warehouse",
					"item_code": r.item_code,
					"warehouse": r.warehouse,
					"posting_date": r.posting_date,
					"status": "Queued",
				},
				"name",
				order_by="creation desc",
			)
			if name and enqueue:
				enqueue_reposting_entry(name)
			if name:
				created.append(name)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			errors.append(f"{r.item_code}/{r.warehouse}: {e}"[:200])
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		"last_activity",
		now_datetime(),
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"campaign": campaign,
		"seeded": len(created),
		"names": created,
		"outstanding_before": outstanding,
		"errors": errors[:10],
	}


def settle_outstanding_sync(limit: int | None = None) -> dict:
	"""Core-execute Queued/In Progress RIVs when workers are slow (bounded)."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	cap = cint(limit) or MAX_OUTSTANDING_RIV
	names = frappe.get_all(
		"Repost Item Valuation",
		filters={"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		pluck="name",
		order_by="creation asc",
		limit_page_length=cap,
	)
	results = []
	for name in names:
		before = frappe.db.get_value("Repost Item Valuation", name, "status")
		try:
			execute_reposting_entry(name, continue_reposting=False)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			results.append({"name": name, "before": before, "error": str(e)[:200]})
			continue
		after = frappe.db.get_value("Repost Item Valuation", name, "status")
		results.append({"name": name, "before": before, "after": after})
	return {"attempted": len(results), "results": results}


def _flags_block(campaign: str) -> str | None:
	row = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		["status", "pause_requested", "stop_requested"],
		as_dict=True,
	)
	if not row:
		return "missing"
	if cint(row.stop_requested) or row.status == STATUS_STOPPED:
		return "stopped"
	if cint(row.pause_requested) or row.status == STATUS_PAUSED:
		return "paused"
	return None


def _pending_count(campaign: str) -> int:
	return cint(
		frappe.db.count(
			ITEM_DOCTYPE,
			{"campaign": campaign, "item_status": ("in", [ITEM_PENDING, ITEM_RIV_LINKED])},
		)
	)


def _outstanding_without_active_job(limit: int = 8) -> list[str]:
	"""Queued or In Progress RIVs with no active long-queue job.

	Stale In Progress (worker crash / lost RQ job) blocks MAX_OUTSTANDING_RIV
	backpressure. Recover via Core execute — never force status by hand.
	"""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		get_entries_with_active_jobs,
	)

	names = frappe.get_all(
		"Repost Item Valuation",
		filters={"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		pluck="name",
		order_by="creation asc",
		limit_page_length=max(cint(limit) or 8, 50),
	)
	if not names:
		return []
	try:
		active = get_entries_with_active_jobs()
	except Exception:
		active = set()
	orphans = [n for n in names if n not in active]
	return orphans[: cint(limit) or 8]


def drain_cycle(campaign: str, *, seed_limit: int = 8, cover_limit: int = 2000) -> dict:
	"""One cycle: seed identities → recover orphan Queued → bulk skip covered."""
	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import (
		_refresh_counters,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.fast_cover import (
		bulk_skip_covered,
	)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	# Re-enqueue orphan Queued/In Progress (no RQ job) so workers own execution.
	# Avoid sync-execute here — it competes with long workers and stalls seeding.
	settle = {"attempted": 0, "results": [], "mode": "enqueue"}
	orphans = _outstanding_without_active_job(limit=seed_limit)
	if orphans:
		apply_iran()
		for name in orphans:
			before = frappe.db.get_value("Repost Item Valuation", name, "status")
			try:
				# Core deduplicates by job_id; safe to call repeatedly.
				enqueue_reposting_entry(name)
				frappe.db.commit()
				settle["results"].append({"name": name, "before": before, "action": "enqueued"})
			except Exception as e:
				frappe.db.rollback()
				settle["results"].append({"name": name, "before": before, "error": str(e)[:200]})
		settle["attempted"] = len(settle["results"])

	seed = seed_identities(campaign, limit=seed_limit, enqueue=True)
	# Skip sync recover while workers are healthy (enqueue path above).
	recover = {"attempted": 0, "skipped": "workers_active"}
	# Coverage is read-only vs Core RIV — safe while workers drain outstanding.
	# Cap batch size so cycles stay responsive under load.
	cover = bulk_skip_covered(campaign, limit=min(cover_limit, 300))
	counters = _refresh_counters(campaign)
	return {
		"seed": seed,
		"recover": recover,
		"settle": settle,
		"cover": cover,
		"counters": counters,
		"outstanding": _outstanding_count(),
		"pending_items": _pending_count(campaign),
	}


def run_identity_drain(
	campaign: str,
	*,
	max_cycles: int | None = None,
	seed_limit: int = 8,
	cover_limit: int = 2500,
	idle_sleep: float = 3.0,
) -> dict:
	"""Continuously seed+cover until no uncovered identities and pending is drained via cover,
	or pause/stop, or max_cycles.

	Does not select new campaign documents for per-voucher processing — that remains
	START/RESUME. This phase maximizes Skipped Covered via identity RIV.
	"""
	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import _append_log
	from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
		process_campaign_item,
	)

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"status": STATUS_RUNNING,
			"pause_requested": 0,
			"stop_requested": 0,
			"started_at": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "started_at")
			or now_datetime(),
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	_append_log(campaign, f"Identity drain started seed_limit={seed_limit}")

	cycles = 0
	history = []
	company = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")

	while True:
		block = _flags_block(campaign)
		if block:
			_append_log(campaign, f"Identity drain halted ({block})")
			break

		cycles += 1
		if max_cycles and cycles > cint(max_cycles):
			_append_log(campaign, f"Identity drain max_cycles={max_cycles}")
			break

		cycle = drain_cycle(campaign, seed_limit=seed_limit, cover_limit=cover_limit)
		history.append(
			{
				"cycle": cycles,
				"seeded": cycle["seed"].get("seeded"),
				"skipped": cycle["cover"].get("skipped_covered"),
				"pending": cycle["pending_items"],
				"outstanding": cycle["outstanding"],
			}
		)

		uncovered = len(uncovered_identities(company, limit=1))
		pending = cycle["pending_items"]
		outstanding = cycle["outstanding"]

		# When identities are covered but items remain pending, process them
		# (source rebuild + RIV) in small batches under backpressure.
		if uncovered == 0 and outstanding == 0 and pending > 0:
			items = frappe.get_all(
				ITEM_DOCTYPE,
				filters={
					"campaign": campaign,
					"item_status": ("in", [ITEM_PENDING, ITEM_RIV_LINKED]),
				},
				pluck="name",
				order_by="ordinal asc",
				limit_page_length=min(seed_limit, MAX_OUTSTANDING_RIV),
			)
			if not items:
				break
			for name in items:
				if _flags_block(campaign):
					break
				try:
					process_campaign_item(name, stop_check=lambda: _flags_block(campaign) is not None)
				except Exception as e:
					frappe.db.rollback()
					frappe.db.set_value(
						ITEM_DOCTYPE,
						name,
						{"item_status": ITEM_FAILED, "error_message": str(e)[:1000]},
						update_modified=False,
					)
					frappe.db.commit()
			continue

		if uncovered == 0 and outstanding == 0 and pending == 0:
			frappe.db.set_value(
				CAMPAIGN_DOCTYPE,
				campaign,
				{
					"status": STATUS_COMPLETED,
					"completed_at": now_datetime(),
					"current_voucher_type": None,
					"current_voucher_no": None,
					"last_activity": now_datetime(),
				},
				update_modified=False,
			)
			frappe.db.commit()
			_append_log(campaign, "Identity drain completed — no pending items")
			break

		if cycle["seed"].get("seeded", 0) == 0 and cycle["cover"].get("skipped_covered", 0) == 0:
			if outstanding > 0:
				time.sleep(idle_sleep)
			else:
				# Stuck: try sync settle harder then process pending.
				settle_outstanding_sync(limit=MAX_OUTSTANDING_RIV)
				time.sleep(1)

	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import (
		_refresh_counters,
	)

	block = _flags_block(campaign)
	if block == "paused":
		frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, "status", STATUS_PAUSED, update_modified=False)
	elif block == "stopped":
		frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, "status", STATUS_STOPPED, update_modified=False)
	frappe.db.commit()
	return {
		"campaign": campaign,
		"cycles": cycles,
		"halt": block or "completed",
		"history_tail": history[-20:],
		"counters": _refresh_counters(campaign),
		"status": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"),
		"failed": frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_FAILED}),
		"blocked": frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_BLOCKED}),
		"completed_items": frappe.db.count(
			ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_COMPLETED}
		),
		"skipped": frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_SKIPPED}),
	}
