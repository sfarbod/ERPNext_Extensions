# Copyright (c) 2026 — debug WR residual on one voucher
from __future__ import annotations

import json


def run(voucher="MAT-STE-2026-03129"):
	import frappe
	from frappe.utils import flt

	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		       stock_value_difference, valuation_rate, serial_and_batch_bundle
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		voucher,
		as_dict=True,
	)
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
		classify_wrong_rate_universe,
	)

	inv = classify_wrong_rate_universe(company="اسپاد فارمد دارو", limit=2000)
	row = next((r for r in (inv.get("ready_rows") or []) if r.get("voucher") == voucher), None)
	out = {
		"voucher": voucher,
		"sles": sles,
		"row": {
			k: (row or {}).get(k)
			for k in (
				"sle",
				"item",
				"warehouse",
				"current_value",
				"expected_value",
				"expected_source",
				"surface",
				"rate_status",
				"sql_updates",
				"flags",
			)
		}
		if row
		else None,
	}
	# Try write_sle_transaction_rates in isolation (rollback)
	if row and row.get("sle"):
		sp = "dbg_wr"
		frappe.db.savepoint(sp)
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import write_sle_transaction_rates

		written = write_sle_transaction_rates(voucher, row.get("item"))
		after = frappe.db.get_value(
			"Stock Ledger Entry",
			row.get("sle"),
			["outgoing_rate", "incoming_rate", "actual_qty", "stock_value_difference"],
			as_dict=True,
		)
		frappe.db.rollback(save_point=sp)
		out["write_preview"] = {"written": written, "after": after}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
