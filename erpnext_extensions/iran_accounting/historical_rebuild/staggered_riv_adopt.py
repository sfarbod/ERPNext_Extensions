# Copyright (c) 2026, ERPNext Extensions contributors
"""Identity-level RIV adoption for Sequential → Staggered migration (v5.7.51).

Prevents duplicate Stage A RIV creation by adopting existing covering /
in-flight RIVs with exact identity and voucher-scope evidence.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, get_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.staggered_riv_locks import (
	fetch_future_voucher_keys,
	identity_key,
)

DISPOSITION_LEDGER_REUSE = "LEDGER_REUSE"
DISPOSITION_ADOPT_COMPLETED = "ADOPT_COMPLETED"
DISPOSITION_ADOPT_QUEUED = "ADOPT_QUEUED"
DISPOSITION_ADOPT_IN_PROGRESS = "ADOPT_IN_PROGRESS"
DISPOSITION_FAILED_NEEDS_RECOVERY = "FAILED_NEEDS_RECOVERY"
DISPOSITION_NEEDS_REVIEW = "NEEDS_REVIEW"
DISPOSITION_CREATE = "CREATE"

ACTIVE = frozenset({"Queued", "In Progress"})
TERMINAL_OK = frozenset({"Completed"})
TERMINAL_FAIL = frozenset({"Failed"})
TERMINAL_SKIP = frozenset({"Skipped", "Cancelled"})


def _norm_time(t) -> str:
	s = str(t or "00:00:00").strip()
	if not s:
		return "00:00:00"
	# Normalize "HH:MM:SS.ffffff" → comparable string via datetime when possible
	try:
		dt = get_datetime(f"2000-01-01 {s}")
		return dt.strftime("%H:%M:%S.") + f"{dt.microsecond:06d}"
	except Exception:
		return s


def _norm_date(d) -> str:
	return str(d or "")[:10]


def posting_tuple(d, t) -> tuple[str, str]:
	return (_norm_date(d), _norm_time(t))


def posting_exact(a_date, a_time, b_date, b_time) -> bool:
	return posting_tuple(a_date, a_time) == posting_tuple(b_date, b_time)


def posting_covers(riv_date, riv_time, id_date, id_time) -> bool:
	"""RIV boundary covers identity when riv posting datetime <= identity posting."""
	rd, rt = posting_tuple(riv_date, riv_time)
	id_, it = posting_tuple(id_date, id_time)
	if not rd or not id_:
		return False
	if rd < id_:
		return True
	if rd > id_:
		return False
	return rt <= it


def list_identity_rivs(item_code: str, warehouse: str) -> list[dict[str, Any]]:
	rows = frappe.get_all(
		"Repost Item Valuation",
		filters={
			"docstatus": 1,
			"based_on": "Item and Warehouse",
			"item_code": item_code,
			"warehouse": warehouse,
		},
		fields=[
			"name",
			"status",
			"posting_date",
			"posting_time",
			"company",
			"based_on",
			"creation",
			"modified",
			"item_code",
			"warehouse",
		],
		order_by="creation desc",
		limit_page_length=50,
	)
	return list(rows or [])


def campaign_item_covering_rivs(
	campaign: str, item_code: str, warehouse: str
) -> list[str]:
	"""covering_riv values on campaign items whose SLE hits this identity."""
	rows = frappe.db.sql(
		"""
		select distinct i.covering_riv
		from `tabHistorical Rebuild Campaign Item` i
		inner join `tabStock Ledger Entry` sle
		  on sle.voucher_type=i.voucher_type and sle.voucher_no=i.voucher_no
		 and sle.is_cancelled=0
		where i.campaign=%s
		  and sle.item_code=%s and sle.warehouse=%s
		  and i.covering_riv is not null and i.covering_riv!=''
		""",
		(campaign, item_code, warehouse),
	)
	return [r[0] for r in (rows or []) if r and r[0]]


def campaign_identity_voucher_keys(
	campaign: str, item_code: str, warehouse: str, *, limit: int = 500
) -> set[str]:
	lim = max(50, cint(limit) or 500)
	rows = frappe.db.sql(
		"""
		select distinct i.voucher_type, i.voucher_no
		from `tabHistorical Rebuild Campaign Item` i
		inner join `tabStock Ledger Entry` sle
		  on sle.voucher_type=i.voucher_type and sle.voucher_no=i.voucher_no
		 and sle.is_cancelled=0
		where i.campaign=%s
		  and sle.item_code=%s and sle.warehouse=%s
		limit %s
		""",
		(campaign, item_code, warehouse, lim),
	)
	return {f"{vt}/{vn}" for vt, vn in (rows or []) if vt and vn}


def voucher_scope_evidence(
	*,
	campaign: str,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	riv_name: str,
	riv_posting_date,
	riv_posting_time,
	riv_status: str,
) -> dict[str, Any]:
	"""Prove whether an existing RIV covers this campaign identity's voucher scope."""
	evidence: dict[str, Any] = {
		"riv_name": riv_name,
		"exact_posting": posting_exact(
			riv_posting_date, riv_posting_time, posting_date, posting_time
		),
		"covers_posting": posting_covers(
			riv_posting_date, riv_posting_time, posting_date, posting_time
		),
		"campaign_covering_match": False,
		"voucher_subset_ok": False,
		"campaign_vouchers": 0,
		"overlap": 0,
		"proven": False,
		"reason": None,
	}
	if not evidence["covers_posting"]:
		evidence["reason"] = "posting_boundary_does_not_cover"
		return evidence

	cov = campaign_item_covering_rivs(campaign, item_code, warehouse)
	if riv_name in cov:
		evidence["campaign_covering_match"] = True

	camp_v = campaign_identity_voucher_keys(campaign, item_code, warehouse)
	evidence["campaign_vouchers"] = len(camp_v)

	# Exact posting match for open RIVs is strong enough without SLE scan.
	if evidence["exact_posting"] and riv_status in ACTIVE:
		evidence["proven"] = True
		evidence["reason"] = "exact_posting_active"
		return evidence

	if evidence["exact_posting"] and riv_status in TERMINAL_OK:
		evidence["proven"] = True
		evidence["reason"] = "exact_posting_completed"
		return evidence

	if evidence["campaign_covering_match"] and riv_status in TERMINAL_OK | ACTIVE:
		evidence["proven"] = True
		evidence["reason"] = "campaign_covering_riv"
		return evidence

	# SLE voucher subset / overlap from RIV boundary forward.
	try:
		riv_v = fetch_future_voucher_keys(
			item_code, warehouse, riv_posting_date, riv_posting_time, limit=500
		)
	except Exception:
		riv_v = set()
	overlap = camp_v & riv_v if camp_v and riv_v else set()
	evidence["overlap"] = len(overlap)
	if camp_v and riv_v and camp_v.issubset(riv_v):
		evidence["voucher_subset_ok"] = True
		evidence["proven"] = True
		evidence["reason"] = "campaign_vouchers_subset_of_riv_future"
		return evidence
	if camp_v and overlap and len(overlap) == len(camp_v):
		evidence["voucher_subset_ok"] = True
		evidence["proven"] = True
		evidence["reason"] = "full_overlap"
		return evidence

	# Completed covering with empty campaign voucher set (identity not in items?) → review
	if not camp_v:
		evidence["reason"] = "no_campaign_vouchers_for_identity"
		evidence["proven"] = False
		return evidence

	if riv_status in TERMINAL_OK and evidence["covers_posting"] and evidence["overlap"] > 0:
		# Partial overlap without subset — ambiguous
		evidence["reason"] = "partial_voucher_overlap"
		evidence["proven"] = False
		return evidence

	evidence["reason"] = "insufficient_voucher_evidence"
	evidence["proven"] = False
	return evidence


