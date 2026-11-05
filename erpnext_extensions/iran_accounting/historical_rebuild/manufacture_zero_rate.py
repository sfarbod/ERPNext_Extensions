# Copyright (c) 2026, ERPNext Extensions contributors
"""Discovery and diagnosis for Manufacture zero-rate valuation defects.

Families:
1. VALUED_SOURCE_ZERO_OUTGOING — consume SED basic_rate=0 while warehouse MA / SLE
   outgoing is valued (existing riv_rate_guard contract).
2. ZERO_REJECT_ONLY — no MAIN_FG, MAIN_PRODUCT_REJECT at illegal zero while consume
   is valued (Job Card finished_good = scrap item; e.g. MAT-STE-2026-27570).
3. ZERO_MAIN_PRODUCT_REJECT — MAIN_FG present with illegal zero reject (CASE F).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt, getdate

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import is_valued_source_zero_outgoing


FAMILY_VALUED_SOURCE_ZERO_OUTGOING = "VALUED_SOURCE_ZERO_OUTGOING"
FAMILY_ZERO_REJECT_ONLY = "ZERO_REJECT_ONLY"
FAMILY_ZERO_MAIN_PRODUCT_REJECT = "ZERO_MAIN_PRODUCT_REJECT"


def _boundary(start_date):
	return getdate(start_date) if start_date else None


def discover_manufacture_zero_rate_defects(
	company: str | None = None,
	start_date=None,
	limit: int | None = None,
) -> dict[str, Any]:
	"""Read-only discovery of Manufacture zero-rate defects.

	``start_date`` filters vouchers by posting_date (>=). Upstream SLE history
	before that date is never mutated by discovery itself; rebuild engines still
	read earlier ledger state when calculating opening valuation.
	"""
	boundary = _boundary(start_date)
	filters: dict[str, Any] = {"purpose": "Manufacture", "docstatus": 1}
	if company:
		filters["company"] = company
	if boundary:
		filters["posting_date"] = (">=", boundary)

	names = frappe.get_all(
		"Stock Entry",
		filters=filters,
		pluck="name",
		order_by="posting_date asc, creation asc, name asc",
		limit_page_length=cint(limit) or 0,
	)

	from erpnext_extensions.iran_accounting.scrap_costing import (
		historical_zero_product_reject_needs_repair,
		historical_zero_reject_only_needs_repair,
	)

	rows: list[dict[str, Any]] = []
	counts = {
		FAMILY_VALUED_SOURCE_ZERO_OUTGOING: 0,
		FAMILY_ZERO_REJECT_ONLY: 0,
		FAMILY_ZERO_MAIN_PRODUCT_REJECT: 0,
	}
	for name in names:
		doc = frappe.get_doc("Stock Entry", name)
		families: list[str] = []
		if historical_zero_reject_only_needs_repair(doc):
			families.append(FAMILY_ZERO_REJECT_ONLY)
		elif historical_zero_product_reject_needs_repair(doc):
			families.append(FAMILY_ZERO_MAIN_PRODUCT_REJECT)

		zero_out_rows = []
		for row in doc.get("items") or []:
			if not row.get("s_warehouse"):
				continue
			# Approximate MA/outgoing from SED valuation_rate or SLE when present.
			outgoing = flt(row.get("valuation_rate")) or flt(row.get("basic_rate"))
			sle_out = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_detail_no": row.name, "is_cancelled": 0},
				"outgoing_rate",
			)
			if sle_out not in (None, ""):
				outgoing = flt(sle_out)
			if is_valued_source_zero_outgoing(
				actual_qty=-abs(flt(row.get("transfer_qty") or row.get("qty") or 0)),
				basic_rate=row.get("basic_rate"),
				allow_zero_valuation_rate=row.get("allow_zero_valuation_rate"),
				outgoing_rate=outgoing,
				purpose=doc.purpose,
			):
				zero_out_rows.append(row.item_code)
		if zero_out_rows:
			families.append(FAMILY_VALUED_SOURCE_ZERO_OUTGOING)

		if not families:
			continue
		for fam in families:
			counts[fam] = counts.get(fam, 0) + 1
		rows.append(
			{
				"voucher_type": "Stock Entry",
				"voucher_no": name,
				"posting_date": str(doc.posting_date),
				"work_order": doc.work_order,
				"job_card": doc.job_card,
				"families": families,
				"zero_outgoing_items": zero_out_rows,
			}
		)

	return {
		"company": company,
		"start_date": str(boundary) if boundary else None,
		"total": len(rows),
		"counts": counts,
		"rows": rows,
	}


def diagnose_voucher_zero_rate(voucher_no: str) -> dict[str, Any]:
	"""Single-voucher diagnosis for Manufacture zero-rate families."""
	if not frappe.db.exists("Stock Entry", voucher_no):
		return {"voucher_no": voucher_no, "exists": False}
	doc = frappe.get_doc("Stock Entry", voucher_no)
	from erpnext_extensions.iran_accounting.scrap_costing import (
		classify_manufacture_outputs,
		historical_zero_product_reject_needs_repair,
		historical_zero_reject_only_needs_repair,
	)

	classified = classify_manufacture_outputs(doc)
	return {
		"voucher_no": voucher_no,
		"exists": True,
		"purpose": doc.purpose,
		"posting_date": str(doc.posting_date),
		"docstatus": cint(doc.docstatus),
		"work_order": doc.work_order,
		"job_card": doc.job_card,
		"classes": {k: [r.item_code for r in v] for k, v in classified.items() if v},
		"needs_fg_reject_repair": historical_zero_product_reject_needs_repair(doc),
		"needs_reject_only_repair": historical_zero_reject_only_needs_repair(doc),
		"consume_amount": sum(flt(r.basic_amount) for r in doc.items if r.s_warehouse),
		"incoming_amount": sum(flt(r.basic_amount) for r in doc.items if r.t_warehouse),
	}
