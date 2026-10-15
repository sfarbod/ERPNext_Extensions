# Copyright (c) 2026, ERPNext Extensions contributors
"""Bulk mark campaign items Skipped Covered when Core RIV already covers identities."""

from __future__ import annotations

import frappe
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_DOCTYPE,
	ITEM_PENDING,
	ITEM_SKIPPED,
	TVA_NA,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	find_covering_riv,
	verify_voucher_basic,
)
from erpnext_extensions.iran_accounting.historical_rebuild.tva_reconcile import (
	reconcile_tva_for_voucher,
)


def bulk_skip_covered(campaign: str, limit: int = 500) -> dict:
	"""Scan Pending items; mark Skipped Covered when every SLE identity has Completed cover."""
	items = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign, "item_status": ITEM_PENDING},
		fields=["name", "voucher_type", "voucher_no", "ordinal"],
		order_by="ordinal asc",
		limit_page_length=cint(limit) or 500,
	)
	skipped = 0
	left = 0
	for it in items:
		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={
				"voucher_type": it.voucher_type,
				"voucher_no": it.voucher_no,
				"is_cancelled": 0,
			},
			fields=["item_code", "warehouse", "posting_date", "posting_time"],
			limit_page_length=0,
		)
		if not sles:
			left += 1
			continue
		seen = set()
		covers = []
		ok = True
		for sle in sles:
			key = (sle.item_code, sle.warehouse)
			if key in seen:
				continue
			seen.add(key)
			cov = find_covering_riv(sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time)
			if not cov:
				ok = False
				break
			covers.append(cov)
		if not ok:
			left += 1
			continue
		ver = verify_voucher_basic(it.voucher_type, it.voucher_no)
		if not ver.get("ok"):
			left += 1
			continue
		tva = reconcile_tva_for_voucher(it.voucher_type, it.voucher_no)
		frappe.db.set_value(
			ITEM_DOCTYPE,
			it.name,
			{
				"item_status": ITEM_SKIPPED,
				"covering_riv": covers[0] if covers else None,
				"tva_reconcile_status": tva.get("status") or TVA_NA,
				"last_processed_at": now_datetime(),
				"error_message": "",
			},
			update_modified=False,
		)
		skipped += 1
	frappe.db.commit()
	return {"campaign": campaign, "skipped_covered": skipped, "still_pending_in_batch": left, "scanned": len(items)}
