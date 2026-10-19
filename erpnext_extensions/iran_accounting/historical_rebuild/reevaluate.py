# Copyright (c) 2026, ERPNext Extensions contributors
"""Re-evaluate Historical Rebuild Campaign entries with evidence-based coverage (v5.6.1).

Historical Skipped Covered records are not preserved blindly. Each entry is
re-checked; only VERIFIED_CLEAN evidence keeps Skipped Covered.

Performance: company-wide integrity scanner builds the dirty identity set
(including SQL continuity breaks and open TVA). Vouchers touching dirty
identities are requeued without per-row full chain walks. Remaining vouchers
use chronological Completed RIV + basic verify + Failed-RIV checks — continuity
for those identities was already asserted absent from the scanner dirty set.
"""

from __future__ import annotations

import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_BLOCKED,
	ITEM_DOCTYPE,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_SKIPPED,
	TVA_NA,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	apply_repost_required,
	apply_verified_skip,
	evidence_payload,
	find_completed_covering_riv,
	has_unresolved_failed_riv,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_scanner import (
	dirty_identity_set,
	run_full_scan,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	MANUAL_REVIEW,
	REPOST_FAILED,
	REPOST_REQUIRED,
	VERIFIED_CLEAN,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	verify_voucher_basic,
)
from erpnext_extensions.iran_accounting.historical_rebuild.tva_reconcile import (
	reconcile_tva_for_voucher,
)


def _voucher_touches_dirty(
	voucher_type: str, voucher_no: str, dirty: set[tuple[str, str]]
) -> bool:
	if not dirty:
		return False
	rows = frappe.db.sql(
		"""
		select distinct item_code, warehouse
		from `tabStock Ledger Entry`
		where is_cancelled=0 and voucher_type=%s and voucher_no=%s
		  and item_code is not null and item_code!=''
		  and warehouse is not null and warehouse!=''
		""",
		(voucher_type, voucher_no),
	)
	for item_code, warehouse in rows or []:
		if (item_code, warehouse) in dirty:
			return True
	return False


def _light_verify_non_dirty(
	voucher_type: str, voucher_no: str, *, company: str | None = None
) -> dict[str, Any]:
	"""Evidence for vouchers whose identities are absent from scanner dirty set.

	Requires: basic voucher ok, every SLE identity has Completed chronological
	cover, no Failed RIV. Continuity / zero-qty / neg-SV already screened by
	company-wide scanner into the dirty set.
	"""
	basic = verify_voucher_basic(voucher_type, voucher_no)
	out: dict[str, Any] = {
		"ok": False,
		"verification_class": REPOST_REQUIRED,
		"covering_rivs": [],
		"identity_results": [],
		"basic": basic,
		"tva": None,
		"reason": "",
	}
	if not basic.get("ok"):
		out["reason"] = f"verify_basic:{basic.get('detail')}"
		return out

	sles = (
		frappe.db.sql(
			"""
			select distinct item_code, warehouse, posting_date, posting_time
			from `tabStock Ledger Entry`
			where is_cancelled=0 and voucher_type=%s and voucher_no=%s
			  and item_code is not null and item_code!=''
			  and warehouse is not null and warehouse!=''
			""",
			(voucher_type, voucher_no),
			as_dict=True,
		)
		or []
	)
	if not sles:
		tva = reconcile_tva_for_voucher(voucher_type, voucher_no)
		out["tva"] = tva
		out["ok"] = True
		out["verification_class"] = VERIFIED_CLEAN
		out["reason"] = f"no_sle_scanner_clean:{basic.get('detail')}"
		return out

	covers: list[str] = []
	for sle in sles:
		failed = has_unresolved_failed_riv(sle.item_code, sle.warehouse)
		if failed:
			out["reason"] = f"unresolved_failed_riv:{failed}"
			return out
		riv = find_completed_covering_riv(
			sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
		)
		if not riv:
			out["reason"] = "no_completed_covering_riv"
			return out
		covers.append(riv)

	tva = reconcile_tva_for_voucher(voucher_type, voucher_no)
	out["tva"] = tva
	out["covering_rivs"] = list(dict.fromkeys(covers))
	out["ok"] = True
	out["verification_class"] = VERIFIED_CLEAN
	out["reason"] = f"scanner_clean_with_chronological_riv:{len(sles)}"
	return out


