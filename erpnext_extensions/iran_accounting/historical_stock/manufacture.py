# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical scan of Manufacture output contract (5.2.0 semantics, no policy change)."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	RATE_EPS,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_RECONSTRUCTABLE,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.scrap_costing import apply_iran_manufacture_output_contract


def scan_manufacture_anomalies(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
	limit=400,
) -> dict:
	conds = ["se.docstatus=1", "se.purpose='Manufacture'"]
	args: list = []
	join = ""
	if company:
		conds.append("se.company=%s")
		args.append(company)
	if voucher:
		conds.append("se.name=%s")
		args.append(voucher)
	if work_order:
		conds.append("se.work_order=%s")
		args.append(work_order)
	if from_date:
		conds.append("se.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("se.posting_date<=%s")
		args.append(to_date)
	if item_code or warehouse:
		join = " JOIN `tabStock Entry Detail` sed ON sed.parent=se.name "
		if item_code:
			conds.append("sed.item_code=%s")
			args.append(item_code)
		if warehouse:
			conds.append("(sed.s_warehouse=%s OR sed.t_warehouse=%s)")
			args.extend([warehouse, warehouse])
	names = frappe.db.sql(
		f"""
		SELECT DISTINCT se.name FROM `tabStock Entry` se
		{join}
		WHERE {" AND ".join(conds)}
		ORDER BY se.posting_date, se.creation
		LIMIT {int(limit)}
		""",
		args,
		pluck=True,
	)
	rows = []
	for name in names:
		preview = preview_manufacture_voucher(name)
		if preview.get("needs_repair"):
			rows.append(preview)
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result({"count": len(rows), "rows": rows, "scanned": len(names)})


def preview_manufacture_voucher(voucher_no: str) -> dict:
	doc = frappe.get_doc("Stock Entry", voucher_no)
	before = _snapshot(doc)
	try:
		apply_iran_manufacture_output_contract(doc)
		after = _snapshot(doc)
	finally:
		_restore(doc, before)
	changed = []
	for b, a in zip(before["rows"], after["rows"], strict=False):
		if abs(flt(b["basic_rate"]) - flt(a["basic_rate"])) > RATE_EPS or abs(
			flt(b["amount"]) - flt(a["amount"])
		) > VALUE_EPS:
			changed.append({"idx": b["idx"], "item": b["item_code"], "before": b, "after": a})
	fg_neg = any(flt(r["amount"]) < 0 or flt(r["basic_rate"]) < 0 for r in before["rows"] if r["is_finished_item"])
	needs = bool(changed) or fg_neg
	# v5.3.0: when the deterministic Iran manufacture contract yields healthy
	# AFTER rates, treat as EXACT even if BEFORE FG was negative. The negative
	# FG is the defect being repaired — not a reason to force MANUAL_REVIEW.
	from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
		manufacture_after_rates_healthy,
	)

	after_healthy = manufacture_after_rates_healthy(changed, fg_negative_before=fg_neg)
	if needs and changed and after_healthy:
		confidence = CONFIDENCE_EXACT
	elif changed and not fg_neg:
		confidence = CONFIDENCE_EXACT
	elif needs:
		confidence = CONFIDENCE_LIKELY
	else:
		confidence = CONFIDENCE_AMBIGUOUS
	if not needs:
		confidence = CONFIDENCE_EXACT
	status = STATUS_RECONSTRUCTABLE if needs and confidence == CONFIDENCE_EXACT else (
		STATUS_MANUAL_REVIEW if needs else "HEALTHY"
	)
	zero_rm = any(
		flt(r["qty"]) and abs(flt(r["basic_rate"])) < RATE_EPS and r["s_warehouse"]
		for r in before["rows"]
	)
	# Phase 5B — input health: poisoned / zero-rate / corrupt transfer inputs
	# force WAITING_UPSTREAM rather than a false EXACT FG from poisoned cost.
	input_health = _manufacture_input_health(doc, before["rows"])
	# v5.3.0: zero RM blocks EXACT only when AFTER rates are NOT healthy.
	# When the native contract already yields healthy AFTER (incl. repairing
	# negative FG), keep EXACT/RECONSTRUCTABLE — zero RM is noted, not a veto.
	if zero_rm and needs and not after_healthy:
		status = STATUS_DEPENDENCY_REPAIR_REQUIRED
		confidence = CONFIDENCE_AMBIGUOUS
	if input_health.get("status") == "poisoned" and needs:
		status = STATUS_DEPENDENCY_REPAIR_REQUIRED
		confidence = CONFIDENCE_AMBIGUOUS
	# Zero RM with healthy AFTER still EXACT, but annotate cause.
	zero_rm_cause = None
	if zero_rm:
		zero_rm_cause = input_health.get("zero_rm_cause") or "zero_basic_rate_on_source_row"

	# Expected target from AFTER contract (authoritative reconstruction).
	fg_after = [r for r in after["rows"] if r.get("is_finished_item")]
	fg_before = [r for r in before["rows"] if r.get("is_finished_item")]
	expected_target_value = sum(flt(r["amount"]) for r in fg_after)
	expected_target_rate = (
		(expected_target_value / sum(flt(r["qty"]) for r in fg_after if flt(r["qty"])))
		if fg_after and sum(flt(r["qty"]) for r in fg_after)
		else 0.0
	)
	current_target_value = sum(flt(r["amount"]) for r in fg_before)
	current_target_rate = (
		(current_target_value / sum(flt(r["qty"]) for r in fg_before if flt(r["qty"])))
		if fg_before and sum(flt(r["qty"]) for r in fg_before)
		else 0.0
	)

	# MATCHED_BUT_CORRUPT: SE FG rate == SLE but both disagree with AFTER contract.
	matched_but_corrupt = False
	if needs and after_healthy and abs(expected_target_rate - current_target_rate) > 1:
		for fr in fg_before:
			sle_rate = _fg_sle_rate(voucher_no, fr.get("item_code"), fr.get("t_warehouse"))
			if sle_rate is not None and abs(flt(sle_rate) - flt(fr["basic_rate"])) <= 1:
				if abs(flt(fr["basic_rate"]) - expected_target_rate) > 1:
					matched_but_corrupt = True
					break

	return {
		"topic": "MANUFACTURE",
		"voucher": voucher_no,
		"purpose": "Manufacture",
		"needs_repair": needs,
		"changed_rows": changed,
		"fg_negative": fg_neg,
		"after_rates_healthy": after_healthy,
		"zero_rm_present": zero_rm,
		"zero_rm_cause": zero_rm_cause,
		"input_health": input_health,
		"input_rows": input_health.get("rows") or [],
		"total_consumed_value": input_health.get("total_consumed_value"),
		"expected_target_value": expected_target_value,
		"expected_target_rate": expected_target_rate,
		"current_target_value": current_target_value,
		"current_target_rate": current_target_rate,
		"difference": expected_target_value - current_target_value,
		"matched_but_corrupt": matched_but_corrupt,
		"confidence": confidence,
		"status": status,
		"eligible": status == STATUS_RECONSTRUCTABLE,
		"source_of_truth": "5.3.0_manufacture_output_contract",
		"patient_zero": (
			{"voucher_no": input_health.get("root_voucher") or voucher_no}
			if needs
			else None
		),
		"dependency_closure": input_health.get("dependencies") or [],
		"planner_note": (
			"WAITING_UPSTREAM — poisoned/unhealthy manufacture inputs"
			if input_health.get("status") == "poisoned" and needs
			else (
				"EXACT via healthy AFTER contract despite fg_negative BEFORE"
				if needs and confidence == CONFIDENCE_EXACT and fg_neg
				else (
					"WAITING zero-RM dependency — AFTER not yet healthy"
					if zero_rm and needs and not after_healthy
					else None
				)
			)
		),
	}


