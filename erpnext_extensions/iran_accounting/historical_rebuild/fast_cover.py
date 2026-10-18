# Copyright (c) 2026, ERPNext Extensions contributors
"""Bulk mark campaign items Skipped Covered only after evidence-based verification.

v5.6.1: A Completed Core RIV is NOT sufficient. Coverage requires identity match,
chronological scope, SLE continuity, no Failed RIV / open TVA, and voucher checks.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_DOCTYPE,
	ITEM_PENDING,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	apply_repost_required,
	apply_verified_skip,
	find_completed_covering_riv,
	verify_voucher_coverage,
)


def _candidate_pending(campaign: str, limit: int) -> list[dict]:
	"""Pending items that have at least a Completed chronological RIV per SLE identity.

	This is only a candidate pre-filter. Evidence verification still decides
	Skipped Covered vs remain Pending.
	"""
	cap = cint(limit) or 500
	return frappe.db.sql(
		"""
		select i.name, i.voucher_type, i.voucher_no, i.ordinal, i.company
		from `tabHistorical Rebuild Campaign Item` i
		where i.campaign=%s
		  and i.item_status=%s
		  and not exists (
			select 1
			from `tabStock Ledger Entry` sle
			where sle.is_cancelled=0
			  and sle.voucher_type=i.voucher_type
			  and sle.voucher_no=i.voucher_no
			  and sle.item_code is not null and sle.item_code!=''
			  and sle.warehouse is not null and sle.warehouse!=''
			  and not exists (
				select 1
				from `tabRepost Item Valuation` r
				where r.docstatus=1
				  and r.based_on='Item and Warehouse'
				  and r.status='Completed'
				  and r.item_code=sle.item_code
				  and r.warehouse=sle.warehouse
				  and (
					r.posting_date < sle.posting_date
					or (r.posting_date=sle.posting_date and r.posting_time<=sle.posting_time)
				  )
			  )
		  )
		order by i.ordinal asc
		limit %s
		""",
		(campaign, ITEM_PENDING, cap),
		as_dict=True,
	) or []


def bulk_skip_covered(campaign: str, limit: int = 500) -> dict:
	"""Mark Skipped Covered only when evidence-based verification passes.

	Prefers SQL candidates that already have Completed chronological cover so
	early Opening Stock Reconciliations do not monopolize the scan window.
	Falls back to ordinal Pending scan for no-SLE / edge vouchers.
	"""
	cap = cint(limit) or 500
	items = _candidate_pending(campaign, cap)
	if len(items) < cap:
		extra = frappe.get_all(
			ITEM_DOCTYPE,
			filters={"campaign": campaign, "item_status": ITEM_PENDING},
			fields=["name", "voucher_type", "voucher_no", "ordinal", "company"],
			order_by="ordinal asc",
			limit_page_length=min(200, cap - len(items)),
		)
		seen = {it["name"] for it in items}
		for it in extra or []:
			if it.name not in seen:
				items.append(it)
				seen.add(it.name)

	skipped = 0
	left = 0
	rejected_dirty = 0
	for it in items:
		result = verify_voucher_coverage(
			it.voucher_type, it.voucher_no, company=getattr(it, "company", None)
		)
		if result.get("ok"):
			apply_verified_skip(it.name, result)
			skipped += 1
		else:
			# Keep Pending; record why Completed RIV was insufficient
			frappe.db.set_value(
				ITEM_DOCTYPE,
				it.name,
				{
					"error_message": f"fast_cover_reject:{result.get('reason')}"[:1000],
					"last_processed_at": now_datetime(),
				},
				update_modified=False,
			)
			# Touch verification fields when present
			meta = frappe.get_meta(ITEM_DOCTYPE)
			if meta.has_field("verification_class"):
				frappe.db.set_value(
					ITEM_DOCTYPE,
					it.name,
					{"verification_class": result.get("verification_class") or "REPOST_REQUIRED"},
					update_modified=False,
				)
			left += 1
			rejected_dirty += 1
	frappe.db.commit()
	return {
		"campaign": campaign,
		"skipped_covered": skipped,
		"still_pending_in_batch": left,
		"rejected_insufficient_evidence": rejected_dirty,
		"scanned": len(items),
		"evidence_based": True,
	}


# Re-export for callers that previously imported chronological finder via fast_cover
__all__ = ["bulk_skip_covered", "find_completed_covering_riv", "apply_repost_required"]
