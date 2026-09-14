# Copyright (c) 2026, ERPNext Extensions contributors
"""Detect zero AND wrong historical rates. Never uses live Bin as truth."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	QTY_EPS,
	RATE_EPS,
	RECONSTRUCTION_PRIORITY,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_RECONSTRUCTABLE,
	STATUS_VALUATION_POISON_DEPENDENCY,
	VALUE_EPS,
	W_STALE_SOURCE,
	W_WRONG_AMOUNT,
	W_WRONG_AVG,
	W_WRONG_BASIC,
	W_WRONG_INCOMING,
	W_WRONG_OUTGOING,
	W_WRONG_SVD,
	W_WRONG_VALUATION,
	W_ZERO_BASIC,
	Z0_LEGITIMATE_ZERO,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row
from erpnext_extensions.iran_accounting.historical_stock.util import g
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D


def pick_reconstruction(sources: dict[str, float], *, corroborating: dict[str, float] | None = None) -> dict:
	"""Choose expected rate from named sources in RECONSTRUCTION_PRIORITY.

	``sources`` maps source name → nonzero rate. Bin is never a valid key.
	EXACT only when two independent sources agree, or transfer_source/implied_svd stands alone.
	"""
	if "bin" in (sources or {}):
		sources = {k: v for k, v in sources.items() if k != "bin"}
	tried = [name for name in RECONSTRUCTION_PRIORITY if abs(flt(sources.get(name))) > RATE_EPS]
	if not tried:
		return {
			"expected": 0.0,
			"source": "manual",
			"confidence": CONFIDENCE_MANUAL,
			"sources_tried": list(sources.keys()),
		}
	primary = tried[0]
	rate = flt(sources[primary])
	others = [n for n in tried[1:] if abs(flt(sources[n]) - rate) <= 1]
	if primary in ("transfer_source", "implied_svd") or others:
		confidence = CONFIDENCE_EXACT
		source = primary if not others else f"{primary}+{others[0]}"
	elif primary == "version":
		confidence = CONFIDENCE_LIKELY
		source = primary
	elif primary in ("previous_healthy_sle", "batch_inward", "manufacture_pool"):
		confidence = CONFIDENCE_LIKELY
		source = primary
	else:
		confidence = CONFIDENCE_AMBIGUOUS
		source = primary
	return {
		"expected": rate,
		"source": source,
		"confidence": confidence,
		"sources_tried": tried,
	}


def classify_rate_flags(*, qty, basic_rate=None, valuation_rate=None, incoming_rate=0, outgoing_rate=0,
                        amount=None, svd=0, avg_rate=0, expected=None, allow_zero=0) -> list[str]:
	flags = []
	q = D(qty)
	if abs(q) <= QTY_EPS:
		return flags
	if allow_zero:
		return flags
	if basic_rate is not None and valuation_rate is not None:
		if abs(flt(basic_rate)) <= RATE_EPS and abs(flt(valuation_rate)) <= RATE_EPS:
			flags.append(W_ZERO_BASIC)
		if abs(flt(basic_rate)) > RATE_EPS and abs(flt(valuation_rate)) <= RATE_EPS:
			flags.append(W_WRONG_VALUATION)
		if amount is not None and abs(flt(amount) - abs(flt(qty)) * abs(flt(basic_rate or valuation_rate))) > VALUE_EPS and abs(flt(basic_rate)) > RATE_EPS:
			flags.append(W_WRONG_AMOUNT)
	implied = abs(D(svd) / q) if q else D(0)
	if q < 0 and abs(D(svd)) > VALUE_EPS and abs(flt(outgoing_rate)) <= RATE_EPS:
		flags.append(W_WRONG_OUTGOING)
	if q > 0 and abs(D(svd)) > VALUE_EPS and abs(flt(incoming_rate)) <= RATE_EPS:
		flags.append(W_WRONG_INCOMING)
	if q < 0 and abs(D(svd)) > VALUE_EPS and abs(flt(outgoing_rate)) > RATE_EPS and abs(implied - D(outgoing_rate)) > 1:
		flags.append(W_WRONG_OUTGOING)
	if q > 0 and abs(D(svd)) > VALUE_EPS and abs(flt(incoming_rate)) > RATE_EPS and abs(implied - D(incoming_rate)) > 1:
		flags.append(W_WRONG_INCOMING)
	if abs(flt(avg_rate)) > RATE_EPS and abs(D(svd)) > VALUE_EPS and abs(implied - D(avg_rate)) > 1:
		flags.append(W_WRONG_AVG)
	if expected is not None and abs(flt(expected)) > RATE_EPS:
		cur = flt(outgoing_rate or incoming_rate or basic_rate or valuation_rate)
		if abs(cur - flt(expected)) > 1:
			flags.append(W_STALE_SOURCE)
			if abs(flt(basic_rate)) > RATE_EPS:
				flags.append(W_WRONG_BASIC)
			if abs(D(svd)) > VALUE_EPS and abs(implied - D(expected)) > 1:
				flags.append(W_WRONG_SVD)
	return sorted(set(flags))


def scan_wrong_rates(company=None, voucher=None, item_code=None, warehouse=None, batch=None,
                     from_date=None, to_date=None, limit=4000) -> dict:
	"""Indexed scan. Read-only. Includes zero and non-zero mismatches."""
	se_rows = _scan_se_flags(company, voucher, item_code, warehouse, from_date, to_date, limit)
	sle_rows = _scan_sle_flags(company, voucher, item_code, warehouse, batch, from_date, to_date, limit)
	classified = []
	cache = {}
	seen = set()
	for raw in se_rows:
		row = classify_zero_row(raw, _cache=cache)
		row["topic"] = "WRONG_RATE"
		row["surface"] = "SE"
		flags = classify_rate_flags(
			qty=row.get("qty"),
			basic_rate=g(raw, "basic_rate"),
			valuation_rate=g(raw, "valuation_rate"),
			amount=g(raw, "amount"),
			expected=row.get("proposed_rate"),
			allow_zero=g(raw, "allow_zero_valuation_rate"),
		)
		row["flags"] = flags
		row["mismatch_class"] = flags[0] if flags else row.get("zero_class")
		row["current"] = row.get("current_rate")
		row["expected"] = row.get("proposed_rate")
		row["difference"] = flt(row.get("proposed_rate")) - flt(row.get("current_rate"))
		row["source"] = row.get("source_of_truth")
		key = ("SE", row.get("voucher_detail") or row.get("voucher"))
		if key not in seen:
			seen.add(key)
			classified.append(row)
	for sle in sle_rows:
		key = ("SLE", sle.name)
		if key in seen:
			continue
		seen.add(key)
		classified.append(_classify_sle_mismatch(sle, cache=cache))
		if len(classified) >= int(limit):
			break
	by_flag = defaultdict(int)
	for r in classified:
		for f in r.get("flags") or [r.get("mismatch_class")]:
			if f:
				by_flag[str(f)] += 1
	return {
		"count": len(classified),
		"rows": classified,
		"by_flag": dict(by_flag),
		"by_confidence": _count(classified, "confidence"),
		"by_status": _count(classified, "status"),
		"exact": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_EXACT),
		"likely": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_LIKELY),
		"ambiguous": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_AMBIGUOUS),
		"manual": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_MANUAL),
		"repairable": sum(1 for r in classified if r.get("eligible")),
	}


def _scan_se_flags(company, voucher, item_code, warehouse, from_date, to_date, limit):
	conds = [
		"se.docstatus=1",
		"ABS(sed.qty) > %s",
		"i.is_stock_item=1",
		"("
		" (ABS(IFNULL(sed.basic_rate,0)) < %s AND ABS(IFNULL(sed.valuation_rate,0)) < %s AND IFNULL(sed.allow_zero_valuation_rate,0)=0)"
		" OR (ABS(IFNULL(sed.basic_rate,0)) > %s AND ABS(IFNULL(sed.valuation_rate,0)) < %s)"
		" OR (ABS(IFNULL(sed.amount,0) - ABS(sed.qty) * IFNULL(sed.basic_rate,0)) > %s AND ABS(IFNULL(sed.basic_rate,0)) > %s)"
		")",
	]
	args: list = [QTY_EPS, RATE_EPS, RATE_EPS, RATE_EPS, RATE_EPS, VALUE_EPS, RATE_EPS]
	if company:
		conds.append("se.company=%s")
		args.append(company)
	if voucher:
		conds.append("se.name=%s")
		args.append(voucher)
	if item_code:
		conds.append("sed.item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("(sed.s_warehouse=%s OR sed.t_warehouse=%s)")
		args.extend([warehouse, warehouse])
	if from_date:
		conds.append("se.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("se.posting_date<=%s")
		args.append(to_date)
	return frappe.db.sql(
		f"""
		SELECT sed.name, sed.idx, sed.parent, se.purpose, se.posting_date, se.posting_time,
		       se.company, se.work_order, se.job_card,
		       sed.item_code, sed.qty, sed.transfer_qty, sed.s_warehouse, sed.t_warehouse,
		       sed.basic_rate, sed.valuation_rate, sed.amount, sed.basic_amount,
		       sed.batch_no, sed.serial_and_batch_bundle, sed.is_finished_item,
		       sed.secondary_item_type, sed.is_scrap_item, sed.allow_zero_valuation_rate
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		JOIN `tabItem` i ON i.name = sed.item_code
		WHERE {" AND ".join(conds)}
		ORDER BY se.posting_date, se.creation, sed.idx
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)


