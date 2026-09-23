# Copyright (c) 2026, ERPNext Extensions contributors
"""Heal 30300042 Quarantine SLE chain after manufacture canary poison.

1) Fix sign-inverted outgoing SVDs using |qty|*outgoing_rate with correct sign
2) Moving-average replay from identity start
3) Commit only if neg valuation clears for this identity and globally non-increasing
"""

from __future__ import annotations

import json

ITEM = "30300042"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"


def run():
	import frappe
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
		replay_series,
		_update_bin,
		sle_poison_reason,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

	before = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
	)[0][0]
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value, stock_value_difference, qty_after_transaction, posting_datetime, creation
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		(ITEM, WH),
		as_dict=True,
	)
	fixed_svd = []
	frappe.db.begin()
	try:
		for r in rows:
			qty = flt(r.actual_qty)
			svd = flt(r.stock_value_difference)
			# Fix inverted outgoing / incoming SVD using txn rates.
			if qty < 0 and svd > 0.0001:
				rate = abs(flt(r.outgoing_rate) or flt(r.valuation_rate))
				if rate <= 0:
					# derive from abs svd
					rate = abs(svd / qty) if qty else 0
				new_svd = -(abs(qty) * rate)
				frappe.db.set_value(
					"Stock Ledger Entry",
					r.name,
					{"stock_value_difference": new_svd},
					update_modified=False,
				)
				r.stock_value_difference = new_svd
				fixed_svd.append({"sle": r.name, "voucher": r.voucher_no, "old": svd, "new": new_svd})
			elif qty > 0 and svd < -0.0001:
				rate = abs(flt(r.incoming_rate) or flt(r.valuation_rate))
				if rate <= 0:
					rate = abs(svd / qty) if qty else 0
				new_svd = abs(qty) * rate
				frappe.db.set_value(
					"Stock Ledger Entry",
					r.name,
					{"stock_value_difference": new_svd, "incoming_rate": rate},
					update_modified=False,
				)
				r.stock_value_difference = new_svd
				r.incoming_rate = rate
				fixed_svd.append({"sle": r.name, "voucher": r.voucher_no, "old": svd, "new": new_svd})

		# Re-fetch after SVD fixes
		rows = frappe.db.sql(
			"""
			SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
			       stock_value, stock_value_difference, qty_after_transaction, posting_datetime, creation
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			""",
			(ITEM, WH),
			as_dict=True,
		)
		series = replay_series(rows, opening_qty=0, opening_value=0)
		for i, row in enumerate(rows):
			step = series[i]
			frappe.db.set_value(
				"Stock Ledger Entry",
				row.name,
				{
					"qty_after_transaction": flt(step["qty_after_transaction"]),
					"stock_value": flt(step["stock_value"]),
					"stock_value_difference": flt(step["stock_value_difference"]),
					"valuation_rate": flt(step["valuation_rate"]),
				},
				update_modified=False,
			)
		if series:
			_update_bin(
				ITEM,
				WH,
				series[-1]["qty_after_transaction"],
				series[-1]["stock_value"],
				series[-1]["valuation_rate"],
			)

		after = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0]
		id_neg = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0 AND valuation_rate < -0.0001
			  AND item_code=%s AND warehouse=%s
			""",
			(ITEM, WH),
		)[0][0]
		# Also check remaining inverted svd
		inv = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND (
			    (actual_qty < 0 AND stock_value_difference > 0.0001)
			    OR (actual_qty > 0 AND stock_value_difference < -0.0001)
			  )
			""",
			(ITEM, WH),
		)[0][0]
		out = {
			"before_neg": before,
			"after_neg": after,
			"identity_neg": id_neg,
			"inverted_remaining": inv,
			"fixed_svd_n": len(fixed_svd),
			"fixed_svd_sample": fixed_svd[:10],
			"final": {
				"qty": flt(series[-1]["qty_after_transaction"]) if series else None,
				"value": flt(series[-1]["stock_value"]) if series else None,
				"rate": flt(series[-1]["valuation_rate"]) if series else None,
			},
		}
		if after == 0 and id_neg == 0 and inv == 0:
			frappe.db.commit()
			out["committed"] = True
		else:
			frappe.db.rollback()
			out["committed"] = False
			# Soft commit if we at least cleared identity and reduced global?
			# No — only commit full clear.
	except Exception as exc:
		frappe.db.rollback()
		out = {"error": str(exc), "before_neg": before}

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
