# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only helpers to execute known-case mid-anchor RIVs (v5.6.1)."""

from __future__ import annotations

from typing import Any

import frappe

from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	execute_reposting_entry,
)


def run_mid_anchor_13200244() -> dict[str, Any]:
	"""Repost 13200244 / پایکار from validated mid-anchor 2026-08-22 23:40:12."""
	item_code = "13200244"
	warehouse = "انبار پایکار خط تولید اسپاد فارمد"
	prior = frappe.db.get_value(
		"Repost Item Valuation",
		{
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": "2026-08-22",
			"status": "Completed",
			"docstatus": 1,
		},
		"name",
		order_by="creation desc",
	)
	riv = frappe.new_doc("Repost Item Valuation")
	riv.based_on = "Item and Warehouse"
	riv.item_code = item_code
	riv.warehouse = warehouse
	riv.posting_date = "2026-08-22"
	riv.posting_time = "23:40:12"
	riv.allow_negative_stock = 1
	riv.flags.ignore_permissions = True
	riv.insert()
	riv.submit()
	frappe.db.commit()
	execute_reposting_entry(riv.name)
	frappe.db.commit()
	status = frappe.db.get_value("Repost Item Valuation", riv.name, ["status", "error_log"], as_dict=True)
	neg = frappe.db.sql(
		"""
		select count(*) from `tabStock Ledger Entry`
		where is_cancelled=0 and item_code=%s and warehouse=%s and stock_value < -1
		""",
		(item_code, warehouse),
	)[0][0]
	sle_40942 = frappe.db.get_value(
		"Stock Ledger Entry",
		{
			"voucher_no": "MAT-STE-2026-40942",
			"item_code": item_code,
			"warehouse": warehouse,
			"is_cancelled": 0,
		},
		["name", "stock_value", "stock_value_difference", "qty_after_transaction"],
		as_dict=True,
	)
	return {
		"riv": riv.name,
		"status": status.status if status else None,
		"error_sample": (status.error_log or "")[:400] if status else None,
		"neg_sv_remaining": neg,
		"sle_40942": sle_40942,
		"prior_completed": prior,
	}


def inspect_40436() -> dict[str, Any]:
	"""Document current state / blocker for MAT-STE-2026-40436 / 30200012."""
	item_code = "30200012"
	rows = frappe.db.sql(
		"""
		select name, warehouse, posting_date, posting_time, actual_qty,
		       qty_after_transaction, stock_value, stock_value_difference, voucher_no
		from `tabStock Ledger Entry`
		where is_cancelled=0 and item_code=%s
		  and voucher_no in ('MAT-STE-2026-40436', 'MAT-STE-2026-41766', 'MAT-STE-2026-41523', 'MAT-STE-2026-40399')
		order by posting_date, posting_time, creation
		""",
		(item_code,),
		as_dict=True,
	)
	# Historical residual signal: consume-to-zero with large SVD
	residual = frappe.db.sql(
		"""
		select name, voucher_no, warehouse, qty_after_transaction, stock_value,
		       stock_value_difference, posting_date
		from `tabStock Ledger Entry`
		where is_cancelled=0 and item_code=%s
		  and abs(qty_after_transaction) < 0.0001
		  and abs(stock_value_difference) > 1e9
		order by abs(stock_value_difference) desc
		limit 10
		""",
		(item_code,),
		as_dict=True,
	)
	return {
		"item_code": item_code,
		"related_sles": rows,
		"large_zero_qty_svds": residual,
		"blocker": (
			"Mega Manufacture / transfer residual class (~3.3e9 IRR). "
			"Do not invent Stock Adjustment to force campaign completion. "
			"Fail-closed under MANUFACTURE_ABSORB_SOFT_CEILING; requires manual review."
		),
		"recommendation": "MANUAL_REVIEW — controlled investigation of earliest corruption; no arbitrary SA.",
	}
