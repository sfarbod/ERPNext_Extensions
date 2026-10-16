# Copyright (c) 2026, ERPNext Extensions contributors
"""Persistent Item+Warehouse RIV failure tracking for historical rebuild.

Prevents uncontrolled re-creation of Core RIV for the same failed identity.
Failures remain visible; retry only after an explicit repair attempt.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_BLOCKED,
	ITEM_DOCTYPE,
	MAX_OUTSTANDING_RIV,
)

REGISTRY_KEY_PREFIX = "hrc_riv_fail::"


def identity_key(item_code: str, warehouse: str, posting_date=None) -> str:
	raw = f"{item_code}|{warehouse}|{posting_date or ''}"
	return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _cache_key(item_code: str, warehouse: str) -> str:
	return f"{REGISTRY_KEY_PREFIX}{item_code}::{warehouse}"


def record_failure(
	*,
	item_code: str,
	warehouse: str,
	riv_name: str | None,
	error: str,
	posting_date=None,
	category: str | None = None,
	voucher_hint: str | None = None,
) -> dict[str, Any]:
	"""Upsert failure record in cache + optional campaign log note."""
	sig = (error or "")[:240]
	cat = category or _categorize(sig)
	key = _cache_key(item_code, warehouse)
	prev = frappe.cache().get_value(key) or {}
	riv_ids = list(prev.get("riv_ids") or [])
	if riv_name and riv_name not in riv_ids:
		riv_ids.append(riv_name)
	payload = {
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": str(posting_date) if posting_date else prev.get("posting_date"),
		"failure_signature": sig,
		"category": cat,
		"riv_ids": riv_ids[-20:],
		"retry_count": cint(prev.get("retry_count")) + 1,
		"last_attempt": str(now_datetime()),
		"resolution_state": "open",
		"voucher_hint": voucher_hint or prev.get("voucher_hint"),
		"identity_key": identity_key(item_code, warehouse, posting_date),
	}
	frappe.cache().set_value(key, payload, expires_in_sec=60 * 60 * 24 * 30)
	return payload


def get_failure(item_code: str, warehouse: str) -> dict | None:
	return frappe.cache().get_value(_cache_key(item_code, warehouse))


def is_blocked_identity(item_code: str, warehouse: str) -> bool:
	"""True when identity has an open Failed RIV and must not auto-create new RIV.

	A Failed row blocks auto-seed/create until :func:`repair_failed_identity`
	succeeds and :func:`mark_resolved` moves Failed rows to Skipped.
	Resolved registry state also clears the block.
	"""
	rec = get_failure(item_code, warehouse)
	if rec and rec.get("resolution_state") == "resolved":
		return False
	if frappe.db.exists(
		"Repost Item Valuation",
		{
			"docstatus": 1,
			"based_on": "Item and Warehouse",
			"item_code": item_code,
			"warehouse": warehouse,
			"status": "Failed",
		},
	):
		return True
	return bool(rec and rec.get("resolution_state") == "open")


def mark_resolved(item_code: str, warehouse: str, detail: str = "") -> None:
	key = _cache_key(item_code, warehouse)
	rec = frappe.cache().get_value(key) or {
		"item_code": item_code,
		"warehouse": warehouse,
	}
	rec["resolution_state"] = "resolved"
	rec["resolved_at"] = str(now_datetime())
	rec["resolution_detail"] = (detail or "")[:300]
	frappe.cache().set_value(key, rec, expires_in_sec=60 * 60 * 24 * 30)
	# Preserve failure history on the RIV row (error_log) but move status off
	# Failed so auto-seed / distinct-failure queries stop treating the identity
	# as an open blocker after a successful covering Completed RIV.
	frappe.db.sql(
		"""
		update `tabRepost Item Valuation`
		set status='Skipped'
		where docstatus=1 and status='Failed'
		  and based_on='Item and Warehouse'
		  and item_code=%s and warehouse=%s
		""",
		(item_code, warehouse),
	)
	frappe.db.commit()


def list_open_failures_from_db() -> list[dict[str, Any]]:
	"""Distinct Failed Item+WH identities currently in Core (authoritative)."""
	rows = frappe.db.sql(
		"""
		select item_code, warehouse,
		       count(*) as riv_count,
		       min(posting_date) as posting_date,
		       group_concat(name order by creation separator ',') as riv_names,
		       max(left(error_log, 240)) as error_sample
		from `tabRepost Item Valuation`
		where docstatus=1 and status='Failed'
		  and based_on='Item and Warehouse'
		  and item_code is not null and warehouse is not null
		group by item_code, warehouse
		order by riv_count desc, item_code, warehouse
		""",
		as_dict=True,
	)
	out = []
	for r in rows:
		cat = _categorize(r.error_sample or "")
		payload = record_failure(
			item_code=r.item_code,
			warehouse=r.warehouse,
			riv_name=None,
			error=r.error_sample or "",
			posting_date=r.posting_date,
			category=cat,
		)
		payload["riv_count"] = cint(r.riv_count)
		payload["riv_names"] = (r.riv_names or "").split(",") if r.riv_names else []
		out.append(payload)
	return out


def _categorize(sig: str) -> str:
	s = (sig or "").lower()
	if "component scrap" in s:
		return "component_scrap"
	if "stock valuation integrity" in s or "(i3)" in s:
		return "valuation_integrity_i3"
	if (
		"no such file" in s
		or "repost_item_valuation-" in s
		or "jsondecodeerror" in s
		or "expecting value" in s
		or "get_reposting_data" in s
		or "reposting_data_file" in s
	):
		return "missing_repost_file"
	if "deadlock" in s or "timeout" in s:
		return "recoverable_db"
	return "other"


def block_campaign_items_for_identity(
	campaign: str, item_code: str, warehouse: str, reason: str
) -> int:
	"""Mark Pending campaign items whose SLE touch this identity as Blocked."""
	voucher_pairs = frappe.db.sql(
		"""
		select distinct voucher_type, voucher_no
		from `tabStock Ledger Entry`
		where is_cancelled=0 and item_code=%s and warehouse=%s
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	n = 0
	for vp in voucher_pairs:
		names = frappe.get_all(
			ITEM_DOCTYPE,
			filters={
				"campaign": campaign,
				"voucher_type": vp.voucher_type,
				"voucher_no": vp.voucher_no,
				"item_status": ("in", ["Pending", "RIV Linked"]),
			},
			pluck="name",
		)
		for name in names:
			frappe.db.set_value(
				ITEM_DOCTYPE,
				name,
				{
					"item_status": ITEM_BLOCKED,
					"error_message": reason[:1000],
				},
				update_modified=False,
			)
			n += 1
	if n:
		frappe.db.commit()
	return n


