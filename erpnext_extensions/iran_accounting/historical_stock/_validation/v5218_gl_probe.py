# Copyright (c) 2026
from __future__ import annotations

import json


def run(voucher="MAT-STE-2026-36701"):
	import frappe
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	se = frappe.get_doc("Stock Entry", voucher)
	imap = se.get_inventory_account_map()
	raw = se.get_gl_entries(imap)
	expected = toggle_debit_credit_if_negative(raw)
	out = {
		"voucher": voucher,
		"purpose": se.purpose,
		"imap_keys": list((imap or {}).keys())[:8],
		"raw_n": len(raw or []),
		"expected_n": len(expected or []),
		"sample": [
			{"account": e.get("account"), "debit": e.get("debit"), "credit": e.get("credit"), "cost_center": e.get("cost_center")}
			for e in (expected or [])[:8]
		],
	}
	# Also find a G1/G3 candidate
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity

	scan = scan_gl_integrity(company="اسپاد فارمد دارو", limit=100)
	g13 = [
		{"voucher": r.get("voucher"), "gl_class": r.get("gl_class"), "diff": r.get("difference"), "eligible": r.get("eligible")}
		for r in (scan.get("rows") or [])
		if r.get("gl_class") in ("G1_BALANCED_BUT_ECONOMICALLY_WRONG", "G3_UNBALANCED")
	]
	out["g1_g3"] = g13[:10]
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:6000])
	return out
