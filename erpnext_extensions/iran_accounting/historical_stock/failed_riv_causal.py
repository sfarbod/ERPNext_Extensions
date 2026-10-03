# Copyright (c) 2026, ERPNext Extensions contributors
"""FAILED_RIV_CAUSAL_ANALYZER — why RIV failed; never blind-retry."""

from __future__ import annotations

from collections import Counter

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.next_downtime.failed_riv_buckets import (
	HISTORICAL_ONLY,
	MANUAL_BUSINESS_EVIDENCE_REQUIRED,
	SAFE_TO_RETRY,
	SUPERSEDED,
	TECHNICAL_TOOL_GAP,
	WAITING_UPSTREAM,
	classify_failed_riv_row,
)

RESOLVED_BY_UPSTREAM = "RESOLVED_BY_UPSTREAM"
SAFE_TO_RETRY_AFTER_ROOT_FIX = "SAFE_TO_RETRY_AFTER_ROOT_FIX"


def _tip_state(item: str, warehouse: str) -> dict | None:
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, posting_datetime,
		       ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv,
		       ROUND(valuation_rate,2) vr
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(item, warehouse),
		as_dict=True,
	)
	return rows[0] if rows else None


def _has_active_wr(item: str, warehouse: str) -> bool:
	# Tip poison / zero-rate-with-value is the cheap upstream WR signal.
	tip = _tip_state(item, warehouse)
	if not tip:
		return False
	if abs(flt(tip.q)) > 1e-9 and abs(flt(tip.vr)) < 1e-9 and abs(flt(tip.sv)) > 1:
		return True
	if abs(flt(tip.q)) < 1e-9 and abs(flt(tip.sv)) > 1:
		return True
	return False


def _has_i4(item: str, warehouse: str) -> bool:
	tip = _tip_state(item, warehouse)
	if not tip:
		return False
	return abs(flt(tip.q)) < 1e-9 and abs(flt(tip.sv)) > 1


def analyze_failed_riv_row(row: dict) -> dict:
	"""Classify one Failed RIV into causal status. Read-only."""
	base = classify_failed_riv_row(row)
	item = row.get("item_code") or row.get("item")
	warehouse = row.get("warehouse")
	err = str(row.get("error_class") or row.get("riv_status") or row.get("error_log") or "")
	recon = str(row.get("riv_reconcile_status") or "")

	upstream_wr = bool(item and warehouse and _has_active_wr(item, warehouse))
	upstream_i4 = bool(item and warehouse and _has_i4(item, warehouse))
	tip = _tip_state(item, warehouse) if item and warehouse else None

	status = base
	reason = recon or err[:160]
	if base in (HISTORICAL_ONLY, SUPERSEDED):
		status = base
	elif upstream_i4 or upstream_wr:
		if "VALUATION_INTEGRITY" in err.upper() or base == TECHNICAL_TOOL_GAP:
			status = SAFE_TO_RETRY_AFTER_ROOT_FIX
			reason = "upstream_wr_or_i4_present_riv_unsafe_until_root_fixed"
		else:
			status = WAITING_UPSTREAM
			reason = "upstream_economic_root_unresolved"
	elif base == SAFE_TO_RETRY:
		status = SAFE_TO_RETRY
	elif tip and abs(flt(tip.q)) > 1e-9 and abs(flt(tip.sv)) > 1 and abs(flt(tip.vr)) > 1e-6:
		# tip healthy — failure may already be resolved by upstream repair
		if recon in ("CURRENT_LEDGER_IMPACT",) and "VALUATION_INTEGRITY" in err.upper():
			status = RESOLVED_BY_UPSTREAM
			reason = "tip_healthy_after_upstream; historical failed RIV audit only"
		else:
			status = TECHNICAL_TOOL_GAP
	elif base == MANUAL_BUSINESS_EVIDENCE_REQUIRED:
		status = MANUAL_BUSINESS_EVIDENCE_REQUIRED
	else:
		status = TECHNICAL_TOOL_GAP

	return {
		"riv": row.get("name") or row.get("riv_name"),
		"item": item,
		"warehouse": warehouse,
		"voucher": row.get("voucher_no") or row.get("voucher"),
		"bucket": base,
		"status": status,
		"reason": reason[:220],
		"upstream_wr_signal": upstream_wr,
		"upstream_i4_signal": upstream_i4,
		"tip": tip,
		"error": err[:180],
		"reconcile": recon,
		"safe_to_retry_now": status == SAFE_TO_RETRY,
	}


def analyze_failed_riv_actionable(*, company: str | None = None, limit: int = 400) -> dict:
	"""Root-compress actionable Failed RIV (excludes historical/superseded)."""
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv

	scan = scan_failed_riv(company=company, limit=limit)
	actionable = [
		r
		for r in (scan.get("rows") or [])
		if (r.get("riv_reconcile_status") or "")
		not in ("HISTORICAL_ONLY", "SUPERSEDED_BY_SUCCESSFUL_REPAIR")
	]
	analyzed = [analyze_failed_riv_row(r) for r in actionable]
	by_status = Counter(a["status"] for a in analyzed)
	# causal roots = unique item+warehouse still blocking
	roots = {}
	for a in analyzed:
		if a["status"] in (HISTORICAL_ONLY, SUPERSEDED, RESOLVED_BY_UPSTREAM):
			continue
		key = (a["item"], a["warehouse"])
		roots.setdefault(key, a)
	return {
		"raw_scanned": int(scan.get("raw_count") or scan.get("count") or 0),
		"actionable_n": len(actionable),
		"by_status": dict(by_status),
		"causal_roots_n": len(roots),
		"causal_roots": list(roots.values()),
		"sample": analyzed[:40],
	}