def resolve_identity_riv_disposition(
	*,
	campaign: str,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	company: str | None = None,
) -> dict[str, Any]:
	"""Decide adopt / create / review for one identity."""
	ikey = identity_key(item_code, warehouse)
	base = {
		"identity_key": ikey,
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": str(posting_date),
		"posting_time": str(posting_time or "00:00:00"),
		"riv_name": None,
		"disposition": DISPOSITION_CREATE,
		"evidence": {},
	}

	rivs = list_identity_rivs(item_code, warehouse)
	if company:
		rivs = [r for r in rivs if not r.get("company") or r.get("company") == company]

	active = [r for r in rivs if r.get("status") in ACTIVE]
	failed = [r for r in rivs if r.get("status") in TERMINAL_FAIL]
	completed = [r for r in rivs if r.get("status") in TERMINAL_OK]
	skipped = [r for r in rivs if r.get("status") in TERMINAL_SKIP]

	# Multiple active RIVs for same identity → never auto-create duplicate
	if len(active) > 1:
		base["disposition"] = DISPOSITION_NEEDS_REVIEW
		base["evidence"] = {
			"reason": "multiple_active_rivs",
			"rivs": [r.name for r in active],
		}
		return base

	if len(active) == 1:
		r = active[0]
		ev = voucher_scope_evidence(
			campaign=campaign,
			item_code=item_code,
			warehouse=warehouse,
			posting_date=posting_date,
			posting_time=posting_time,
			riv_name=r.name,
			riv_posting_date=r.posting_date,
			riv_posting_time=r.posting_time,
			riv_status=r.status,
		)
		if not ev.get("proven"):
			base["disposition"] = DISPOSITION_NEEDS_REVIEW
			base["riv_name"] = r.name
			base["evidence"] = ev
			return base
		base["riv_name"] = r.name
		base["evidence"] = ev
		base["disposition"] = (
			DISPOSITION_ADOPT_IN_PROGRESS
			if r.status == "In Progress"
			else DISPOSITION_ADOPT_QUEUED
		)
		return base

	# Failed without a proven Completed cover → recovery, do not create
	proven_completed = []
	for r in completed:
		ev = voucher_scope_evidence(
			campaign=campaign,
			item_code=item_code,
			warehouse=warehouse,
			posting_date=posting_date,
			posting_time=posting_time,
			riv_name=r.name,
			riv_posting_date=r.posting_date,
			riv_posting_time=r.posting_time,
			riv_status=r.status,
		)
		if ev.get("proven"):
			proven_completed.append((r, ev))

	if proven_completed:
		# Prefer exact posting, then earliest covering
		proven_completed.sort(
			key=lambda pair: (
				0 if pair[1].get("exact_posting") else 1,
				str(pair[0].posting_date),
				_norm_time(pair[0].posting_time),
				str(pair[0].creation),
			)
		)
		if len(proven_completed) > 1 and not proven_completed[0][1].get("exact_posting"):
			# Multiple non-exact covers without a clear winner beyond earliest — still adopt earliest
			# but flag if posting boundaries differ materially
			dates = {posting_tuple(p[0].posting_date, p[0].posting_time) for p in proven_completed}
			if len(dates) > 1 and not any(p[1].get("campaign_covering_match") for p in proven_completed):
				base["disposition"] = DISPOSITION_NEEDS_REVIEW
				base["evidence"] = {
					"reason": "multiple_completed_covers",
					"rivs": [p[0].name for p in proven_completed],
				}
				return base
		r, ev = proven_completed[0]
		base["riv_name"] = r.name
		base["evidence"] = ev
		base["disposition"] = DISPOSITION_ADOPT_COMPLETED
		return base

	if failed:
		# Prefer failed with exact/covering posting
		failed_match = [
			r
			for r in failed
			if posting_covers(r.posting_date, r.posting_time, posting_date, posting_time)
		]
		pick = failed_match[0] if failed_match else failed[0]
		base["disposition"] = DISPOSITION_FAILED_NEEDS_RECOVERY
		base["riv_name"] = pick.name
		base["evidence"] = {
			"reason": "failed_riv_requires_explicit_recovery",
			"status": pick.status,
			"posting_date": str(pick.posting_date),
			"posting_time": str(pick.posting_time),
		}
		return base

	if skipped:
		base["disposition"] = DISPOSITION_NEEDS_REVIEW
		base["riv_name"] = skipped[0].name
		base["evidence"] = {
			"reason": "skipped_or_cancelled_not_treated_as_completed",
			"status": skipped[0].status,
		}
		return base

	# Completed rows exist but none proven → review (do not duplicate blindly)
	if completed:
		base["disposition"] = DISPOSITION_NEEDS_REVIEW
		base["evidence"] = {
			"reason": "completed_rivs_without_proven_coverage",
			"rivs": [r.name for r in completed],
		}
		return base

	base["disposition"] = DISPOSITION_CREATE
	base["evidence"] = {"reason": "no_existing_riv"}
	return base