def _scan_sle_flags(company, voucher, item_code, warehouse, batch, from_date, to_date, limit):
	conds = [
		"sle.is_cancelled=0",
		"sle.voucher_type='Stock Entry'",
		"sle.actual_qty <> 0",
		"("
		" (sle.actual_qty < 0 AND ABS(sle.stock_value_difference) > %s AND ABS(IFNULL(sle.outgoing_rate,0)) < %s)"
		" OR (sle.actual_qty > 0 AND ABS(sle.stock_value_difference) > %s AND ABS(IFNULL(sle.incoming_rate,0)) < %s)"
		" OR (sle.actual_qty < 0 AND ABS(sle.stock_value_difference) > %s AND ABS(ABS(sle.stock_value_difference / sle.actual_qty) - IFNULL(sle.outgoing_rate,0)) > 1)"
		" OR (sle.actual_qty > 0 AND ABS(sle.stock_value_difference) > %s AND ABS(ABS(sle.stock_value_difference / sle.actual_qty) - IFNULL(sle.incoming_rate,0)) > 1)"
		")",
	]
	args: list = [VALUE_EPS, RATE_EPS, VALUE_EPS, RATE_EPS, VALUE_EPS, VALUE_EPS]
	if company:
		conds.append("se.company=%s")
		args.append(company)
	if voucher:
		conds.append("sle.voucher_no=%s")
		args.append(voucher)
	if item_code:
		conds.append("sle.item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if from_date:
		conds.append("sle.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("sle.posting_date<=%s")
		args.append(to_date)
	join_sbe = ""
	if batch:
		join_sbe = "JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle"
		conds.append("sbe.batch_no=%s")
		args.append(batch)
	return frappe.db.sql(
		f"""
		SELECT sle.name, sle.voucher_no, sle.item_code, sle.warehouse, sle.actual_qty,
		       sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate,
		       sle.stock_value_difference, sle.stock_value, sle.qty_after_transaction,
		       sle.serial_and_batch_bundle, sle.voucher_detail_no, sle.posting_datetime,
		       sle.posting_date, sle.posting_time, se.purpose, se.work_order, se.company
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		{join_sbe}
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)


def _classify_sle_mismatch(sle, cache=None) -> dict:
	qty = flt(sle.actual_qty)
	svd = flt(sle.stock_value_difference)
	implied = abs(svd / qty) if qty else 0.0
	flags = classify_rate_flags(
		qty=qty,
		incoming_rate=sle.incoming_rate,
		outgoing_rate=sle.outgoing_rate,
		valuation_rate=sle.valuation_rate,
		svd=svd,
	)
	current = flt(sle.outgoing_rate if qty < 0 else sle.incoming_rate)
	use_implied = bool(flags) and abs(implied) > RATE_EPS and abs(current) <= RATE_EPS
	expected = implied if use_implied else current
	confidence = CONFIDENCE_EXACT if use_implied else (CONFIDENCE_LIKELY if flags else CONFIDENCE_AMBIGUOUS)
	eligible = use_implied
	return {
		"topic": "WRONG_RATE",
		"surface": "SLE",
		"voucher": sle.voucher_no,
		"voucher_detail": sle.voucher_detail_no,
		"sle": sle.name,
		"purpose": sle.purpose,
		"item": sle.item_code,
		"warehouse": sle.warehouse,
		"batch": None,
		"sabb": sle.serial_and_batch_bundle,
		"qty": qty,
		"current": current,
		"expected": expected,
		"difference": flt(expected) - current,
		"source": "implied_svd" if use_implied else "scan_flag",
		"source_of_truth": "implied_svd" if use_implied else "scan_flag",
		"confidence": confidence,
		"flags": flags,
		"mismatch_class": flags[0] if flags else W_STALE_SOURCE,
		"status": STATUS_RECONSTRUCTABLE if eligible else STATUS_MANUAL_REVIEW,
		"eligible": eligible,
		"patient_zero": None,
		"work_order": sle.work_order,
		"current_rate": current,
		"proposed_rate": expected,
		"historical_rate": expected,
	}


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)
