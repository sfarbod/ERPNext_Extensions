# Copyright (c) 2026, ERPNext Extensions contributors
"""Authoritative stock-movement evidence for one Job Card."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

import frappe
from frappe.utils import cint, flt


def _is_return(se_row) -> bool:
	return bool(cint(se_row.get("is_return")) or cint(se_row.get("custom_is_returned")))


def collect_job_card_stock_entries(job_card: str) -> list[dict]:
	"""Submitted Stock Entries owned by ``Stock Entry.job_card``."""
	if not job_card:
		return []
	return frappe.db.sql(
		"""
		select name, purpose, stock_entry_type, docstatus, posting_date, posting_time,
		       work_order, job_card, is_return, creation, modified,
		       custom_rahkaran_no, custom_is_returned, fg_completed_qty
		from `tabStock Entry`
		where job_card=%s and docstatus=1
		order by posting_date, posting_time, creation, name
		""",
		job_card,
		as_dict=1,
	)


def collect_detail_rows(stock_entry: str) -> list[dict]:
	return frappe.db.sql(
		"""
		select name, idx, item_code, qty, transfer_qty, uom, stock_uom, conversion_factor,
		       s_warehouse, t_warehouse, batch_no, serial_and_batch_bundle,
		       is_finished_item, is_scrap_item, secondary_item_type, valuation_type,
		       job_card_item, basic_rate, valuation_rate, basic_amount, amount,
		       custom_output_class, custom_output_equivalent_factor,
		       custom_physical_conversion, custom_equivalent_qty, custom_common_uom,
		       custom_parent_co_product
		from `tabStock Entry Detail`
		where parent=%s
		order by idx
		""",
		stock_entry,
		as_dict=1,
	)


def collect_sle(voucher_no: str, item_code: str | None = None) -> list[dict]:
	conds = ["voucher_no=%s", "is_cancelled=0"]
	vals: list = [voucher_no]
	if item_code:
		conds.append("item_code=%s")
		vals.append(item_code)
	return frappe.db.sql(
		f"""
		select name, posting_date, posting_time, creation, item_code, warehouse,
		       actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value_difference, batch_no, serial_and_batch_bundle,
		       voucher_detail_no, modified
		from `tabStock Ledger Entry`
		where {" AND ".join(conds)}
		order by posting_date, posting_time, creation, name
		""",
		vals,
		as_dict=1,
	)


def classify_movement(purpose: str, is_return: bool, row: dict) -> str | None:
	"""Classify one SE Detail row into rebuild buckets."""
	purpose = purpose or ""
	qty = flt(row.get("transfer_qty") or row.get("qty"))
	if qty == 0:
		return None
	s_wh = row.get("s_warehouse")
	t_wh = row.get("t_warehouse")
	sec = (row.get("secondary_item_type") or "").strip()
	is_scrap = cint(row.get("is_scrap_item"))

	if purpose == "Material Transfer for Manufacture":
		if is_return:
			return "RETURN"
		if s_wh and t_wh:
			return "ISSUE"
		if t_wh and not s_wh:
			return "ISSUE"
		return "OTHER"
	if purpose == "Manufacture":
		if s_wh and not t_wh:
			return "CONSUME"
		if t_wh and (sec == "Scrap" or is_scrap):
			# Component scrap / reject outputs land on t_warehouse
			out_class = (row.get("custom_output_class") or "").strip()
			if out_class == "MAIN_PRODUCT_REJECT":
				return "PRODUCT_REJECT"
			if out_class == "COMPONENT_SCRAP":
				return "COMPONENT_SCRAP"
			return "ORDINARY_SCRAP"
		if t_wh and cint(row.get("is_finished_item")):
			return "FINISHED"
		if t_wh and sec in ("Co-Product", "By-Product", "Additional Finished Good"):
			return "SECONDARY_OUTPUT"
		return "MFG_OTHER"
	return "OTHER"


def build_evidence(job_card: str, item_filter: str | None = None, batch_filter: str | None = None) -> dict:
	"""Full evidence package for SCAN."""
	ses = collect_job_card_stock_entries(job_card)
	movements = []
	by_item_batch: dict[tuple[str, str], dict] = defaultdict(
		lambda: {
			"issued": 0.0,
			"returned": 0.0,
			"consumed": 0.0,
			"component_scrap": 0.0,
			"product_reject": 0.0,
			"ordinary_scrap": 0.0,
			"other": 0.0,
			"evidence": [],
			"link_candidates": [],
		}
	)
	mfg_postings = []
	transfer_postings = []

	for se in ses:
		is_ret = _is_return(se)
		details = collect_detail_rows(se.name)
		for row in details:
			item = row.item_code
			if item_filter and item != item_filter:
				continue
			batch = row.batch_no or ""
			if batch_filter is not None and batch != (batch_filter or ""):
				continue
			bucket = classify_movement(se.purpose, is_ret, row)
			if not bucket:
				continue
			qty = flt(row.transfer_qty or row.qty)
			key = (item, batch)
			rec = {
				"voucher": se.name,
				"purpose": se.purpose,
				"is_return": is_ret,
				"posting_date": str(se.posting_date),
				"posting_time": str(se.posting_time),
				"modified": str(se.modified),
				"rahkaran": se.custom_rahkaran_no,
				"detail_name": row.name,
				"idx": row.idx,
				"item_code": item,
				"batch_no": batch,
				"qty": qty,
				"uom": row.uom,
				"stock_uom": row.stock_uom,
				"conversion_factor": flt(row.conversion_factor),
				"s_warehouse": row.s_warehouse,
				"t_warehouse": row.t_warehouse,
				"job_card_item": row.job_card_item,
				"secondary_item_type": row.secondary_item_type,
				"custom_output_class": row.get("custom_output_class"),
				"custom_output_equivalent_factor": row.get("custom_output_equivalent_factor"),
				"bucket": bucket,
			}
			movements.append(rec)
			agg = by_item_batch[key]
			agg["evidence"].append(rec)
			if bucket == "ISSUE":
				agg["issued"] += qty
				agg["link_candidates"].append(rec)
			elif bucket == "RETURN":
				agg["returned"] += qty
				agg["link_candidates"].append(rec)
			elif bucket == "CONSUME":
				agg["consumed"] += qty
			elif bucket == "COMPONENT_SCRAP":
				agg["component_scrap"] += qty
			elif bucket == "PRODUCT_REJECT":
				agg["product_reject"] += qty
			elif bucket == "ORDINARY_SCRAP":
				agg["ordinary_scrap"] += qty
			else:
				agg["other"] += qty

		if se.purpose == "Manufacture":
			mfg_postings.append(se)
		elif se.purpose == "Material Transfer for Manufacture":
			transfer_postings.append(se)

	# Finalize remainders
	items_out = []
	for (item, batch), agg in sorted(by_item_batch.items()):
		remainder = (
			flt(agg["issued"])
			- flt(agg["returned"])
			- flt(agg["consumed"])
			- flt(agg["component_scrap"])
			- flt(agg["other"])
		)
		items_out.append(
			{
				"item_code": item,
				"batch_no": batch,
				"issued": flt(agg["issued"]),
				"returned": flt(agg["returned"]),
				"consumed": flt(agg["consumed"]),
				"component_scrap": flt(agg["component_scrap"]),
				"product_reject": flt(agg["product_reject"]),
				"ordinary_scrap": flt(agg["ordinary_scrap"]),
				"other": flt(agg["other"]),
				"wip_remainder": remainder,
				"net_available_for_manufacture": flt(agg["issued"]) - flt(agg["returned"]) - flt(agg["consumed"]),
				"evidence": agg["evidence"],
				"link_candidates": agg["link_candidates"],
			}
		)

	posting_order_warning = False
	if mfg_postings and transfer_postings:
		earliest_mfg = min(
			(str(m.posting_date), str(m.posting_time), str(m.creation)) for m in mfg_postings
		)
		latest_xfer = max(
			(str(t.posting_date), str(t.posting_time), str(t.creation)) for t in transfer_postings
		)
		if earliest_mfg < latest_xfer:
			posting_order_warning = True

	fingerprint_payload = {
		"job_card": job_card,
		"jc_modified": str(frappe.db.get_value("Job Card", job_card, "modified") or ""),
		"wo": frappe.db.get_value("Job Card", job_card, "work_order"),
		"wo_modified": str(
			frappe.db.get_value(
				"Work Order", frappe.db.get_value("Job Card", job_card, "work_order"), "modified"
			)
			or ""
		),
		"ses": [
			{"name": s.name, "modified": str(s.modified), "purpose": s.purpose, "is_return": _is_return(s)}
			for s in ses
		],
		"details": [
			{"name": m["detail_name"], "job_card_item": m["job_card_item"], "qty": m["qty"]}
			for m in movements
		],
	}
	digest = hashlib.sha256(
		json.dumps(fingerprint_payload, sort_keys=True, default=str).encode()
	).hexdigest()

	return {
		"job_card": job_card,
		"stock_entries": ses,
		"movements": movements,
		"items": items_out,
		"posting_order_warning": posting_order_warning,
		"fingerprint": digest,
		"fingerprint_payload": fingerprint_payload,
	}


__all__ = [
	"build_evidence",
	"classify_movement",
	"collect_job_card_stock_entries",
	"collect_detail_rows",
	"collect_sle",
]
