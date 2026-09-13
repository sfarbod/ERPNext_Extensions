# Copyright (c) 2026, ERPNext Extensions contributors
"""Map Failed Repost Item Valuation to upstream repair dependencies."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	RIV_SAFE_TO_RETRY,
	RIV_UNSAFE,
	RIV_WAITING_GL,
	RIV_WAITING_RATE,
	RIV_WAITING_SLE,
)
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity
from erpnext_extensions.iran_accounting.historical_stock import (
	G0_HEALTHY,
	G3_UNBALANCED,
	SLE_HEALTHY,
	SLE_POISONED_CHAIN,
	SLE_PATIENT_ZERO_REQUIRED,
)


def scan_failed_riv(limit=400) -> dict:
	docs = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, voucher_no, voucher_type, posting_date,
		       status, error_log, based_on, modified, company
		FROM `tabRepost Item Valuation`
		WHERE status='Failed' AND docstatus=1
		ORDER BY modified DESC
		LIMIT %s
		""",
		int(limit),
		as_dict=True,
	)
	cache = {}
	rows = [classify_failed_riv(d, _cache=cache) for d in docs]
	return {"count": len(rows), "rows": rows, "by_status": _count(rows, "riv_status")}


def classify_failed_riv(doc, _cache=None) -> dict:
	item = doc.item_code
	warehouse = doc.warehouse
	err = doc.error_log or ""
	ck = (item, warehouse)
	if _cache is not None and ck in _cache:
		sle_state = _cache[ck]
	else:
		sle_state = classify_identity(item, warehouse) if item and warehouse else SLE_HEALTHY
		if _cache is not None:
			_cache[ck] = sle_state
	zero_dep = _has_zero_rate_dependency(item, warehouse)
	err_l = err
	gl_dep = G0_HEALTHY
	if "Debit and Credit not equal" in err_l:
		gl_dep = G3_UNBALANCED
	retryable = any(
		token in err_l
		for token in (
			"Deadlock found",
			"Lock wait timeout",
			"QueryTimeout",
			"Lost connection",
			"Unable to acquire",
		)
	)
	if sle_state in (SLE_POISONED_CHAIN, SLE_PATIENT_ZERO_REQUIRED) or zero_dep:
		riv_status = RIV_WAITING_RATE if zero_dep else RIV_WAITING_SLE
	elif gl_dep == G3_UNBALANCED:
		riv_status = RIV_WAITING_GL
	elif "I1" in err_l or "I4" in err_l or "Stock valuation integrity" in err_l:
		riv_status = RIV_UNSAFE
	elif "Get Raw Materials Cost from Consumption Entry" in err_l:
		riv_status = RIV_UNSAFE
	elif "The stock for the item" in err_l or "NegativeStock" in err_l:
		riv_status = RIV_WAITING_SLE
	elif retryable and sle_state == SLE_HEALTHY and not zero_dep:
		riv_status = RIV_SAFE_TO_RETRY
	else:
		riv_status = RIV_UNSAFE
	return {
		"topic": "FAILED_RIV",
		"riv_name": doc.name,
		"item": item,
		"warehouse": warehouse,
		"voucher": doc.voucher_no,
		"posting_date": str(doc.posting_date) if doc.posting_date else None,
		"error_head": err[:240],
		"sle_state": sle_state,
		"zero_rate_dependency": zero_dep,
		"riv_status": riv_status,
		"status": riv_status,
		"eligible": riv_status == RIV_SAFE_TO_RETRY,
		"confidence": CONFIDENCE_EXACT if riv_status == RIV_SAFE_TO_RETRY else "LIKELY",
	}


def retry_failed_riv(riv_name: str, *, dry_run=True) -> dict:
	preview = classify_failed_riv(frappe.get_doc("Repost Item Valuation", riv_name))
	if preview["riv_status"] != RIV_SAFE_TO_RETRY:
		return {**preview, "written": False, "blocked": True}
	if dry_run:
		return {**preview, "dry_run": True, "written": False}
	doc = frappe.get_doc("Repost Item Valuation", riv_name)
	if cint(doc.docstatus) != 1:
		frappe.throw(f"RIV {riv_name} is not submitted")
	if hasattr(doc, "restart_reposting"):
		doc.restart_reposting()
	else:
		doc.db_set("status", "Queued")
	return {**preview, "written": True, "status": "QUEUED"}


def _has_zero_rate_dependency(item, warehouse) -> bool:
	if not item:
		return False
	n = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.docstatus=1 AND sed.item_code=%s
		  AND (sed.s_warehouse=%s OR sed.t_warehouse=%s)
		  AND ABS(sed.qty) > 0.0001
		  AND ABS(IFNULL(sed.basic_rate,0)) < 0.0001
		  AND IFNULL(sed.allow_zero_valuation_rate,0)=0
		""",
		(item, warehouse, warehouse),
	)[0][0]
	return n > 0


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)
