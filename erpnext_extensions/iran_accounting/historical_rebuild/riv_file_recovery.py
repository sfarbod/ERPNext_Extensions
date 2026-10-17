# Copyright (c) 2026, ERPNext Extensions contributors
"""In-place recovery for Failed Core RIV caused by shared/empty reposting JSON files.

Production-safe: no campaign recreate, no Completed RIV deletion, no SLE/GL
hand edits. Operates only on Failed Item+Warehouse RIV rows and campaign
Blocked items whose identities become healthy after recovery.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_BLOCKED,
	ITEM_DOCTYPE,
	ITEM_PENDING,
	ITEM_SKIPPED,
	MAX_OUTSTANDING_RIV,
)
from erpnext_extensions.iran_accounting.historical_rebuild.failure_registry import (
	_categorize,
	mark_resolved,
	record_failure,
)


def _is_json_file_failure(error_log: str | None, reposting_data_file: str | None) -> bool:
	cat = _categorize(error_log or "")
	if cat == "missing_repost_file":
		return True
	s = (error_log or "").lower()
	if "jsondecodeerror" in s or "expecting value" in s or "get_reposting_data" in s:
		return True
	# Shared empty-skeleton URL used by concurrent identical progress payloads.
	if reposting_data_file and "repost_item_valuation-" in reposting_data_file:
		# Failed with a file pointer still set is recoverable by clear+re-execute.
		return True
	return False


def list_recoverable_failed_rivs(campaign: str | None = None) -> list[dict[str, Any]]:
	"""Failed Item+WH RIVs eligible for JSON/file recovery (read-only)."""
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, posting_date, posting_time, company,
		       reposting_data_file, left(error_log, 500) as err
		from `tabRepost Item Valuation`
		where docstatus=1 and status='Failed' and based_on='Item and Warehouse'
		order by creation asc
		""",
		as_dict=True,
	)
	out = []
	for r in rows or []:
		if not _is_json_file_failure(r.err, r.reposting_data_file):
			continue
		out.append(
			{
				"name": r.name,
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"posting_date": str(r.posting_date) if r.posting_date else None,
				"posting_time": str(r.posting_time) if r.posting_time else None,
				"company": r.company,
				"reposting_data_file": r.reposting_data_file,
				"category": _categorize(r.err or "") or "missing_repost_file",
			}
		)
	return out


def _detach_riv_files(riv_name: str) -> list[str]:
	"""Delete File rows attached to this RIV only (does not force-status RIV)."""
	names = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Repost Item Valuation",
			"attached_to_name": riv_name,
		},
		pluck="name",
	)
	deleted = []
	for name in names or []:
		try:
			# force=True: shared content_hash URLs must not block detach.
			frappe.delete_doc("File", name, ignore_permissions=True, force=True, delete_permanently=True)
			deleted.append(name)
		except Exception:
			frappe.db.rollback()
			# Clear attachment link without deleting shared physical target.
			frappe.db.set_value(
				"File",
				name,
				{"attached_to_doctype": None, "attached_to_name": None, "attached_to_field": None},
				update_modified=False,
			)
			deleted.append(f"{name}:detached")
	return deleted


def recover_failed_riv(riv_name: str, *, execute: bool = True) -> dict[str, Any]:
	"""Clear broken reposting file pointer and Core-re-execute one Failed RIV.

	Preconditions:
	- RIV exists, docstatus=1, status=Failed, based_on=Item and Warehouse
	- Failure matches JSON/file recovery class
	"""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		enqueue_reposting_entry,
		execute_reposting_entry,
	)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	row = frappe.db.get_value(
		"Repost Item Valuation",
		riv_name,
		[
			"name",
			"docstatus",
			"status",
			"based_on",
			"item_code",
			"warehouse",
			"reposting_data_file",
			"error_log",
		],
		as_dict=True,
	)
	if not row:
		return {"ok": False, "detail": "riv_not_found", "riv": riv_name}
	if cint(row.docstatus) != 1 or row.status != "Failed" or row.based_on != "Item and Warehouse":
		return {
			"ok": False,
			"detail": "precondition_failed",
			"riv": riv_name,
			"status": row.status,
			"based_on": row.based_on,
			"docstatus": row.docstatus,
		}
	if not _is_json_file_failure(row.error_log, row.reposting_data_file):
		return {"ok": False, "detail": "not_json_file_failure", "riv": riv_name}

	detached = _detach_riv_files(riv_name)
	frappe.db.set_value(
		"Repost Item Valuation",
		riv_name,
		{
			"reposting_data_file": None,
			"status": "Queued",
			"error_log": None,
			"current_index": 0,
		},
		update_modified=False,
	)
	frappe.db.commit()

	result: dict[str, Any] = {
		"ok": True,
		"riv": riv_name,
		"item_code": row.item_code,
		"warehouse": row.warehouse,
		"detached_files": detached,
		"before_file": row.reposting_data_file,
	}
	try:
		if execute:
			execute_reposting_entry(riv_name, continue_reposting=False)
		else:
			enqueue_reposting_entry(riv_name)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		result["ok"] = False
		result["error"] = str(e)[:300]
		record_failure(
			item_code=row.item_code,
			warehouse=row.warehouse,
			riv_name=riv_name,
			error=str(e),
		)
		return result

	st = frappe.db.get_value("Repost Item Valuation", riv_name, "status")
	result["status"] = st
	if st == "Completed":
		mark_resolved(row.item_code, row.warehouse, detail=f"riv_file_recovery {riv_name}")
	elif st == "Failed":
		err = frappe.db.get_value("Repost Item Valuation", riv_name, "error_log") or ""
		record_failure(
			item_code=row.item_code,
			warehouse=row.warehouse,
			riv_name=riv_name,
			error=err,
		)
		result["ok"] = False
		result["detail"] = "reexecute_failed"
	return result


