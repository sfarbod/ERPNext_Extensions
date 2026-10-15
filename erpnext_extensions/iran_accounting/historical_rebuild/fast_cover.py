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


def _covered_pending_candidates(campaign: str, limit: int) -> list[dict]:
	"""Pending campaign items whose every SLE identity already has Completed cover.

	Uses a set-oriented anti-join so early multi-identity Opening SRs do not
	block discovery of later vouchers that are already fully covered.
	"""
	cap = cint(limit) or 500
	return frappe.db.sql(
		"""
		select i.name, i.voucher_type, i.voucher_no, i.ordinal
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
	"""Mark Skipped Covered when every SLE identity has Completed cover.

	Prefers SQL candidates that are already fully covered so early Opening
	Stock Reconciliations do not monopolize the scan window. Falls back to
	ordinal Pending scan for no-SLE / edge vouchers.
	"""
	cap = cint(limit) or 500
	items = _covered_pending_candidates(campaign, cap)
	# Also pick up no-SLE pending vouchers (fixed asset / non-stock) via a
	# small ordinal scan — they never appear in the SLE anti-join as covered.
	if len(items) < cap:
		extra = frappe.get_all(
			ITEM_DOCTYPE,
			filters={"campaign": campaign, "item_status": ITEM_PENDING},
			fields=["name", "voucher_type", "voucher_no", "ordinal"],
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
	for it in items:
		# Do not use limit_page_length=0 — Frappe treats 0 as "return no rows".
		sles = frappe.db.sql(
			"""
			select item_code, warehouse, posting_date, posting_time
			from `tabStock Ledger Entry`
			where is_cancelled=0 and voucher_type=%s and voucher_no=%s
			  and item_code is not null and item_code!=''
			  and warehouse is not null and warehouse!=''
			""",
			(it.voucher_type, it.voucher_no),
			as_dict=True,
		) or []
		if not sles:
			# Fixed-asset / non-stock vouchers: verify without SLE cover.
			ver = verify_voucher_basic(it.voucher_type, it.voucher_no)
			if ver.get("ok"):
				tva = reconcile_tva_for_voucher(it.voucher_type, it.voucher_no)
				frappe.db.set_value(
					ITEM_DOCTYPE,
					it.name,
					{
						"item_status": ITEM_SKIPPED,
						"covering_riv": None,
						"tva_reconcile_status": tva.get("status") or TVA_NA,
						"last_processed_at": now_datetime(),
						"error_message": f"no_sle:{ver.get('detail')}",
					},
					update_modified=False,
				)
				skipped += 1
			else:
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
	return {
		"campaign": campaign,
		"skipped_covered": skipped,
		"still_pending_in_batch": left,
		"scanned": len(items),
	}
