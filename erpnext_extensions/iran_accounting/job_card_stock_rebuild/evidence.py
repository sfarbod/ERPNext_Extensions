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
			elif bucket in ("FINISHED", "SECONDARY_OUTPUT", "MFG_OTHER"):
				# FG / Co-/By-/AFG outputs are not component WIP drains.
				pass
			else:
				# Proven component WIP outflow that is not CONSUME/RETURN/SCRAP.
				agg["other"] += qty

		if se.purpose == "Manufacture":
			mfg_postings.append(se)
		elif se.purpose == "Material Transfer for Manufacture":
			transfer_postings.append(se)

	# Pair COMPONENT_SCRAP outputs to WIP CONSUME so scrap is not double-counted.
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.scrap_pairing import (
		golden_remainder,
		pair_scrap_for_job_card_evidence,
	)

	pairing = pair_scrap_for_job_card_evidence(movements)
	paired_map = pairing.get("paired_scrap_qty") or {}

	# Finalize remainders — PHYSICAL WIP drain = CONSUME only when scrap is paired.
	items_out = []
	for (item, batch), agg in sorted(by_item_batch.items()):
		paired = flt(paired_map.get((item, batch)))
		remainder = golden_remainder(
			agg["issued"],
			agg["returned"],
			agg["consumed"],
			agg["component_scrap"],
			paired,
			agg["other"],
		)
		items_out.append(
			{
				"item_code": item,
				"batch_no": batch,
				"issued": flt(agg["issued"]),
				"returned": flt(agg["returned"]),
				"consumed": flt(agg["consumed"]),
				"component_scrap": flt(agg["component_scrap"]),
				"paired_component_scrap": paired,
				"product_reject": flt(agg["product_reject"]),
				"ordinary_scrap": flt(agg["ordinary_scrap"]),
				"other": flt(agg["other"]),
				"wip_remainder": remainder,
				"net_available_for_manufacture": flt(agg["issued"]) - flt(agg["returned"]) - flt(agg["consumed"]),
				"scrap_pair_ok": flt(agg["component_scrap"]) <= paired + 1e-9,
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

	# Job-Card dependency closure only — do NOT include unrelated JC / global DB stamps.
	wo_name = frappe.db.get_value("Job Card", job_card, "work_order")
	wo_identity = {}
	if wo_name:
		wo_identity = frappe.db.get_value(
			"Work Order",
			wo_name,
			["name", "bom_no", "production_item", "wip_warehouse", "fg_warehouse"],
			as_dict=1,
		) or {}
	jc_items_fp = frappe.db.sql(
		"""
		select name, item_code, transferred_qty, consumed_qty, required_qty,
		       custom_issued_qty, custom_returned_qty, custom_still_in_wip, custom_returnable_qty
		from `tabJob Card Item` where parent=%s order by idx
		""",
		job_card,
		as_dict=1,
	)
	jc_sec_fp = []
	try:
		cols = set(frappe.db.get_table_columns("Job Card Secondary Item") or [])
		sec_fields = [
			c
			for c in (
				"name",
				"item_code",
				"secondary_item_type",
				"stock_qty",
				"stock_uom",
				"bom_secondary_item",
				"custom_output_equivalent_factor",
			)
			if c in cols
		]
		if sec_fields:
			jc_sec_fp = frappe.db.sql(
				f"select {', '.join(sec_fields)} from `tabJob Card Secondary Item` where parent=%s order by idx",
				job_card,
				as_dict=1,
			)
	except Exception:
		jc_sec_fp = []
	# SLE belonging to this JC's Stock Entries only (not global SLE stamps)
	se_names = [s.name for s in ses]
	sle_fp = []
	if se_names:
		sle_fp = frappe.db.sql(
			"""
			select name, voucher_no, item_code, actual_qty, modified
			from `tabStock Ledger Entry`
			where voucher_no in %s and is_cancelled=0
			order by voucher_no, name
			""",
			(se_names,),
			as_dict=1,
		)
	fingerprint_payload = {
		"job_card": job_card,
		"jc_modified": str(frappe.db.get_value("Job Card", job_card, "modified") or ""),
		"wo_identity": wo_identity,
		"jc_items": jc_items_fp,
		"jc_secondary": jc_sec_fp,
		"ses": [
			{"name": s.name, "modified": str(s.modified), "purpose": s.purpose, "is_return": _is_return(s)}
			for s in ses
		],
		"details": [
			{"name": m["detail_name"], "job_card_item": m["job_card_item"], "qty": m["qty"]}
			for m in movements
		],
		"sle": [
			{
				"name": s.name,
				"voucher_no": s.voucher_no,
				"item_code": s.item_code,
				"actual_qty": flt(s.actual_qty),
				"modified": str(s.modified),
			}
			for s in sle_fp
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
