# Copyright (c) 2026, ERPNext Extensions contributors
"""Audit negative valuation_rate SLEs after manufacture canary."""

from __future__ import annotations

import json


def run():
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT sle.name, sle.voucher_no, sle.item_code, sle.warehouse, sle.actual_qty,
		       sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate,
		       sle.stock_value_difference, sle.stock_value, sle.qty_after_transaction,
		       se.purpose, sle.posting_datetime
		FROM `tabStock Ledger Entry` sle
		LEFT JOIN `tabStock Entry` se ON se.name=sle.voucher_no AND sle.voucher_type='Stock Entry'
		WHERE sle.is_cancelled=0 AND sle.valuation_rate < -0.0001
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT 50
		""",
		as_dict=True,
	)
	by_v = {}
	for r in rows:
		by_v.setdefault(r.voucher_no, []).append(r)
	out = {
		"count": len(rows),
		"vouchers": list(by_v.keys()),
		"by_purpose": {},
		"samples": rows[:20],
	}
	for r in rows:
		p = r.purpose or "?"
		out["by_purpose"][p] = out["by_purpose"].get(p, 0) + 1
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