def _fg_sle_rate(voucher_no, item_code, warehouse=None):
	conds = [
		"voucher_type='Stock Entry'",
		"voucher_no=%s",
		"item_code=%s",
		"is_cancelled=0",
		"actual_qty > 0",
	]
	args: list = [voucher_no, item_code]
	if warehouse:
		conds.append("warehouse=%s")
		args.append(warehouse)
	rows = frappe.db.sql(
		f"""
		SELECT incoming_rate, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	if not rows:
		return None
	return flt(rows[0].incoming_rate or rows[0].valuation_rate)


def _manufacture_input_health(doc, before_rows: list) -> dict:
	"""Validate economically relevant manufacture inputs before FG reconstruction."""
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason

	rows_out = []
	deps = []
	total = 0.0
	poisoned = False
	root = None
	zero_rm_cause = None
	posting_dt = f"{doc.posting_date} {doc.posting_time or '00:00:00'}"
	for r in before_rows or []:
		s_wh = r.get("s_warehouse")
		if not s_wh or not flt(r.get("qty")):
			continue
		item = r.get("item_code")
		qty = flt(r.get("qty"))
		basic = flt(r.get("basic_rate"))
		# Authoritative source SLE outgoing on this voucher if present.
		sle = frappe.db.sql(
			"""
			SELECT name, actual_qty, outgoing_rate, stock_value_difference, stock_value,
			       qty_after_transaction, voucher_no, incoming_rate, valuation_rate
			FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s
			  AND warehouse=%s AND is_cancelled=0 AND actual_qty < 0
			ORDER BY posting_datetime, creation
			LIMIT 1
			""",
			(doc.name, item, s_wh),
			as_dict=True,
		)
		auth_value = abs(flt(basic) * qty)
		auth_rate = abs(basic)
		auth_source = "se_basic_rate"
		if sle:
			oq = flt(sle[0].actual_qty)
			svd = flt(sle[0].stock_value_difference)
			if abs(oq) > 0 and abs(svd) > VALUE_EPS:
				auth_rate = abs(svd / oq)
				auth_value = abs(svd)
				auth_source = "outgoing_sle_svd"
			poison = sle_poison_reason(sle[0])
			if poison:
				poisoned = True
				root = root or doc.name
				deps.append(doc.name)
		# Upstream poison on source warehouse before this posting.
		prev = frappe.db.sql(
			"""
			SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
			       stock_value, stock_value_difference, qty_after_transaction
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND posting_datetime < %s
			ORDER BY posting_datetime DESC, creation DESC
			LIMIT 15
			""",
			(item, s_wh, posting_dt),
			as_dict=True,
		)
		for p in prev:
			pr = sle_poison_reason(p)
			if pr:
				poisoned = True
				root = root or p.voucher_no
				deps.append(p.voucher_no)
				break
			if abs(flt(p.qty_after_transaction)) < 1e-9 and abs(flt(p.stock_value)) > 1:
				poisoned = True
				root = root or p.voucher_no
				deps.append(p.voucher_no)
				break
		if abs(basic) < RATE_EPS and abs(auth_rate) < RATE_EPS:
			# Classify zero RM cause.
			if any(abs(flt(p.incoming_rate or p.outgoing_rate or p.valuation_rate)) < RATE_EPS and abs(flt(p.actual_qty)) > 0 for p in (prev or [])):
				zero_rm_cause = zero_rm_cause or "upstream_zero_rate"
				poisoned = True
				root = root or ((prev[0].voucher_no) if prev else doc.name)
			else:
				zero_rm_cause = zero_rm_cause or "legitimate_or_missing_source_rate"
		total += auth_value
		rows_out.append(
			{
				"item": item,
				"warehouse": s_wh,
				"qty": qty,
				"se_basic_rate": basic,
				"authoritative_rate": auth_rate,
				"authoritative_value": auth_value,
				"authoritative_source": auth_source,
			}
		)
	return {
		"status": "poisoned" if poisoned else "healthy",
		"rows": rows_out,
		"total_consumed_value": total,
		"dependencies": sorted(set(deps)),
		"root_voucher": root,
		"zero_rm_cause": zero_rm_cause,
	}


def apply_manufacture_preview_to_doc(doc) -> bool:
	return bool(apply_iran_manufacture_output_contract(doc))


def _snapshot(doc) -> dict:
	rows = []
	for d in doc.get("items") or []:
		rows.append(
			{
				"idx": d.idx,
				"name": d.name,
				"item_code": d.item_code,
				"qty": flt(d.qty),
				"basic_rate": flt(d.basic_rate),
				"basic_amount": flt(d.basic_amount),
				"valuation_rate": flt(d.valuation_rate),
				"amount": flt(d.amount),
				"additional_cost": flt(d.additional_cost),
				"is_finished_item": d.is_finished_item,
				"secondary_item_type": d.secondary_item_type,
				"s_warehouse": d.s_warehouse,
				"t_warehouse": d.t_warehouse,
			}
		)
	return {"rows": rows}


def _restore(doc, snap) -> None:
	by_name = {r["name"]: r for r in snap["rows"]}
	for d in doc.get("items") or []:
		src = by_name.get(d.name)
		if not src:
			continue
		d.basic_rate = src["basic_rate"]
		d.basic_amount = src["basic_amount"]
		d.valuation_rate = src["valuation_rate"]
		d.amount = src["amount"]
		d.additional_cost = src["additional_cost"]
