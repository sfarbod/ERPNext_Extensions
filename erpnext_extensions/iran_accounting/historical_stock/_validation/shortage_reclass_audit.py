# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only shortage reclassification audit after opening-state fix (v5.3.0).

Does NOT repair stock. Classifies each prior REAL_STOCK_SHORTAGE finding.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.stock_posting_order.simulation import (
	D,
	align_movements_to_qty_after,
	is_authoritative_shortage,
	opening_state_before,
)
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import sle_sort_key


def _series_for(item, warehouse, batch=""):
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, batch_no, serial_and_batch_bundle,
		       voucher_type, voucher_no, actual_qty, qty_after_transaction,
		       posting_datetime, posting_date, posting_time, creation,
		       stock_value, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code=%s AND warehouse=%s
		  AND IFNULL(batch_no,'')=%s
		ORDER BY posting_datetime, creation
		""",
		(item, warehouse, batch or ""),
		as_dict=True,
	)
	for r in rows:
		r["canonical_batch"] = r.batch_no or ""
	return rows


def classify_finding(row: dict) -> str:
	"""Map one prior/current shortage hit to audit bucket."""
	item = row.get("item") or row.get("item_code")
	wh = row.get("warehouse")
	batch = row.get("batch") or row.get("batch_no") or ""
	out_vn = row.get("outbound_document") or row.get("voucher_no")
	if not item or not wh or not out_vn:
		return "AMBIGUOUS"

	series = _series_for(item, wh, batch)
	if not series:
		# Try without batch scope — possible batch-scope FP
		series_nb = _series_for(item, wh, "")
		if series_nb:
			return "FALSE_POSITIVE_BATCH_SCOPE"
		return "AMBIGUOUS"

	out_rows = [r for r in series if r.voucher_no == out_vn]
	if not out_rows:
		return "AMBIGUOUS"
	out = out_rows[0]
	native_qa = out.qty_after_transaction
	if native_qa is not None and not is_authoritative_shortage(qty_after=native_qa):
		# Determine FP subtype
		aligned = align_movements_to_qty_after(series, opening=0)
		# Did raw actual_qty sum go negative while qty_after did not?
		raw_run = D(0)
		raw_neg = False
		for r in sorted(series, key=sle_sort_key):
			raw_run += D(r.actual_qty)
			if raw_run < 0:
				raw_neg = True
				break
		# Opening RECO with aq=0?
		prior = [r for r in series if get_datetime(r.posting_datetime) < get_datetime(out.posting_datetime)
			or (get_datetime(r.posting_datetime) == get_datetime(out.posting_datetime)
				and str(r.creation) < str(out.creation))]
		opening_reco_zero_aq = any(
			r.voucher_type == "Stock Reconciliation"
			and flt(r.actual_qty) == 0
			and abs(flt(r.qty_after_transaction)) > 0.0001
			for r in prior
		)
		state = opening_state_before(series, out.posting_datetime, out.creation)
		if opening_reco_zero_aq or (raw_neg and D(state["opening_qty"]) > 0):
			return "FALSE_POSITIVE_OPENING_STATE"
		# Window: if sum of all prior actual_qty is 0 but qty_after opening > 0
		if D(state["opening_qty"]) > 0 and sum(flt(r.actual_qty) for r in prior) == 0:
			return "FALSE_POSITIVE_WINDOW_BOUNDARY"
		# Ordering FP unlikely if native qa >= 0 and no later inbound repair needed
		return "FALSE_POSITIVE_OTHER"

	# Authoritative negative — confirmed unless dependency waiting
	aligned = align_movements_to_qty_after(series, opening=0)
	run = D(0)
	min_q = D(0)
	for r in aligned:
		run += D(r["actual_qty"])
		if run < min_q:
			min_q = run
	if min_q >= 0:
		return "FALSE_POSITIVE_OTHER"
	# Check if a later inbound recovers (waiting dependency / chronology)
	after = [
		r for r in series
		if get_datetime(r.posting_datetime) > get_datetime(out.posting_datetime)
		and flt(r.actual_qty) > 0
	]
	if after:
		# Still negative at out, but later inbound exists — may be PO repairable
		# For shortage bucket we still call it confirmed if unrecovered at that point
		# and native qa < 0.
		pass
	return "CONFIRMED_REAL_SHORTAGE"


def run(company=None, sync_blockers=False):
	"""Audit current PO shortage findings; optionally sync blockers."""
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	# Before counts from open blockers
	before_blockers = frappe.db.count(
		"Historical Repair Blocker",
		{"issue_type": "HISTORICAL_NEGATIVE_STOCK", "status": "OPEN", "lane": "USER_ACTION_REQUIRED"},
	)
	before_user = frappe.db.count(
		"Historical Repair Blocker",
		{"lane": "USER_ACTION_REQUIRED", "status": "OPEN"},
	)

	po = run_full_history_scan(company=company, include_no_repair=True, include_likely=True)
	shortage_rows = [
		r
		for r in (po.get("rows") or [])
		if (r.get("optimizer_status") or r.get("status")) in ("REAL_STOCK_SHORTAGE", "INSUFFICIENT_STOCK")
	]

	# Also classify previously-known open shortage blockers (the "292")
	old_blockers = frappe.get_all(
		"Historical Repair Blocker",
		filters={
			"issue_type": "HISTORICAL_NEGATIVE_STOCK",
			"status": "OPEN",
			"lane": "USER_ACTION_REQUIRED",
		},
		fields=["name", "item_code", "warehouse", "batch_no", "voucher_no", "company"],
		limit=2000,
	)
	buckets = Counter()
	samples = defaultdict(list)
	for b in old_blockers:
		row = {
			"item": b.item_code,
			"warehouse": b.warehouse,
			"batch": b.batch_no,
			"outbound_document": b.voucher_no,
		}
		bucket = classify_finding(row)
		buckets[bucket] += 1
		if len(samples[bucket]) < 5:
			samples[bucket].append(
				{
					"item": b.item_code,
					"warehouse": b.warehouse,
					"voucher": b.voucher_no,
					"batch": b.batch_no,
					"blocker": b.name,
				}
			)

	case_105 = classify_finding(
		{
			"item": "10510117",
			"warehouse": "انبار ملزومات مصرفی اسپاد",
			"batch": "",
			"outbound_document": "MAT-STE-2026-24520",
		}
	)
	case_still_shortage = any(
		(r.get("item") == "10510117" and r.get("outbound_document") == "MAT-STE-2026-24520")
		for r in shortage_rows
	)

	sync_result = None
	if sync_blockers:
		from erpnext_extensions.iran_accounting.historical_stock.blockers import scan_and_sync_blockers

		sync_result = scan_and_sync_blockers(company=company, include_tool_limits=False, limit=500)

	after_blockers = frappe.db.count(
		"Historical Repair Blocker",
		{"issue_type": "HISTORICAL_NEGATIVE_STOCK", "status": "OPEN", "lane": "USER_ACTION_REQUIRED"},
	)
	after_user = frappe.db.count(
		"Historical Repair Blocker",
		{"lane": "USER_ACTION_REQUIRED", "status": "OPEN"},
	)
	case_blocker = frappe.db.sql(
		"""
		SELECT name, status, lane, issue_type
		FROM `tabHistorical Repair Blocker`
		WHERE item_code=%s AND voucher_no=%s
		ORDER BY modified DESC LIMIT 3
		""",
		("10510117", "MAT-STE-2026-24520"),
		as_dict=True,
	)

	return {
		"before_open_shortage_blockers": before_blockers,
		"before_open_user_action": before_user,
		"old_blocker_reclass": dict(buckets),
		"old_blocker_total": sum(buckets.values()),
		"samples": dict(samples),
		"after_scan_shortage_findings": len(shortage_rows),
		"case_10510117_bucket": case_105,
		"case_still_in_shortage_scan": case_still_shortage,
		"case_blockers": case_blocker,
		"sync": {
			"ran": bool(sync_blockers),
			"result_summary": (
				{
					"created": (sync_result or {}).get("created"),
					"updated": (sync_result or {}).get("updated"),
					"shortage_summary": (sync_result or {}).get("shortage_summary"),
					"user_action_required": (sync_result or {}).get("user_action_required"),
				}
				if sync_result
				else None
			),
		},
		"after_open_shortage_blockers": after_blockers,
		"after_open_user_action": after_user,
		"po_summary_keys": sorted((po.get("summary") or {}).keys())[:40],
		"po_shortage_status_count": sum(
			1
			for r in (po.get("rows") or [])
			if (r.get("optimizer_status") or "") == "REAL_STOCK_SHORTAGE"
		),
	}