def unblock_campaign_items_when_identity_healthy(campaign: str) -> dict[str, Any]:
	"""Return Blocked campaign items to Pending when no Failed RIV remains on SLE identities."""
	blocked = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign, "item_status": ITEM_BLOCKED},
		fields=["name", "voucher_type", "voucher_no", "error_message"],
	)
	released = []
	kept = []
	for it in blocked or []:
		failed = frappe.db.sql(
			"""
			select count(*) from `tabRepost Item Valuation` r
			where r.docstatus=1 and r.status='Failed' and r.based_on='Item and Warehouse'
			  and exists (
			    select 1 from `tabStock Ledger Entry` sle
			    where sle.is_cancelled=0
			      and sle.voucher_type=%s and sle.voucher_no=%s
			      and sle.item_code=r.item_code and sle.warehouse=r.warehouse
			  )
			""",
			(it.voucher_type, it.voucher_no),
		)[0][0]
		if cint(failed):
			kept.append(it.name)
			continue
		frappe.db.set_value(
			ITEM_DOCTYPE,
			it.name,
			{
				"item_status": ITEM_PENDING,
				"error_message": "",
				"last_processed_at": now_datetime(),
			},
			update_modified=False,
		)
		released.append(it.name)
	if released:
		frappe.db.commit()
	return {
		"campaign": campaign,
		"released_to_pending": len(released),
		"still_blocked": len(kept),
		"released": released[:50],
		"kept": kept[:50],
	}


def recover_campaign_json_failures(
	campaign: str,
	*,
	execute: bool = True,
	dry_run: bool = False,
) -> dict[str, Any]:
	"""Full in-place recovery for HRC JSON/file Failed RIVs + blocked vouchers.

	Idempotent: Completed RIVs are never touched; re-run only acts on remaining Failed.
	"""
	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import _append_log, _refresh_counters
	from erpnext_extensions.iran_accounting.historical_rebuild.fast_cover import bulk_skip_covered
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		return {"ok": False, "detail": "campaign_not_found", "campaign": campaign}

	candidates = list_recoverable_failed_rivs(campaign)
	if dry_run:
		return {
			"ok": True,
			"dry_run": True,
			"campaign": campaign,
			"recoverable": len(candidates),
			"candidates": candidates,
		}

	_append_log(campaign, f"riv_file_recovery start candidates={len(candidates)}")
	results = []
	for c in candidates:
		# Bound concurrency: settle one at a time via sync execute.
		results.append(recover_failed_riv(c["name"], execute=execute))

	unblock = unblock_campaign_items_when_identity_healthy(campaign)
	cover = bulk_skip_covered(campaign, limit=5000)
	counters = _refresh_counters(campaign)

	failed_left = cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": "Failed", "based_on": "Item and Warehouse"},
		)
	)
	blocked_left = cint(
		frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_BLOCKED})
	)
	pending_left = cint(
		frappe.db.count(
			ITEM_DOCTYPE,
			{"campaign": campaign, "item_status": ("in", [ITEM_PENDING, "RIV Linked"])},
		)
	)

	# If all items covered and no Failed/Blocked, mark campaign Completed without
	# forcing while work remains.
	terminal_ok = failed_left == 0 and blocked_left == 0 and pending_left == 0
	if terminal_ok:
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{
				"status": "Completed",
				"pause_requested": 0,
				"stop_requested": 0,
				"completed_at": now_datetime(),
				"last_activity": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()
		_append_log(campaign, "riv_file_recovery completed — campaign terminal")
	else:
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"last_activity": now_datetime()},
			update_modified=False,
		)
		frappe.db.commit()
		_append_log(
			campaign,
			f"riv_file_recovery partial failed_left={failed_left} "
			f"blocked={blocked_left} pending={pending_left}",
			level="WARN",
		)

	ok = all(r.get("ok") for r in results) if results else True
	ok = ok and failed_left == 0
	return {
		"ok": ok,
		"campaign": campaign,
		"recovered": results,
		"unblock": unblock,
		"cover": cover,
		"counters": counters,
		"failed_riv_remaining": failed_left,
		"blocked_remaining": blocked_left,
		"pending_remaining": pending_left,
		"terminal_ok": terminal_ok,
		"status": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"),
		"max_outstanding_riv": MAX_OUTSTANDING_RIV,
		"skipped_covered": cint(
			frappe.db.count(ITEM_DOCTYPE, {"campaign": campaign, "item_status": ITEM_SKIPPED})
		),
	}