def repair_failed_identity(
	item_code: str,
	warehouse: str,
	*,
	campaign: str | None = None,
	enqueue: bool = True,
) -> dict[str, Any]:
	"""Repair root cause then create ONE new Core RIV (does not force old status)."""
	from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		enqueue_reposting_entry,
		execute_reposting_entry,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.source_rebuild import (
		repair_component_scrap_for_item,
	)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	fail = frappe.db.sql(
		"""
		select name, posting_date, posting_time, company, left(error_log, 500) err
		from `tabRepost Item Valuation`
		where docstatus=1 and status='Failed'
		  and based_on='Item and Warehouse'
		  and item_code=%s and warehouse=%s
		order by creation asc
		limit 1
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	if not fail:
		return {"ok": False, "detail": "no Failed RIV for identity"}
	row = fail[0]
	cat = _categorize(row.err or "")
	# Truncated error_log prefixes often miss JSONDecodeError; empty/missing
	# reposting_data_file is authoritative for this recovery class.
	if cat == "other":
		fpath = frappe.db.get_value("Repost Item Valuation", row.name, "reposting_data_file")
		if not fpath:
			cat = "missing_repost_file"
	actions = []

	if cat == "component_scrap":
		actions.append(repair_component_scrap_for_item(item_code))
	elif cat == "valuation_integrity_i3":
		# Clear sticky Material Issue SLE.outgoing_rate so Core MA owns valuation.
		cleared = frappe.db.sql(
			"""
			update `tabStock Ledger Entry` sle
			inner join `tabStock Entry` se on se.name=sle.voucher_no
			set sle.outgoing_rate=0
			where sle.is_cancelled=0
			  and sle.item_code=%s and sle.warehouse=%s
			  and sle.voucher_type='Stock Entry'
			  and sle.actual_qty < 0
			  and se.purpose in (
			    'Material Issue','Material Transfer',
			    'Material Transfer for Manufacture','Send to Subcontractor'
			  )
			  and abs(sle.outgoing_rate) > 0
			""",
			(item_code, warehouse),
		)
		frappe.db.commit()
		actions.append({"cleared_sticky_outgoing_rates": True, "sql": str(cleared)})
	elif cat == "missing_repost_file":
		# Drop broken file pointer so Core regenerates on next execute.
		# Re-queue the same RIV (Core-supported recovery) — do not leave it Failed
		# under backpressure with a cleared file and no replacement.
		frappe.db.set_value(
			"Repost Item Valuation",
			row.name,
			{
				"reposting_data_file": None,
				"status": "Queued",
				"error_log": None,
			},
			update_modified=False,
		)
		frappe.db.commit()
		actions.append({"cleared_and_requeued": row.name})
		try:
			if enqueue:
				enqueue_reposting_entry(row.name)
			else:
				execute_reposting_entry(row.name, continue_reposting=False)
			frappe.db.commit()
			st = frappe.db.get_value("Repost Item Valuation", row.name, "status")
			if st == "Completed":
				mark_resolved(item_code, warehouse, detail=f"re-executed {row.name}")
				return {"ok": True, "status": st, "riv": row.name, "actions": actions}
			if st in ("Queued", "In Progress"):
				# Old Failed cleared by requeue; identity no longer blocks seeding.
				return {"ok": True, "status": st, "riv": row.name, "actions": actions}
		except Exception as e:
			actions.append({"reexecute_error": str(e)[:200]})

	# Always create a fresh RIV from earliest SLE boundary after repair attempts.
	if _outstanding_count() >= MAX_OUTSTANDING_RIV:
		return {"ok": False, "detail": "backpressure", "actions": actions}

	boundary = frappe.db.sql(
		"""
		select min(posting_date) as posting_date,
		       substring_index(group_concat(posting_time order by posting_date, posting_time), ',', 1) as posting_time
		from `tabStock Ledger Entry`
		where is_cancelled=0 and item_code=%s and warehouse=%s
		""",
		(item_code, warehouse),
		as_dict=True,
	)[0]
	company = row.company or frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company") if campaign else None
	try:
		create_repost_item_valuation_entry(
			frappe._dict(
				based_on="Item and Warehouse",
				item_code=item_code,
				warehouse=warehouse,
				posting_date=boundary.posting_date or row.posting_date,
				posting_time=boundary.posting_time or row.posting_time,
				company=company,
				allow_zero_rate=False,
			)
		)
		new_name = frappe.db.get_value(
			"Repost Item Valuation",
			{
				"based_on": "Item and Warehouse",
				"item_code": item_code,
				"warehouse": warehouse,
				"status": "Queued",
			},
			"name",
			order_by="creation desc",
		)
		if new_name and enqueue:
			enqueue_reposting_entry(new_name)
		elif new_name:
			execute_reposting_entry(new_name, continue_reposting=False)
		frappe.db.commit()
		st = frappe.db.get_value("Repost Item Valuation", new_name, "status") if new_name else None
		if st in ("Completed", "Queued", "In Progress"):
			# Clear old Failed blockers immediately; replacement RIV owns recovery.
			mark_resolved(item_code, warehouse, detail=f"new riv {new_name} status={st}")
		elif st == "Failed":
			record_failure(
				item_code=item_code,
				warehouse=warehouse,
				riv_name=new_name,
				error=frappe.db.get_value("Repost Item Valuation", new_name, "error_log") or "",
				posting_date=boundary.posting_date,
			)
		return {"ok": st in ("Completed", "Queued", "In Progress"), "riv": new_name, "status": st, "actions": actions}
	except Exception as e:
		frappe.db.rollback()
		return {"ok": False, "error": str(e)[:300], "actions": actions}


def _outstanding_count() -> int:
	return cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"status": ("in", ["Queued", "In Progress"]), "docstatus": 1},
		)
	)


def resolve_covered_failures() -> dict:
	"""Skip Failed RIVs whose identity now has a Completed cover at earliest SLE."""
	rows = frappe.db.sql(
		"""
		select distinct f.item_code, f.warehouse
		from `tabRepost Item Valuation` f
		where f.docstatus=1 and f.status='Failed'
		  and f.based_on='Item and Warehouse'
		  and exists (
		    select 1 from `tabRepost Item Valuation` c
		    where c.docstatus=1 and c.status='Completed'
		      and c.based_on='Item and Warehouse'
		      and c.item_code=f.item_code and c.warehouse=f.warehouse
		      and c.posting_date <= (
		        select min(s.posting_date) from `tabStock Ledger Entry` s
		        where s.is_cancelled=0 and s.item_code=f.item_code and s.warehouse=f.warehouse
		      )
		  )
		""",
		as_dict=True,
	)
	resolved = []
	for r in rows or []:
		mark_resolved(r.item_code, r.warehouse, detail="Completed cover at earliest SLE")
		resolved.append({"item_code": r.item_code, "warehouse": r.warehouse})
	return {"resolved": len(resolved), "identities": resolved}


def repair_all_failed_identities(campaign: str = "HRC-00001") -> dict:
	"""Attempt repair for every distinct Failed identity."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	# First close any that already have a covering Completed RIV.
	covered = resolve_covered_failures()
	ids = list_open_failures_from_db()
	results = []
	for rec in ids:
		results.append(
			{
				"item_code": rec["item_code"],
				"warehouse": rec["warehouse"],
				"category": rec.get("category"),
				"result": repair_failed_identity(
					rec["item_code"], rec["warehouse"], campaign=campaign, enqueue=True
				),
			}
		)
	return {"identities": len(ids), "covered_first": covered, "results": results}
