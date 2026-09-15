# Copyright (c) 2026, ERPNext Extensions contributors
"""Search for hidden inbound before classifying REAL_STOCK_SHORTAGE."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	OPERATOR_DECISION,
	REAL_STOCK_SHORTAGE,
)


def search_hidden_inbound(row: dict, *, cache: dict | None = None) -> dict:
	"""Prove shortage only after exhausting hidden inbound candidates."""
	import frappe

	cache = cache if cache is not None else {}
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse")
	batch = row.get("batch") or row.get("batch_no")
	out_v = row.get("outbound_document") or row.get("out_voucher") or row.get("voucher")
	if not item or not warehouse:
		return {
			"outcome": OPERATOR_DECISION,
			"proven_shortage": False,
			"candidates": [],
			"reason": "Missing item/warehouse for shortage probe",
		}

	candidates = []
	# Later transfers into this warehouse
	later = frappe.db.sql(
		"""
		SELECT sle.voucher_no, sle.actual_qty, sle.posting_datetime, sle.voucher_type, sle.is_cancelled
		FROM `tabStock Ledger Entry` sle
		WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.actual_qty>0
		ORDER BY sle.posting_datetime DESC
		LIMIT 20
		""",
		(item, warehouse),
		as_dict=True,
	)
	for r in later or []:
		candidates.append(
			{
				"kind": "later_inbound",
				"voucher": r.voucher_no,
				"qty": flt(r.actual_qty),
				"posting_datetime": str(r.posting_datetime),
				"voucher_type": r.voucher_type,
				"cancelled": int(r.is_cancelled or 0),
			}
		)

	# Cancelled vouchers that once brought stock
	cancelled = frappe.db.sql(
		"""
		SELECT sle.voucher_no, sle.actual_qty, sle.posting_datetime, sle.voucher_type
		FROM `tabStock Ledger Entry` sle
		WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=1 AND sle.actual_qty>0
		ORDER BY sle.posting_datetime DESC
		LIMIT 10
		""",
		(item, warehouse),
		as_dict=True,
	)
	for r in cancelled or []:
		candidates.append(
			{
				"kind": "cancelled_inbound",
				"voucher": r.voucher_no,
				"qty": flt(r.actual_qty),
				"posting_datetime": str(r.posting_datetime),
				"voucher_type": r.voucher_type,
			}
		)

	# Returns / material receipt on other warehouses for same batch
	if batch:
		returns = frappe.db.sql(
			"""
			SELECT sle.voucher_no, sle.warehouse, sle.actual_qty, sle.posting_datetime, sle.voucher_type
			FROM `tabStock Ledger Entry` sle
			WHERE sle.item_code=%s AND sle.batch_no=%s AND sle.actual_qty>0 AND sle.is_cancelled=0
			  AND sle.warehouse!=%s
			ORDER BY sle.posting_datetime DESC
			LIMIT 10
			""",
			(item, batch, warehouse),
			as_dict=True,
		)
		for r in returns or []:
			candidates.append(
				{
					"kind": "cross_warehouse_inbound",
					"voucher": r.voucher_no,
					"warehouse": r.warehouse,
					"qty": flt(r.actual_qty),
					"posting_datetime": str(r.posting_datetime),
					"voucher_type": r.voucher_type,
				}
			)

	# Opening / reco
	openings = frappe.db.sql(
		"""
		SELECT sle.voucher_no, sle.actual_qty, sle.posting_datetime, sle.voucher_type
		FROM `tabStock Ledger Entry` sle
		WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=0
		  AND sle.voucher_type IN ('Stock Reconciliation','Purchase Receipt')
		ORDER BY sle.posting_datetime ASC
		LIMIT 5
		""",
		(item, warehouse),
		as_dict=True,
	)
	for r in openings or []:
		candidates.append(
			{
				"kind": "opening_or_purchase",
				"voucher": r.voucher_no,
				"qty": flt(r.actual_qty),
				"posting_datetime": str(r.posting_datetime),
				"voucher_type": r.voucher_type,
			}
		)

	# Manufacture outputs of this item
	mfg = frappe.db.sql(
		"""
		SELECT se.name, sed.qty, se.posting_date, se.posting_time
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name=sed.parent
		WHERE se.docstatus=1 AND se.purpose='Manufacture' AND sed.item_code=%s
		  AND sed.t_warehouse=%s
		ORDER BY se.posting_date DESC
		LIMIT 8
		""",
		(item, warehouse),
		as_dict=True,
	)
	for r in mfg or []:
		candidates.append(
			{
				"kind": "manufacture_output",
				"voucher": r.name,
				"qty": flt(r.qty),
				"posting_date": str(r.posting_date),
			}
		)

	actionable = [
		c
		for c in candidates
		if c.get("kind") in ("cancelled_inbound", "cross_warehouse_inbound")
		or (c.get("kind") == "later_inbound" and c.get("voucher") != out_v)
	]

	min_before = flt(row.get("min_qty_before") or row.get("min_before") or 0)
	# Proven shortage: no actionable hidden inbound and optimizer already EXACT shortage
	opt = str(row.get("optimizer_status") or row.get("status") or "")
	if opt == "REAL_STOCK_SHORTAGE" and not actionable:
		return {
			"outcome": REAL_STOCK_SHORTAGE,
			"proven_shortage": True,
			"candidates": candidates[:15],
			"actionable": [],
			"reason": "No hidden inbound found; shortage remains after cancelled/transfer/opening/manufacture search",
			"outbound": out_v,
			"min_before": min_before,
		}
	if actionable:
		return {
			"outcome": OPERATOR_DECISION,
			"proven_shortage": False,
			"candidates": candidates[:15],
			"actionable": actionable[:10],
			"reason": "Hidden inbound candidates found — operator must confirm whether to restore/reorder",
			"outbound": out_v,
			"min_before": min_before,
		}
	return {
		"outcome": REAL_STOCK_SHORTAGE,
		"proven_shortage": True,
		"candidates": candidates[:15],
		"actionable": [],
		"reason": "Shortage probe exhausted with no actionable hidden inbound",
		"outbound": out_v,
		"min_before": min_before,
	}
