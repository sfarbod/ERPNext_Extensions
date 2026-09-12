# Copyright (c) 2026, ERPNext Extensions contributors
"""Post-repair integrity gate for production posting-order changes."""

from __future__ import annotations

import frappe
from frappe.utils import flt


def integrity_check(vouchers: list[str], *, item_code=None, warehouse=None) -> dict:
	failures = []
	for vn in vouchers or []:
		se = frappe.db.get_value(
			"Stock Entry",
			vn,
			["name", "docstatus", "posting_date", "posting_time"],
			as_dict=True,
		)
		if not se:
			failures.append({"voucher": vn, "error": "missing_stock_entry"})
			continue
		sle_count = frappe.db.count(
			"Stock Ledger Entry",
			{"voucher_type": "Stock Entry", "voucher_no": vn, "is_cancelled": 0},
		)
		if se.docstatus == 1 and sle_count == 0:
			failures.append({"voucher": vn, "error": "orphan_missing_sle"})
		gl = frappe.db.sql(
			"""
			SELECT SUM(debit) debit, SUM(credit) credit, COUNT(*) n
			FROM `tabGL Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
			""",
			vn,
			as_dict=True,
		)[0]
		if se.docstatus == 1 and flt(gl.n):
			if abs(flt(gl.debit) - flt(gl.credit)) > 0.5:
				failures.append(
					{
						"voucher": vn,
						"error": "gl_unbalanced",
						"debit": gl.debit,
						"credit": gl.credit,
					}
				)
		dup = frappe.db.sql(
			"""
			SELECT item_code, warehouse, actual_qty, COUNT(*) c
			FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
			GROUP BY item_code, warehouse, actual_qty, posting_datetime
			HAVING COUNT(*) > 1
			""",
			vn,
			as_dict=True,
		)
		if dup:
			failures.append({"voucher": vn, "error": "duplicate_sle", "rows": dup})

	neg = []
	if item_code and warehouse:
		neg = frappe.db.sql(
			"""
			SELECT voucher_no, qty_after_transaction, posting_datetime, creation
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND qty_after_transaction < 0
			ORDER BY posting_datetime, creation
			LIMIT 20
			""",
			(item_code, warehouse),
			as_dict=True,
		)

	return {
		"ok": not failures,
		"failures": failures,
		"temporary_negatives": neg,
	}
