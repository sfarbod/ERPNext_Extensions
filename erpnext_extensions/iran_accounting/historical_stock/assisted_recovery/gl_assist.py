# Copyright (c) 2026, ERPNext Extensions contributors
"""Assisted reconstruction attempts for G2_MISSING GL vouchers."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	ASSISTED_READY,
	NO_EVIDENCE,
	OPERATOR_DECISION,
)


def assist_g2_missing(voucher: str) -> dict:
	"""Try to uniquely reconstruct expected GL for a G2_MISSING Stock Entry."""
	import frappe
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	if not voucher or not frappe.db.exists("Stock Entry", voucher):
		return {"outcome": NO_EVIDENCE, "reason": "Stock Entry missing", "voucher": voucher}

	se = frappe.get_doc("Stock Entry", voucher)
	# Path 1: native expected map
	try:
		expected = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
	except Exception as exc:
		expected = None
		native_err = str(exc)
	else:
		native_err = None

	if expected:
		return {
			"outcome": "READY",
			"reason": "Native Stock Entry GL map is now non-empty — rebuild eligible",
			"voucher": voucher,
			"n_gl_rows": len(expected),
			"eligible_rebuild": True,
		}

	# Path 2: historical GL pattern from same purpose + company
	hist = frappe.db.sql(
		"""
		SELECT gle.account, gle.cost_center, gle.debit, gle.credit, gle.voucher_no
		FROM `tabGL Entry` gle
		JOIN `tabStock Entry` se ON se.name=gle.voucher_no
		WHERE gle.voucher_type='Stock Entry' AND gle.is_cancelled=0
		  AND se.company=%s AND se.purpose=%s AND se.name!=%s
		ORDER BY gle.creation DESC
		LIMIT 40
		""",
		(se.company, se.purpose, voucher),
		as_dict=True,
	)
	accounts = sorted({r.account for r in hist or [] if r.account})
	# Path 3: warehouse inventory account defaults
	wh_accounts = set()
	for d in se.items:
		for wh in (d.s_warehouse, d.t_warehouse):
			if not wh:
				continue
			acc = frappe.db.get_value("Warehouse", wh, "account")
			if acc:
				wh_accounts.add(acc)

	company_defaults = {}
	try:
		company_defaults = {
			"stock_adjustment_account": frappe.get_cached_value(
				"Company", se.company, "stock_adjustment_account"
			),
			"default_inventory_account": frappe.get_cached_value(
				"Company", se.company, "default_inventory_account"
			),
		}
	except Exception:
		pass

	# Material Transfer with empty map often has no value movement — OPERATOR if both rates zero
	zero_value = all(abs(flt(d.basic_rate)) < 1e-9 and abs(flt(d.amount)) < 1e-9 for d in se.items)
	if se.purpose in ("Material Transfer", "Material Transfer for Manufacture") and zero_value:
		return {
			"outcome": OPERATOR_DECISION,
			"reason": "Material Transfer with zero value — GL may be intentionally empty",
			"voucher": voucher,
			"purpose": se.purpose,
			"historical_accounts": accounts[:10],
			"warehouse_accounts": sorted(wh_accounts),
			"company_defaults": company_defaults,
			"native_error": native_err,
			"choices": [
				{"id": "leave_empty", "label": "Leave GL empty (no value movement)"},
				{"id": "force_rebuild", "label": "Force rebuild from warehouse/company accounts"},
			],
		}

	if wh_accounts or company_defaults.get("default_inventory_account"):
		return {
			"outcome": ASSISTED_READY,
			"reason": "Expected GL map empty but warehouse/company accounts available for assisted rebuild",
			"voucher": voucher,
			"purpose": se.purpose,
			"historical_accounts": accounts[:10],
			"warehouse_accounts": sorted(wh_accounts),
			"company_defaults": company_defaults,
			"confidence": 0.6,
			"native_error": native_err,
			"eligible_rebuild": False,
		}

	return {
		"outcome": NO_EVIDENCE,
		"reason": "Cannot uniquely reconstruct GL — empty native map and no warehouse/company accounts",
		"voucher": voucher,
		"purpose": se.purpose,
		"historical_accounts": accounts[:10],
		"native_error": native_err,
	}