def disposition_to_ledger_fields(disp: dict[str, Any]) -> dict[str, Any]:
	"""Map disposition to staggered ledger entry fields."""
	d = disp.get("disposition")
	riv = disp.get("riv_name")
	common = {
		"identity_key": disp["identity_key"],
		"item_code": disp["item_code"],
		"warehouse": disp["warehouse"],
		"posting_date": disp["posting_date"],
		"posting_time": disp["posting_time"],
		"riv_name": riv,
		"adoption_disposition": d,
		"adoption_evidence": disp.get("evidence") or {},
		"adopted": d
		in (
			DISPOSITION_ADOPT_COMPLETED,
			DISPOSITION_ADOPT_QUEUED,
			DISPOSITION_ADOPT_IN_PROGRESS,
			DISPOSITION_LEDGER_REUSE,
		),
	}
	if d == DISPOSITION_ADOPT_COMPLETED:
		common.update(
			{
				"status": "adopted_completed",
				"dispatch_status": "terminal",
				"riv_status": "Completed",
			}
		)
	elif d == DISPOSITION_ADOPT_QUEUED:
		common.update(
			{
				"status": "adopted_queued",
				"dispatch_status": "pending",
				"riv_status": "Queued",
			}
		)
	elif d == DISPOSITION_ADOPT_IN_PROGRESS:
		common.update(
			{
				"status": "adopted_in_progress",
				"dispatch_status": "dispatched",
				"riv_status": "In Progress",
				"adopted_in_flight": True,
			}
		)
	elif d == DISPOSITION_FAILED_NEEDS_RECOVERY:
		common.update(
			{
				"status": "failed_needs_recovery",
				"dispatch_status": "blocked",
				"riv_status": "Failed",
			}
		)
	elif d == DISPOSITION_NEEDS_REVIEW:
		common.update(
			{
				"status": "needs_review",
				"dispatch_status": "blocked",
				"riv_status": None,
			}
		)
	else:
		common.update({"status": "pending_create", "dispatch_status": None})
	return common


