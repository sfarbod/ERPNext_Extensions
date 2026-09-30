# Copyright (c) 2026, ERPNext Extensions contributors
"""Poisoned-opening analyzer — no auto-clear without economic authority."""

from __future__ import annotations

from collections import Counter

import frappe
from frappe.utils import flt

TECHNICAL_TOOL_GAP = "TECHNICAL_TOOL_GAP"
MANUAL_BUSINESS_EVIDENCE_REQUIRED = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"
LEGITIMATE = "LEGITIMATE"
PRECISION_DUST = "PRECISION_DUST"
WAITING_UPSTREAM = "WAITING_UPSTREAM"


def analyze_identity_opening(item: str, warehouse: str, *, batch: str | None = None) -> dict:
	"""Earliest SLE → first divergence → terminal; classify poison origin."""
	conds = ["item_code=%s", "warehouse=%s", "is_cancelled=0"]
	args: list = [item, warehouse]
	if batch:
		conds.append("IFNULL(batch_no,'')=%s")
		args.append(batch)
	sles = frappe.db.sql(
		f"""
		SELECT name, posting_datetime, voucher_no, voucher_type, batch_no,
		       ROUND(actual_qty,6) aq, ROUND(qty_after_transaction,6) q,
		       ROUND(incoming_rate,2) ir, ROUND(outgoing_rate,2) ogr,
		       ROUND(valuation_rate,2) vr, ROUND(stock_value,2) sv,
		       ROUND(stock_value_difference,2) svd
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime ASC, creation ASC
		LIMIT 8
		""",
		args,
		as_dict=True,
	)
	terminal = frappe.db.sql(
		f"""
		SELECT name, posting_datetime, voucher_no,
		       ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv,
		       ROUND(valuation_rate,2) vr
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	if not sles:
		return {
			"item": item,
			"warehouse": warehouse,
			"batch": batch,
			"status": MANUAL_BUSINESS_EVIDENCE_REQUIRED,
			"origin": "no_sle",
			"repairable": False,
		}
	e0 = sles[0]
	opening_qty = flt(e0.q)
	opening_sv = flt(e0.sv)
	opening_vr = flt(e0.vr)
	first_later = sles[1] if len(sles) > 1 else None
	term = terminal[0] if terminal else None

	origin = "other"
	status = TECHNICAL_TOOL_GAP
	repairable = False
	authority = None

	# Precision dust: tiny residual value with ~0 qty
	if abs(opening_qty) < 1e-9 and abs(opening_sv) <= 1.0:
		origin = "precision_dust"
		status = PRECISION_DUST
		repairable = False
	elif abs(opening_qty) < 1e-9 and abs(opening_sv) > 1:
		origin = "invalid_opening_value"
		# Stock Reconciliation / Opening may carry authority
		if e0.voucher_type in ("Stock Reconciliation", "Purchase Receipt"):
			authority = e0.voucher_no
			status = MANUAL_BUSINESS_EVIDENCE_REQUIRED
			repairable = False
		else:
			status = TECHNICAL_TOOL_GAP
			repairable = False
	elif abs(opening_qty) > 1e-9 and abs(opening_vr) < 1e-9 and abs(opening_sv) > 1:
		origin = "lost_historical_rate"
		status = TECHNICAL_TOOL_GAP
		repairable = False
	elif abs(opening_qty) > 1e-9 and abs(opening_sv) < 1e-6:
		origin = "legitimate_zero_opening"
		status = LEGITIMATE
		repairable = False
	elif e0.voucher_type == "Stock Reconciliation" and abs(opening_qty) > 1e-9 and abs(opening_vr) > 1e-9:
		origin = "documented_opening_reconciliation"
		status = LEGITIMATE
		authority = e0.voucher_no
		repairable = False
	elif abs(opening_qty) > 1e-9 and abs(opening_vr) > 1e-9 and abs(opening_sv - opening_qty * opening_vr) <= max(
		1.0, abs(opening_sv) * 1e-6
	):
		# Opening SLE is economically coherent. Mid-chain historical I4 with a
		# healthy current tip is not a poisoned-opening Full Repost blocker.
		tip_q = flt(term.q) if term else 0
		tip_sv = flt(term.sv) if term else 0
		tip_vr = flt(term.vr) if term else 0
		tip_healthy = abs(tip_q) > 1e-9 and abs(tip_sv) > 1 and abs(tip_vr) > 1e-9
		tip_clear = abs(tip_q) < 1e-9 and abs(tip_sv) <= 1.0
		if tip_healthy or tip_clear:
			origin = "mid_chain_historical_residue_tip_healthy"
			status = LEGITIMATE
			repairable = False
		elif abs(tip_q) < 1e-9 and abs(tip_sv) > 1:
			origin = "mid_chain_terminal_i4"
			status = TECHNICAL_TOOL_GAP
			repairable = False
		else:
			origin = "mid_chain_unresolved"
			status = WAITING_UPSTREAM
			repairable = False
	else:
		origin = "wrong_source_valuation_or_migration"
		status = TECHNICAL_TOOL_GAP
		repairable = False

	# Why RIV would reproduce poison
	if status in (TECHNICAL_TOOL_GAP, MANUAL_BUSINESS_EVIDENCE_REQUIRED, WAITING_UPSTREAM):
		riv_reproduce = (
			"Native RIV may reapply an inconsistent opening/mid-chain value onto dependents"
		)
	else:
		riv_reproduce = (
			"opening coherent and tip healthy/clear — historical residue must not block Full Repost"
		)

	return {
		"item": item,
		"warehouse": warehouse,
		"batch": batch or getattr(e0, "batch_no", None),
		"earliest_sle": e0.name,
		"earliest_voucher": e0.voucher_no,
		"earliest_voucher_type": e0.voucher_type,
		"opening_qty": opening_qty,
		"opening_stock_value": opening_sv,
		"opening_valuation_rate": opening_vr,
		"first_later_voucher": first_later.voucher_no if first_later else None,
		"first_later_dt": str(first_later.posting_datetime) if first_later else None,
		"terminal": term,
		"origin": origin,
		"status": status,
		"authority": authority,
		"repairable_auto": repairable,
		"riv_reproduce_reason": riv_reproduce,
	}


def analyze_poison_roots(rows: list[dict]) -> dict:
	"""Root-compress I4/poison rows into independent opening identities."""
	roots = {}
	analyzed = []
	for r in rows:
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse")
		if not item or not wh:
			continue
		key = (item, wh)
		if key in roots:
			continue
		info = analyze_identity_opening(item, wh, batch=r.get("batch") or r.get("batch_no"))
		info["source_voucher"] = r.get("voucher") or r.get("voucher_no")
		info["source_reason"] = str(r.get("reason") or r.get("message") or "")[:200]
		roots[key] = info
		analyzed.append(info)
	by_status = Counter(a["status"] for a in analyzed)
	by_origin = Counter(a["origin"] for a in analyzed)
	blocking = [
		a
		for a in analyzed
		if a["status"] in (TECHNICAL_TOOL_GAP, MANUAL_BUSINESS_EVIDENCE_REQUIRED)
	]
	return {
		"raw_input": len(rows),
		"independent_roots": len(analyzed),
		"by_status": dict(by_status),
		"by_origin": dict(by_origin),
		"blocking_n": len(blocking),
		"roots": analyzed,
	}
