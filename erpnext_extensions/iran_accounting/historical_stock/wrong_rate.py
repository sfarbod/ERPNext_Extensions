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
	disagree = [n for n in tried[1:] if abs(flt(sources[n]) - rate) > 1]
	if disagree:
		return {
			"expected": rate,
			"source": primary,
			"confidence": CONFIDENCE_AMBIGUOUS,
			"sources_tried": tried,
			"disagree": disagree,
		}
	if others:
		confidence = CONFIDENCE_EXACT
		source = f"{primary}+{others[0]}"
	elif primary in ("transfer_source", "implied_svd"):
		confidence = CONFIDENCE_EXACT
		source = primary
	elif primary == "version":
		confidence = CONFIDENCE_LIKELY
		source = primary
	elif primary in ("previous_healthy_sle", "batch_inward"):
		# Single strong warehouse/batch provenance with no disagree → EXACT.
		confidence = CONFIDENCE_EXACT
		source = primary
	elif primary in ("manufacture_pool",):
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
                     from_date=None, to_date=None, limit=4000, planner_status=None, kpi_bucket=None) -> dict:
	"""Indexed scan. Read-only. Includes zero and non-zero mismatches."""
	se_rows = _scan_se_flags(company, voucher, item_code, warehouse, from_date, to_date, limit)
	sle_rows = _scan_sle_flags(company, voucher, item_code, warehouse, batch, from_date, to_date, limit)
	classified = []
	cache = {}
	seen = set()
	for raw in se_rows:
		row = classify_zero_row(raw, _cache=cache)
		# Rule 1 — legitimate scrap zeros are not Wrong Rate defects.
		if row.get("no_action_required") or row.get("status") == "NO_ACTION_REQUIRED":
			continue
		row["topic"] = "WRONG_RATE"
		row["surface"] = "SE"
		# Phase 5B — authoritative transfer reconstruction (outgoing SLE → target).
		purpose = g(raw, "purpose") or row.get("purpose") or ""
		if purpose in (
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Send to Subcontractor",
			"Material Issue",
			"Material Consumption for Manufacture",
		):
			from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
				apply_transfer_reconstruction_to_row,
			)

			row = apply_transfer_reconstruction_to_row(row, cache=cache)
		# Phase 5B — Manufacture dependency: native contract + input health.
		if purpose == "Manufacture" or row.get("purpose") == "Manufacture":
			from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
				preview_manufacture_voucher,
			)

			voucher = row.get("voucher") or g(raw, "parent")
			if voucher:
				try:
					mfg = preview_manufacture_voucher(voucher)
					row["manufacture_preview"] = mfg
					row["input_health"] = mfg.get("input_health")
					row["matched_but_corrupt"] = bool(mfg.get("matched_but_corrupt")) or bool(
						row.get("matched_but_corrupt")
					)
					if mfg.get("status") == "DEPENDENCY_REPAIR_REQUIRED":
						row["status"] = "DEPENDENCY_REPAIR_REQUIRED"
						row["confidence"] = CONFIDENCE_AMBIGUOUS
						row["eligible"] = False
						row["patient_zero"] = mfg.get("patient_zero")
						row["wrong_reason"] = "MANUAL_MANUFACTURE_DEPENDENCY"
						row["manual_lane"] = "WAITING_UPSTREAM"
						row["message"] = mfg.get("planner_note") or "WAITING_UPSTREAM manufacture inputs"
					elif mfg.get("eligible") and mfg.get("expected_target_rate"):
						# Only apply FG expected rate to finished-item rows.
						if g(raw, "is_finished_item") or row.get("is_finished_item"):
							exp = flt(mfg.get("expected_target_rate"))
							if abs(exp) > RATE_EPS:
								row["proposed_rate"] = exp
								row["expected"] = exp
								row["expected_rate"] = exp
								row["confidence"] = mfg.get("confidence") or CONFIDENCE_EXACT
								row["status"] = "RECONSTRUCTABLE"
								row["eligible"] = True
								row["source_of_truth"] = "5.3.0_manufacture_output_contract"
								row["difference"] = exp - flt(row.get("current_rate") or 0)
								row["message"] = mfg.get("planner_note") or "manufacture EXACT reconstruction"
								if mfg.get("matched_but_corrupt"):
									flags_pre = list(row.get("flags") or [])
									if "MATCHED_BUT_CORRUPT" not in flags_pre:
										flags_pre.insert(0, "MATCHED_BUT_CORRUPT")
									row["flags"] = flags_pre
				except Exception as exc:
					row["manufacture_preview_error"] = str(exc)[:200]
		flags = classify_rate_flags(
			qty=row.get("qty"),
			basic_rate=g(raw, "basic_rate"),
			valuation_rate=g(raw, "valuation_rate"),
			amount=g(raw, "amount"),
			expected=row.get("proposed_rate"),
			allow_zero=g(raw, "allow_zero_valuation_rate"),
		)
		# MATCHED_BUT_CORRUPT: SE basic_rate == SLE incoming/outgoing but both disagree with expected.
		from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
			detect_matched_but_corrupt,
			is_authoritative_healthy_rate,
		)

		sle_rate = _paired_sle_rate(raw)
		matched = detect_matched_but_corrupt(
			se_rate=g(raw, "basic_rate") or g(raw, "valuation_rate"),
			sle_rate=sle_rate if sle_rate is not None else g(raw, "basic_rate"),
			expected_rate=row.get("proposed_rate"),
		)
		if matched and row.get("proposed_rate") and abs(flt(row.get("proposed_rate"))) > RATE_EPS:
			flags = list(flags or [])
			if "MATCHED_BUT_CORRUPT" not in flags:
				flags.insert(0, "MATCHED_BUT_CORRUPT")
			row["matched_but_corrupt"] = True
			row.update({k: matched[k] for k in ("se_rate", "sle_rate", "expected_rate", "difference", "message") if k in matched})
			# Keep actionable even when SE==SLE if expected differs.
			if not is_authoritative_healthy_rate(g(raw, "basic_rate"), allow_zero=False):
				row["status"] = STATUS_RECONSTRUCTABLE if row.get("confidence") == CONFIDENCE_EXACT else STATUS_MANUAL_REVIEW
				row["eligible"] = row.get("confidence") == CONFIDENCE_EXACT
		row["flags"] = flags
		row["mismatch_class"] = flags[0] if flags else row.get("zero_class")
		row["current"] = row.get("current_rate")
		row["expected"] = row.get("proposed_rate")
		row["difference"] = flt(row.get("proposed_rate")) - flt(row.get("current_rate"))
		row["source"] = row.get("source_of_truth")
		from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

		row = attach_rate_analysis(row, raw)
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
	_force_include_patient_zeros(classified, seen, cache, company=company, limit=limit)
	by_flag = defaultdict(int)
	for r in classified:
		for f in r.get("flags") or [r.get("mismatch_class")]:
			if f:
				by_flag[str(f)] += 1
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import (
		wrong_rate_bucket,
		count_wrong_rate_buckets,
	)
	from erpnext_extensions.iran_accounting.historical_stock.scan_filters import filter_rows_by_planner

	stamped = stamp_scan_result(
		{
			"count": len(classified),
			"rows": classified,
			"by_flag": dict(by_flag),
			"by_confidence": _count(classified, "confidence"),
			"by_status": _count(classified, "status"),
			"exact": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_EXACT),
			"likely": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_LIKELY),
			"ambiguous": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_AMBIGUOUS),
			"manual": sum(1 for r in classified if r.get("confidence") == CONFIDENCE_MANUAL),
		}
	)
	for r in stamped.get("rows") or []:
		r["kpi_bucket"] = wrong_rate_bucket(r)
	buckets = count_wrong_rate_buckets(stamped.get("rows") or [])
	stamped["by_kpi_bucket"] = buckets
	# Active problem count excludes RATE_REPAIR_COMPLETE / already-valued.
	stamped["active_count"] = buckets.get("active") or 0
	stamped["complete_count"] = buckets.get("complete") or 0
	rows = stamped.get("rows") or []
	if planner_status or kpi_bucket:
		rows = filter_rows_by_planner(
			rows, planner_status=planner_status, kpi_bucket=kpi_bucket
		)
		stamped["rows"] = rows
		stamped["count"] = len(rows)
		stamped["filtered_by"] = {"planner_status": planner_status, "kpi_bucket": kpi_bucket}
	return stamped


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


