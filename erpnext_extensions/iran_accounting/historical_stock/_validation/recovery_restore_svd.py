# Copyright (c) 2026 — restore SVD zeroed by WR replay for implied_svd repairs
from __future__ import annotations

import json


def run(*, apply=1, limit=500):
	"""Find SLE where |qty|>0, txn rate>0, |SVD|~0 — restore SVD = sign(qty)*rate*|qty|."""
	import frappe
	from frappe.utils import flt

	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, item_code, warehouse, actual_qty,
		       incoming_rate, outgoing_rate, stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0
		  AND ABS(IFNULL(actual_qty,0)) > 0.0001
		  AND ABS(IFNULL(stock_value_difference,0)) < 0.5
		  AND (
		    (actual_qty < 0 AND IFNULL(outgoing_rate,0) > 0.5)
		    OR (actual_qty > 0 AND IFNULL(incoming_rate,0) > 0.5)
		  )
		ORDER BY posting_datetime
		LIMIT %s
		""",
		(int(limit),),
		as_dict=True,
	)
	restored = []
	for sle in rows:
		qty = flt(sle.actual_qty)
		rate = flt(sle.outgoing_rate if qty < 0 else sle.incoming_rate)
		svd = (-1 if qty < 0 else 1) * abs(rate) * abs(qty)
		entry = {
			"sle": sle.name,
			"voucher": sle.voucher_no,
			"item": sle.item_code,
			"warehouse": sle.warehouse,
			"qty": qty,
			"rate": rate,
			"svd_before": flt(sle.stock_value_difference),
			"svd_after": svd,
		}
		if int(apply):
			frappe.db.set_value(
				"Stock Ledger Entry",
				sle.name,
				{"stock_value_difference": svd},
				update_modified=False,
			)
			entry["written"] = True
		restored.append(entry)
	if int(apply) and restored:
		frappe.db.commit()
	out = {"count": len(rows), "restored": len(restored), "sample": restored[:20]}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
