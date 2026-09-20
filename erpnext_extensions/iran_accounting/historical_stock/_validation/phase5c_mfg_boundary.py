# Copyright (c) 2026, ERPNext Extensions contributors
"""Prove Manufacture replay from posting_date (midnight) vs posting_datetime."""

from __future__ import annotations

import json

ITEM = "30300042"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"
ROOT = "MAT-STE-2026-27825"


def run():
	import frappe
	from frappe.utils import flt, get_datetime
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
		_fetch_previous,
		_fetch_sles,
		replay_series,
	)
	from erpnext_extensions.iran_accounting.historical_stock.replay import replay_from_patient_zero

	root_dt = frappe.db.sql(
		"""
		SELECT posting_datetime, posting_date FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime LIMIT 1
		""",
		(ROOT, ITEM, WH),
		as_dict=True,
	)[0]
	posting_date = str(root_dt.posting_date)
	posting_datetime = str(root_dt.posting_datetime)

	# Same-day SLEs before ROOT
	same_day = frappe.db.sql(
		"""
		SELECT voucher_no, actual_qty, qty_after_transaction, stock_value, valuation_rate,
		       stock_value_difference, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_date=%s AND posting_datetime < %s
		ORDER BY posting_datetime, creation
		""",
		(ITEM, WH, posting_date, posting_datetime),
		as_dict=True,
	)

	# Simulate old path: from_dt = posting_date (midnight)
	from_midnight = get_datetime(posting_date)
	prev_m = _fetch_previous(ITEM, WH, from_midnight)
	rows_m = _fetch_sles(ITEM, WH, from_midnight, before=False)
	# Attach purpose for manufacture SVD keep
	for r in rows_m:
		r["purpose"] = frappe.db.get_value("Stock Entry", r.voucher_no, "purpose")
	oq_m = flt(prev_m.qty_after_transaction) if prev_m else 0
	ov_m = flt(prev_m.stock_value) if prev_m else 0
	series_m = replay_series(rows_m, oq_m, ov_m)
	neg_m = [
		{
			"voucher": s.get("voucher_no"),
			"val": float(s["valuation_rate"]),
			"qty_after": float(s["qty_after_transaction"]),
			"sv": float(s["stock_value"]),
			"svd": float(s["stock_value_difference"]),
		}
		for s in series_m
		if float(s["valuation_rate"]) < -0.0001
	]

	# Correct path: from_dt = posting_datetime
	from_dt = get_datetime(posting_datetime)
	prev_d = _fetch_previous(ITEM, WH, from_dt)
	rows_d = _fetch_sles(ITEM, WH, from_dt, before=False)
	for r in rows_d:
		r["purpose"] = frappe.db.get_value("Stock Entry", r.voucher_no, "purpose")
	oq_d = flt(prev_d.qty_after_transaction) if prev_d else 0
	ov_d = flt(prev_d.stock_value) if prev_d else 0
	series_d = replay_series(rows_d, oq_d, ov_d)
	neg_d = [s for s in series_d if float(s["valuation_rate"]) < -0.0001]

	# Also: what if we INVERT outbound SVD artificially then replay from midnight?
	# Reconstruct the bug: after sync_sle on manufacture, outbound transfers keep
	# OLD inverted svd until replay overwrites — but replay_series for OUTGOING
	# ignores stored svd and uses MA. So inverted stored shouldn't matter...
	# Unless ignore: manufacture path also calls sync AFTER replay which can re-corrupt?

	# Check reconstruct order: replay then sync_sle again — sync only touches ROOT voucher.
	# Then write_sle_transaction_rates.

	# Hypothesis: opening qty negative when using wrong from_dt OR when previous
	# manufacture scrap/sample on same item different WH doesn't matter.
	# Check qty_after history same day.

	out = {
		"posting_date": posting_date,
		"posting_datetime": posting_datetime,
		"same_day_before_root_n": len(same_day),
		"same_day_before_root": same_day[:20],
		"midnight_path": {
			"from": str(from_midnight),
			"prev_voucher": prev_m.voucher_no if prev_m else None,
			"opening_qty": oq_m,
			"opening_value": ov_m,
			"window_n": len(rows_m),
			"neg_n": len(neg_m),
			"first_neg": neg_m[0] if neg_m else None,
			"neg_sample": neg_m[:5],
		},
		"datetime_path": {
			"from": str(from_dt),
			"prev_voucher": prev_d.voucher_no if prev_d else None,
			"opening_qty": oq_d,
			"opening_value": ov_d,
			"window_n": len(rows_d),
			"neg_n": len(neg_d),
		},
		"root_cause_hypothesis": (
			"replay_from_patient_zero(from_dt=posting_date) starts at midnight, "
			"including same-day prior SLEs under a different opening than the voucher boundary"
			if len(same_day) or (oq_m != oq_d)
			else "boundary date vs datetime same today; look at multi-voucher wave interaction / sync order"
		),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