def reevaluate_campaign(
	campaign: str = "HRC-00001",
	*,
	company: str | None = None,
	limit: int | None = None,
	run_scanner: bool = True,
	scan_report: dict | None = None,
	commit_every: int = 100,
) -> dict[str, Any]:
	"""Re-evaluate all (or limited) campaign items; fix false Skipped Covered."""
	t0 = time.time()
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(f"Campaign {campaign} not found")

	co = company or frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "company")
	report = scan_report
	if run_scanner and report is None:
		report = run_full_scan(co, campaign=campaign)

	dirty: set[tuple[str, str]] = set()
	if report:
		cached = frappe.cache().get_value("hrc_integrity_dirty_identities")
		if cached:
			dirty = {(a, b) for a, b in cached}
		elif report.get("dirty_identities"):
			dirty = {(d["item_code"], d["warehouse"]) for d in report["dirty_identities"]}
		elif report.get("findings"):
			dirty = dirty_identity_set(report["findings"])

	kwargs: dict[str, Any] = {
		"filters": {"campaign": campaign},
		"fields": ["name", "voucher_type", "voucher_no", "item_status", "ordinal", "company"],
		"order_by": "ordinal asc",
	}
	if limit is not None:
		kwargs["limit_page_length"] = cint(limit)
	items = frappe.get_all(ITEM_DOCTYPE, **kwargs)

	counts = {
		"scanned": 0,
		"verified_clean": 0,
		"repost_required": 0,
		"manual_review": 0,
		"failed_preserved": 0,
		"was_skipped_covered": 0,
		"skipped_requeued": 0,
		"skipped_confirmed": 0,
	}
	samples = {"repost_required": [], "verified_clean": []}

	meta = frappe.get_meta(ITEM_DOCTYPE)
	has_vclass = meta.has_field("verification_class")
	has_vdetail = meta.has_field("verification_detail")

	for idx, row in enumerate(items):
		counts["scanned"] += 1
		prev_status = row.item_status
		if prev_status == ITEM_SKIPPED:
			counts["was_skipped_covered"] += 1
		if prev_status == ITEM_FAILED:
			counts["failed_preserved"] += 1
			if has_vclass:
				frappe.db.set_value(
					ITEM_DOCTYPE,
					row.name,
					{"verification_class": REPOST_FAILED},
					update_modified=False,
				)
			continue

		force_dirty = _voucher_touches_dirty(row.voucher_type, row.voucher_no, dirty)
		if force_dirty:
			result = {
				"ok": False,
				"verification_class": REPOST_REQUIRED,
				"reason": "identity_in_integrity_dirty_set",
				"covering_rivs": [],
				"identity_results": [],
			}
			apply_repost_required(row.name, result)
			counts["repost_required"] += 1
			if prev_status == ITEM_SKIPPED:
				counts["skipped_requeued"] += 1
			if len(samples["repost_required"]) < 20:
				samples["repost_required"].append(
					{"name": row.name, "voucher": f"{row.voucher_type}/{row.voucher_no}"}
				)
		else:
			result = _light_verify_non_dirty(
				row.voucher_type, row.voucher_no, company=row.company or co
			)
			if result.get("ok"):
				apply_verified_skip(row.name, result)
				counts["verified_clean"] += 1
				if prev_status == ITEM_SKIPPED:
					counts["skipped_confirmed"] += 1
				if len(samples["verified_clean"]) < 10:
					samples["verified_clean"].append(
						{
							"name": row.name,
							"voucher": f"{row.voucher_type}/{row.voucher_no}",
							"rivs": result.get("covering_rivs"),
						}
					)
			elif result.get("verification_class") == MANUAL_REVIEW:
				fields = {
					"item_status": ITEM_BLOCKED,
					"error_message": f"coverage_verify:{result.get('reason')}"[:1000],
					"last_processed_at": now_datetime(),
				}
				if has_vclass:
					fields["verification_class"] = MANUAL_REVIEW
				if has_vdetail:
					fields["verification_detail"] = evidence_payload(result)
				frappe.db.set_value(ITEM_DOCTYPE, row.name, fields, update_modified=False)
				counts["manual_review"] += 1
			else:
				apply_repost_required(row.name, result)
				counts["repost_required"] += 1
				if prev_status == ITEM_SKIPPED:
					counts["skipped_requeued"] += 1
				if len(samples["repost_required"]) < 20:
					samples["repost_required"].append(
						{
							"name": row.name,
							"voucher": f"{row.voucher_type}/{row.voucher_no}",
							"reason": result.get("reason"),
						}
					)

		if commit_every and (idx + 1) % cint(commit_every) == 0:
			frappe.db.commit()

	frappe.db.commit()

	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import (
		_refresh_counters,
	)

	counters = _refresh_counters(campaign)
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{"last_activity": now_datetime(), "status": "Paused"},
		update_modified=False,
	)
	frappe.db.commit()

	elapsed = time.time() - t0
	return {
		"campaign": campaign,
		"company": co,
		"runtime_seconds": round(elapsed, 3),
		"dirty_identity_count": len(dirty),
		"counts": counts,
		"samples": samples,
		"campaign_counters": counters,
		"scanner_report_path": (report or {}).get("report_path"),
		"ok": counts["scanned"] > 0,
	}