def _paired_sle_rate(raw) -> float | None:
	"""SLE rate for the same Stock Entry Detail (incoming or outgoing)."""
	detail = g(raw, "name")
	parent = g(raw, "parent")
	if not detail and not parent:
		return None
	conds = ["sle.is_cancelled=0", "sle.voucher_type='Stock Entry'"]
	args: list = []
	if detail:
		conds.append("sle.voucher_detail_no=%s")
		args.append(detail)
	elif parent:
		conds.append("sle.voucher_no=%s")
		args.append(parent)
		conds.append("sle.item_code=%s")
		args.append(g(raw, "item_code"))
	row = frappe.db.sql(
		f"""
		SELECT sle.actual_qty, sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate
		FROM `tabStock Ledger Entry` sle
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	if not row:
		return None
	sle = row[0]
	qty = flt(sle.actual_qty)
	if qty > 0:
		return flt(sle.incoming_rate or sle.valuation_rate)
	if qty < 0:
		return flt(sle.outgoing_rate or sle.valuation_rate)
	return flt(sle.valuation_rate)


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
	from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

	return attach_rate_analysis(
		{
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
			"rate_source": "implied_svd" if use_implied else "scan_flag",
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
			"current_incoming_rate": flt(sle.incoming_rate),
			"current_outgoing_rate": flt(sle.outgoing_rate),
			"current_valuation_rate": flt(sle.valuation_rate),
			"reconstruction_sources": {"implied_svd": implied} if abs(implied) > RATE_EPS else {},
		},
		sle,
	)


def _force_include_patient_zeros(classified: list, seen: set, cache: dict, *, company=None, limit=4000) -> None:
	"""Pull patient-zero vouchers missing from the scan so WAITING can resolve.

	Missing roots are always appended even when ``limit`` is already reached —
	otherwise WAITING_PATIENT_ZERO chains stay stuck forever on PZ_NOT_IN_SCAN.
	"""
	present = {r.get("voucher") for r in classified if r.get("voucher")}
	missing = []
	for row in classified:
		pz = row.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v and pz_v not in present and pz_v not in missing:
			missing.append(pz_v)
	# Hard cap on extra roots so a pathological graph cannot explode memory.
	extra_budget = max(200, min(800, len(missing)))
	added = 0
	for pz_v in missing:
		if added >= extra_budget:
			break
		raws = _scan_se_flags(company, pz_v, None, None, None, None, 50)
		if not raws:
			raws = _fetch_se_details(pz_v, company=company, limit=50)
		for raw in raws:
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
			from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

			row = attach_rate_analysis(row, raw)
			key = ("SE", row.get("voucher_detail") or row.get("voucher"))
			if key in seen:
				continue
			seen.add(key)
			classified.append(row)
			present.add(row.get("voucher"))
			added += 1
			if added >= extra_budget:
				break
		if pz_v in present:
			continue
		sles = _scan_sle_flags(company, pz_v, None, None, None, None, None, 50)
		for sle in sles:
			key = ("SLE", sle.name)
			if key in seen:
				continue
			seen.add(key)
			classified.append(_classify_sle_mismatch(sle, cache=cache))
			present.add(pz_v)
			added += 1
			if added >= extra_budget:
				break


def _fetch_se_details(voucher, *, company=None, limit=50):
	"""Load SE details for a voucher even when rate flags are clean."""
	if not voucher:
		return []
	conds = ["se.docstatus=1", "se.name=%s", "ABS(sed.qty) > %s", "i.is_stock_item=1"]
	args: list = [voucher, QTY_EPS]
	if company:
		conds.append("se.company=%s")
		args.append(company)
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
		ORDER BY sed.idx
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)
