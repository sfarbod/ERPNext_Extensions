# Copyright (c) 2026 — Probe CROSS_ITEM_CONFLICT for joint-safe promotion
from __future__ import annotations

import json

import frappe

COMPANY = "اسپاد فارمد دارو"


def run(limit=9):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	scan = run_full_history_scan(company=COMPANY)
	cross = [
		r
		for r in (scan.get("rows") or [])
		if str(r.get("optimizer_status") or r.get("status")) == "CROSS_ITEM_CONFLICT"
	][:limit]

	out = []
	for r in cross:
		out_vn = r.get("outbound_document") or r.get("negative_voucher") or r.get("voucher")
		in_vn = r.get("inbound_document") or r.get("later_inbound")
		touched = frappe.db.sql(
			"""
			SELECT item_code, warehouse, batch_no,
			       SUM(actual_qty) qty, SUM(stock_value_difference) svd, COUNT(*) n
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND is_cancelled=0
			GROUP BY item_code, warehouse, batch_no
			ORDER BY item_code
			""",
			out_vn,
			as_dict=True,
		)
		# Other CROSS/SHORTAGE rows sharing this outbound
		siblings = [
			{
				"item": x.get("item"),
				"wh": x.get("warehouse"),
				"opt": x.get("optimizer_status") or x.get("status"),
				"inbound": x.get("inbound_document") or x.get("later_inbound"),
			}
			for x in (scan.get("rows") or [])
			if (x.get("outbound_document") or x.get("negative_voucher")) == out_vn
			and str(x.get("optimizer_status") or x.get("status") or "") != "NO_REPAIR_NEEDED"
		]
		out.append(
			{
				"outbound": out_vn,
				"inbound": in_vn,
				"item": r.get("item"),
				"wh": r.get("warehouse"),
				"reason": r.get("dependency_reason"),
				"confidence": r.get("confidence"),
				"touched": touched,
				"siblings": siblings,
			}
		)
	print(json.dumps({"n": len(out), "rows": out}, ensure_ascii=False, indent=2, default=str))
	return {"n": len(out), "rows": out}
