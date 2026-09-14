# Copyright (c) 2026, ERPNext Extensions contributors
"""Controlled Repost Selected — never global RIV / never all Stock Entries."""

from __future__ import annotations

import frappe
from frappe.utils import nowdate

from erpnext_extensions.iran_accounting.historical_stock import (
	STATUS_SAFE_TO_REPOST,
	STATUS_UNSAFE_TO_REPOST,
)
from erpnext_extensions.iran_accounting.historical_stock.integrity import chain_integrity
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity
from erpnext_extensions.iran_accounting.historical_stock import SLE_HEALTHY


def _resolve_repost_identity(
	item_code=None,
	warehouse=None,
	*,
	batch=None,
	work_order=None,
	voucher=None,
	company=None,
	from_date=None,
	to_date=None,
) -> tuple:
	"""Map a selective scope to one item+warehouse. Company/date alone is not enough."""
	if item_code and warehouse:
		return item_code, warehouse, {"identity_count": 1, "resolved_from": "item_warehouse"}
	scope = {
		"item": item_code,
		"item_code": item_code,
		"warehouse": warehouse,
		"batch": batch,
		"work_order": work_order,
		"voucher": voucher,
		"from_date": from_date,
		"to_date": to_date,
	}
	if not any(scope.get(k) for k in ("item", "item_code", "warehouse", "batch", "work_order", "voucher")):
		return None, None, {"identity_count": 0, "resolved_from": None, "global_riv": False}
	from erpnext_extensions.iran_accounting.historical_stock.selective import resolve_identities

	rows = resolve_identities(scope)
	pairs = sorted({(r.item, r.warehouse) for r in rows if r.get("item") and r.get("warehouse")})
	if len(pairs) != 1:
		return None, None, {
			"identity_count": len(pairs),
			"resolved_from": "scope",
			"global_riv": False,
			"identities": [{"item": a, "warehouse": b} for a, b in pairs[:20]],
		}
	item, wh = pairs[0]
	return item, wh, {"identity_count": 1, "resolved_from": "scope"}


def preview_repost_selected(
	item_code=None,
	warehouse=None,
	*,
	batch=None,
	from_date=None,
	to_date=None,
	work_order=None,
	voucher=None,
	company=None,
) -> dict:
	item_code, warehouse, resolved = _resolve_repost_identity(
		item_code,
		warehouse,
		batch=batch,
		work_order=work_order,
		voucher=voucher,
		company=company,
		from_date=from_date,
		to_date=to_date,
	)
	if not item_code or not warehouse:
		return {
			"item": item_code,
			"warehouse": warehouse,
			"batch": batch,
			"from_date": from_date,
			"to_date": to_date,
			"work_order": work_order,
			"voucher": voucher,
			"company": company,
			"eligible": False,
			"global_riv": False,
			"status": STATUS_UNSAFE_TO_REPOST,
			"reason": "Repost requires a single item + warehouse identity (optionally via batch, work order, or voucher). Never global.",
			**resolved,
		}
	state = classify_identity(item_code, warehouse)
	sle_n = frappe.db.count(
		"Stock Ledger Entry",
		{"item_code": item_code, "warehouse": warehouse, "is_cancelled": 0},
	)
	vouchers = frappe.db.sql(
		"""
		SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND voucher_type='Stock Entry'
		""",
		(item_code, warehouse),
		pluck=True,
	)
	failed = frappe.db.sql(
		"""
		SELECT name FROM `tabRepost Item Valuation`
		WHERE item_code=%s AND warehouse=%s AND status='Failed' AND docstatus=1
		""",
		(item_code, warehouse),
		pluck=True,
	)
	ok = state == SLE_HEALTHY
	gate = chain_integrity(item_code, warehouse, vouchers)
	if not gate.get("ok"):
		ok = False
	return {
		"item": item_code,
		"warehouse": warehouse,
		"batch": batch,
		"from_date": from_date,
		"to_date": to_date,
		"work_order": work_order,
		"voucher": voucher,
		"company": company,
		"global_riv": False,
		"sle_state": state,
		"affected_sle_count": sle_n,
		"affected_vouchers": vouchers,
		"affected_voucher_count": len(vouchers),
		"failed_rivs": failed,
		"integrity": gate,
		"status": STATUS_SAFE_TO_REPOST if ok else STATUS_UNSAFE_TO_REPOST,
		"eligible": ok,
		"estimated_runtime_s": max(1.0, sle_n * 0.02),
	}


def repost_selected(
	item_code=None,
	warehouse=None,
	*,
	from_date=None,
	to_date=None,
	batch=None,
	work_order=None,
	voucher=None,
	company=None,
	dry_run=True,
) -> dict:
	preview = preview_repost_selected(
		item_code,
		warehouse,
		batch=batch,
		from_date=from_date,
		to_date=to_date,
		work_order=work_order,
		voucher=voucher,
		company=company,
	)
	item_code = preview.get("item")
	warehouse = preview.get("warehouse")
	if not preview["eligible"]:
		return {**preview, "written": False, "blocked": True}
	if dry_run:
		return {**preview, "dry_run": True, "written": False}
	company = frappe.db.get_value("Warehouse", warehouse, "company")
	riv = frappe.get_doc(
		{
			"doctype": "Repost Item Valuation",
			"based_on": "Item and Warehouse",
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": from_date or nowdate(),
			"company": company,
			"allow_negative_stock": 0,
			"status": "Queued",
		}
	)
	riv.insert(ignore_permissions=True)
	riv.submit()
	return {**preview, "written": True, "riv_name": riv.name, "status": "QUEUED"}
