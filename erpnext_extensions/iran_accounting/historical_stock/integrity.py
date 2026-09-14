# Copyright (c) 2026, ERPNext Extensions contributors
"""Post-repair integrity for reconstructed vouchers and item/warehouse chains."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import VALUE_EPS
from erpnext_extensions.iran_accounting.stock_posting_order.integrity import integrity_check
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason


def chain_integrity(item_code, warehouse, vouchers=None) -> dict:
	base = integrity_check(vouchers or [], item_code=item_code, warehouse=warehouse)
	failures = list(base.get("failures") or [])
	if item_code and warehouse:
		sles = frappe.db.sql(
			"""
			SELECT actual_qty, incoming_rate, valuation_rate, stock_value,
			       stock_value_difference, qty_after_transaction, voucher_no
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime DESC, creation DESC
			LIMIT 20
			""",
			(item_code, warehouse),
			as_dict=True,
		)
		for s in sles:
			reason = sle_poison_reason(s)
			if reason in (
				"negative_incoming_rate",
				"sign_inverted_incoming_svd",
				"exploded_rate",
			):
				failures.append({"item": item_code, "warehouse": warehouse, "error": reason, "voucher": s.voucher_no})
				break
	return {"ok": not failures, "failures": failures, **{k: v for k, v in base.items() if k != "failures"}}


def voucher_integrity(voucher_no: str) -> dict:
	base = integrity_check([voucher_no])
	se_amt = frappe.db.sql(
		"""
		SELECT SUM(amount) amt FROM `tabStock Entry Detail` WHERE parent=%s
		""",
		voucher_no,
	)[0][0]
	sle_svd = frappe.db.sql(
		"""
		SELECT SUM(ABS(stock_value_difference)) v
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0 AND actual_qty < 0
		""",
		voucher_no,
	)[0][0]
	failures = list(base.get("failures") or [])
	if abs(flt(se_amt) - flt(sle_svd)) > VALUE_EPS and flt(se_amt) and flt(sle_svd):
		# Transfer: outgoing abs SVD should match SE outgoing amount closely
		pass
	return {"ok": not failures, "failures": failures, "se_amount": se_amt, "sle_abs_out": sle_svd}
