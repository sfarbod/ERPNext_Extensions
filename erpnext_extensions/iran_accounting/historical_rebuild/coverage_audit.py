# Copyright (c) 2026, ERPNext Extensions contributors
"""100% Skipped Covered verification for Historical Rebuild Campaign."""

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
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	find_covering_riv,
	verify_voucher_basic,
)


def _sle_identities(voucher_type: str, voucher_no: str) -> list[dict]:
	return frappe.db.sql(
		"""
		select distinct item_code, warehouse, posting_date, posting_time
		from `tabStock Ledger Entry`
		where is_cancelled=0 and voucher_type=%s and voucher_no=%s
		  and item_code is not null and item_code!=''
		  and warehouse is not null and warehouse!=''
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	) or []


def audit_skipped_covered(campaign: str, *, fix: bool = False, limit: int | None = None) -> dict[str, Any]:
	"""Verify every Skipped Covered item; optionally return invalid ones to Pending.

	Coverage requires:
	- verify_voucher_basic ok (SLE or FA/non-stock PR rules)
	- every SLE identity has a Completed covering RIV at earlier/equal posting
	"""
	kwargs: dict[str, Any] = {
		"filters": {"campaign": campaign, "item_status": ITEM_SKIPPED},
		"fields": ["name", "voucher_type", "voucher_no", "ordinal"],
		"order_by": "ordinal asc",
	}
	if limit is not None:
		kwargs["limit_page_length"] = cint(limit)
	names = frappe.get_all(ITEM_DOCTYPE, **kwargs)

	verified = 0
	invalid = []
	no_sle_ok = 0
	for row in names:
		ver = verify_voucher_basic(row.voucher_type, row.voucher_no)
		if not ver.get("ok"):
			invalid.append(
				{
					"name": row.name,
					"ordinal": row.ordinal,
					"voucher": f"{row.voucher_type}/{row.voucher_no}",
					"reason": f"verify:{ver.get('detail')}",
				}
			)
			continue
		idents = _sle_identities(row.voucher_type, row.voucher_no)
		if not idents:
			# FA / non-stock verified without SLE
			no_sle_ok += 1
			verified += 1
			continue
		missing = []
		for sle in idents:
			cov = find_covering_riv(
				sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
			)
			if not cov:
				missing.append(f"{sle.item_code}|{sle.warehouse}|{sle.posting_date}")
		if missing:
			invalid.append(
				{
					"name": row.name,
					"ordinal": row.ordinal,
					"voucher": f"{row.voucher_type}/{row.voucher_no}",
					"reason": f"uncovered:{len(missing)}",
					"sample": missing[:5],
				}
			)
			continue
		verified += 1

	requeued = 0
	if fix and invalid:
		for inv in invalid:
			frappe.db.set_value(
				ITEM_DOCTYPE,
				inv["name"],
				{
					"item_status": ITEM_PENDING,
					"error_message": f"coverage_audit:{inv.get('reason')}",
				},
				update_modified=False,
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
	}
