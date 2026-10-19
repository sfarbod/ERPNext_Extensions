# Copyright (c) 2026, ERPNext Extensions contributors
"""100% Skipped Covered verification for Historical Rebuild Campaign (v5.6.1).

Evidence-based: Completed RIV alone is insufficient. Invalid Skipped Covered
rows are returned to Pending when ``fix=True``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_DOCTYPE,
	ITEM_PENDING,
	ITEM_SKIPPED,
)
from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	apply_repost_required,
	verify_voucher_coverage,
)


def audit_skipped_covered(campaign: str, *, fix: bool = False, limit: int | None = None) -> dict[str, Any]:
	"""Verify every Skipped Covered item with full accounting evidence checks."""
	kwargs: dict[str, Any] = {
		"filters": {"campaign": campaign, "item_status": ITEM_SKIPPED},
		"fields": ["name", "voucher_type", "voucher_no", "ordinal", "company"],
		"order_by": "ordinal asc",
	}
	if limit is not None:
		kwargs["limit_page_length"] = cint(limit)
	names = frappe.get_all(ITEM_DOCTYPE, **kwargs)

	verified = 0
	invalid = []
	no_sle_ok = 0
	for row in names:
		result = verify_voucher_coverage(
			row.voucher_type, row.voucher_no, company=getattr(row, "company", None)
		)
		if result.get("ok"):
			if not (result.get("covering_rivs") or []) and "no_sle" in (result.get("reason") or ""):
				no_sle_ok += 1
			verified += 1
			continue
		invalid.append(
			{
				"name": row.name,
				"ordinal": row.ordinal,
				"voucher": f"{row.voucher_type}/{row.voucher_no}",
				"reason": result.get("reason") or "coverage_failed",
				"verification_class": result.get("verification_class"),
			}
		)

	requeued = 0
	if fix and invalid:
		for inv in invalid:
			apply_repost_required(
				inv["name"],
				{
					"ok": False,
					"verification_class": inv.get("verification_class") or "REPOST_REQUIRED",
					"reason": inv.get("reason"),
					"covering_rivs": [],
					"identity_results": [],
				},
			)
			requeued += 1
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"last_activity": now_datetime(), "status": "Running"},
			update_modified=False,
		)
		frappe.db.commit()

	return {
		"campaign": campaign,
		"skipped_covered_scanned": len(names),
		"verified": verified,
		"no_sle_verified": no_sle_ok,
		"invalid": len(invalid),
		"invalid_sample": invalid[:50],
		"requeued_to_pending": requeued,
		"ok": len(invalid) == 0 and len(names) > 0,
		"evidence_based": True,
	}