def empty_reconciliation() -> dict[str, Any]:
	return {
		"adopted_completed": [],
		"adopted_queued": [],
		"adopted_in_progress": [],
		"newly_created": [],
		"failed_needs_recovery": [],
		"needs_review": [],
		"ledger_reused": [],
		"counts": {
			"adopted_completed": 0,
			"adopted_queued": 0,
			"adopted_in_progress": 0,
			"newly_created": 0,
			"failed_needs_recovery": 0,
			"needs_review": 0,
			"ledger_reused": 0,
			"plan_size": 0,
			"reconciled": 0,
		},
	}


def record_reconciliation(recon: dict[str, Any], disp: str, identity_key: str, riv_name: str | None):
	bucket_map = {
		DISPOSITION_ADOPT_COMPLETED: "adopted_completed",
		DISPOSITION_ADOPT_QUEUED: "adopted_queued",
		DISPOSITION_ADOPT_IN_PROGRESS: "adopted_in_progress",
		DISPOSITION_CREATE: "newly_created",
		DISPOSITION_FAILED_NEEDS_RECOVERY: "failed_needs_recovery",
		DISPOSITION_NEEDS_REVIEW: "needs_review",
		DISPOSITION_LEDGER_REUSE: "ledger_reused",
	}
	bucket = bucket_map.get(disp)
	if not bucket:
		bucket = "needs_review"
	recon[bucket].append({"identity_key": identity_key, "riv_name": riv_name})
	recon["counts"][bucket] = cint(recon["counts"].get(bucket)) + 1
	recon["counts"]["reconciled"] = cint(recon["counts"].get("reconciled")) + 1
