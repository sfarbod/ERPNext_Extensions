# Copyright (c) 2026, ERPNext Extensions contributors
"""Trace confirmed shortage false-positive: 10510117 / MAT-STE-2026-24520."""

from __future__ import annotations

import json

ITEM = "10510117"
VOUCHER = "MAT-STE-2026-24520"
COMPANY = "اسپاد فارمد دارو"


def run():
	import frappe
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	# Document
	se = frappe.db.get_value(
		"Stock Entry",
		VOUCHER,
		["name", "company", "purpose", "posting_date", "posting_time", "creation", "modified", "docstatus"],
		as_dict=True,
	)
	details = frappe.db.sql(
		"""
		SELECT name, idx, item_code, s_warehouse, t_warehouse, qty, transfer_qty,
		       basic_rate, batch_no, serial_and_batch_bundle
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND item_code=%s
		ORDER BY idx
		""",
		(VOUCHER, ITEM),
		as_dict=True,
	)

	# All SLEs for item (and for voucher)
	sles = frappe.db.sql(
		"""
		SELECT name, voucher_type, voucher_no, voucher_detail_no, warehouse, batch_no,
		       serial_and_batch_bundle, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate, stock_value,
		       stock_value_difference, posting_date, posting_time, posting_datetime,
		       creation, modified, is_cancelled
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		ITEM,
		as_dict=True,
	)
	voucher_sles = [s for s in sles if s.voucher_no == VOUCHER]

	# Warehouses involved
	warehouses = sorted({s.warehouse for s in sles if s.warehouse})

	# Per-warehouse qty mins from native SLE
	by_wh = {}
	for wh in warehouses:
		chain = [s for s in sles if s.warehouse == wh]
		mins = min((flt(s.qty_after_transaction) for s in chain), default=None)
		by_wh[wh] = {
			"n": len(chain),
			"min_qty_after": mins,
			"first": chain[0] if chain else None,
			"at_voucher": [s for s in chain if s.voucher_no == VOUCHER],
			"around": [],
		}
		# 5 before / after voucher in this WH
		idxs = [i for i, s in enumerate(chain) if s.voucher_no == VOUCHER]
		if idxs:
			i0 = idxs[0]
			by_wh[wh]["around"] = chain[max(0, i0 - 5) : i0 + 6]

	# PO scan row for this item/voucher
	po = run_full_history_scan(company=COMPANY)
	hits = []
	for raw in po.get("rows") or []:
		if raw.get("item") == ITEM or raw.get("outbound_document") == VOUCHER or raw.get("inbound_document") == VOUCHER:
			r = attach_plan(dict(raw))
			hits.append(
				{
					"item": r.get("item"),
					"warehouse": r.get("warehouse"),
					"batch": r.get("batch"),
					"out": r.get("outbound_document"),
					"in": r.get("inbound_document"),
					"opt": r.get("optimizer_status") or r.get("status"),
					"ps": r.get("planner_status"),
					"opening_qty": r.get("opening_qty"),
					"min_qty_before": r.get("min_qty_before"),
					"min_qty_after": r.get("min_qty_after"),
					"qty_before": r.get("qty_before"),
					"final_qty_before": r.get("final_qty_before"),
					"final_qty_after": r.get("final_qty_after"),
					"current_outbound_time": r.get("current_outbound_time"),
					"current_inbound_time": r.get("current_inbound_time"),
					"proposed_outbound_time": r.get("proposed_outbound_time"),
					"confidence": r.get("confidence"),
					"reason": (r.get("reason") or r.get("blocked_because") or "")[:240],
					"detection": r.get("detection"),
					"keys_sample": sorted([k for k, v in r.items() if v not in (None, "", [], {})])[:40],
				}
			)

	# Blocker rows
	blockers = frappe.get_all(
		"Historical Repair Blocker",
		filters={"item_code": ITEM},
		fields=[
			"name",
			"blocker_key",
			"lane",
			"status",
			"issue_type",
			"warehouse",
			"batch_no",
			"voucher_no",
			"root_cause",
			"recommended_user_action",
			"current_qty",
			"expected_qty",
			"payload_json",
		],
		limit_page_length=20,
	)

	out = {
		"se": se,
		"details": details,
		"voucher_sles": voucher_sles,
		"sle_total": len(sles),
		"warehouses": warehouses,
		"by_warehouse": {
			wh: {
				"n": v["n"],
				"min_qty_after": v["min_qty_after"],
				"first": v["first"],
				"at_voucher": v["at_voucher"],
				"around": v["around"],
			}
			for wh, v in by_wh.items()
		},
		"po_hits": hits,
		"blockers": blockers,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
