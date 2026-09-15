# Copyright (c) 2026 — probe why G1 persists after GL rebuild
from __future__ import annotations

import json


def run(voucher="MAT-STE-2026-36677"):
	import frappe
	from frappe.utils import flt
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl

	se = frappe.get_doc("Stock Entry", voucher)
	imap = se.get_inventory_account_map()
	expected = toggle_debit_credit_if_negative(se.get_gl_entries(imap))
	exp_debit = sum(flt(e.get("debit")) for e in (expected or []))
	exp_credit = sum(flt(e.get("credit")) for e in (expected or []))
	before = classify_stock_entry_gl(voucher)
	sle_svd = frappe.db.sql(
		"""
		SELECT SUM(ABS(stock_value_difference)) s, COUNT(*) n
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		voucher,
	)[0]
	out = {
		"voucher": voucher,
		"purpose": se.purpose,
		"se_out": se.total_outgoing_value,
		"se_in": se.total_incoming_value,
		"expected_gl_n": len(expected or []),
		"expected_debit": exp_debit,
		"expected_credit": exp_credit,
		"gl_class": before.get("gl_class"),
		"stored_debit": before.get("stored_debit"),
		"diff": before.get("difference"),
		"sle_abs_svd": flt(sle_svd[0]),
		"sle_n": sle_svd[1],
		"gap_se_vs_gle": abs(max(flt(se.total_outgoing_value), flt(se.total_incoming_value)) - max(exp_debit, exp_credit)),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
