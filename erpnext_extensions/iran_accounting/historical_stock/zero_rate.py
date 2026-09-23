# Copyright (c) 2026, ERPNext Extensions contributors
"""Scan and reconstruct lost / unexpected zero Stock Entry rates."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	NO_ACTION_REQUIRED,
	AUTHORITATIVE_SOURCE_EXISTS_BUT_RATE_IS_ZERO,
	NO_AUTHORITATIVE_SOURCE,
	QTY_EPS,
	RATE_EPS,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_MATERIAL_RECEIPT_USER_REVIEW,
	STATUS_RATE_REBUILD_COMPLETE,
	STATUS_RECONSTRUCTABLE,
	STATUS_VALUATION_POISON_DEPENDENCY,
	VALUE_EPS,
	Z0_LEGITIMATE_SCRAP_ZERO,
	Z0_LEGITIMATE_ZERO,
	Z1_HISTORICAL_RATE_LOST,
	Z3_MISSING_INCOMING_VALUATION,
	Z4_BATCH_SABB_LOOKUP_ZERO,
	Z6_UNKNOWN,
	Z_MATERIAL_RECEIPT_USER_REVIEW,
	ZERO_REASON_LEGITIMATE_SCRAP,
	ZERO_REASON_MANUAL,
	ZERO_REASON_MANUFACTURE_DEP,
	ZERO_REASON_MATERIAL_RECEIPT_USER,
	ZERO_REASON_MISSING_SOURCE,
	ZERO_REASON_NO_AUTHORITATIVE_SOURCE,
	ZERO_REASON_TRUE_CORRUPTION,
	ZERO_REASON_UPSTREAM_POISONED,
)
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero
from erpnext_extensions.iran_accounting.historical_stock.transaction_semantics import (
	NO_INVENT_RATE,
	RECONSTRUCT_FROM_SOURCE,
	RECONSTRUCT_MANUFACTURE,
	RECONSTRUCT_REPACK,
	material_receipt_user_message,
	may_auto_propose_rate,
	purpose_semantics,
)
from erpnext_extensions.iran_accounting.historical_stock.util import (
	g,
	last_nonzero_from_version,
	parse_version_blob,
	version_row_rates,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	_issued_rate_for_component,
	is_product_reject,
	is_scrap_row,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason

TRANSFER_PURPOSES = {
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
	"Material Issue",
	"Material Consumption for Manufacture",
	"Repack",
}


def _is_incoming(row) -> bool:
	return bool(g(row, "t_warehouse")) and not g(row, "s_warehouse")


def scan_zero_rate_rows(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
	repair_class=None,
	planner_status=None,
	patient_zero=None,
	limit=8000,
) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.scan_filters import (
		append_stock_entry_scope,
		filter_rows_by_planner,
		normalize_scope,
	)

	scope = normalize_scope(
		company=company,
		voucher=voucher,
		item_code=item_code,
		warehouse=warehouse,
		batch=batch,
		serial_and_batch_bundle=serial_and_batch_bundle,
		work_order=work_order,
		from_date=from_date,
		to_date=to_date,
		repair_class=repair_class,
		planner_status=planner_status,
		patient_zero=patient_zero,
	)
	conds = [
		"se.docstatus=1",
		"ABS(sed.qty) > %s",
		"ABS(IFNULL(sed.basic_rate,0)) < %s",
		"ABS(IFNULL(sed.valuation_rate,0)) < %s",
		"IFNULL(sed.allow_zero_valuation_rate,0)=0",
		"i.is_stock_item=1",
	]
	args: list = [QTY_EPS, RATE_EPS, RATE_EPS]
	append_stock_entry_scope(conds, args, scope)
	rows = frappe.db.sql(
		f"""
		SELECT sed.name, sed.idx, sed.parent, se.purpose, se.posting_date, se.posting_time,
		       se.company, se.work_order, se.job_card, se.modified se_modified,
		       sed.item_code, sed.qty, sed.transfer_qty, sed.s_warehouse, sed.t_warehouse,
		       sed.basic_rate, sed.valuation_rate, sed.amount, sed.basic_amount,
		       sed.batch_no, sed.serial_and_batch_bundle, sed.is_finished_item,
		       sed.secondary_item_type, sed.is_scrap_item, sed.allow_zero_valuation_rate,
		       sed.modified
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
	out = []
	cache = {}
	for row in rows:
		out.append(classify_zero_row(row, _cache=cache))
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	stamped = stamp_scan_result(
		{
			"count": len(out),
			"rows": out,
			"by_class": _count(out, "zero_class"),
			"by_confidence": _count(out, "confidence"),
			"by_status": _count(out, "status"),
			"by_zero_reason": _count(out, "zero_reason"),
		}
	)
	stamped["rows"] = filter_rows_by_planner(
		stamped["rows"],
		repair_class=scope.get("repair_class"),
		planner_status=scope.get("planner_status"),
		patient_zero=scope.get("patient_zero"),
	)
	stamped["count"] = len(stamped["rows"])
	# KPI semantics (v5.3.0 purpose-first): RAW vs Actionable vs Material Receipt user review.
	# Scrap/Reject warehouse alone is NOT a no-action exemption.
	raw_rows = stamped["rows"]
	user_review = [
		r
		for r in raw_rows
		if r.get("status") == STATUS_MATERIAL_RECEIPT_USER_REVIEW
		or r.get("zero_class") == Z_MATERIAL_RECEIPT_USER_REVIEW
		or r.get("kpi_bucket") == "MATERIAL_RECEIPT_ZERO_USER_REVIEW"
	]
	no_action = [
		r
		for r in raw_rows
		if r.get("no_action_required")
		or r.get("status") in (NO_ACTION_REQUIRED, Z0_LEGITIMATE_ZERO)
		or r.get("zero_class") == Z0_LEGITIMATE_ZERO
	]
	reconstructable = [
		r
		for r in raw_rows
		if (
			r.get("eligible")
			or r.get("status") == STATUS_RECONSTRUCTABLE
			or r.get("kpi_bucket") == "ZERO_RATE_RECONSTRUCTABLE"
		)
		and r not in user_review
		and r not in no_action
	]
	waiting = [
		r
		for r in raw_rows
		if r.get("status")
		in (STATUS_DEPENDENCY_REPAIR_REQUIRED, STATUS_VALUATION_POISON_DEPENDENCY)
		or "WAITING" in str(r.get("planner_status") or "")
	]
	# Actionable = auto-repair candidates (not user-review, not no-action).
	actionable = [
		r
		for r in raw_rows
		if r not in no_action
		and r not in user_review
		and (r.get("eligible") or r.get("actionable", True))
		and r.get("status")
		not in (STATUS_MATERIAL_RECEIPT_USER_REVIEW, STATUS_RATE_REBUILD_COMPLETE)
	]
	stamped["raw_count"] = len(raw_rows)
	stamped["no_action_required_count"] = len(no_action)
	# Legacy field kept for dashboard compatibility — always 0 under purpose-first rules.
	stamped["legitimate_scrap_zero_count"] = 0
	stamped["material_receipt_user_review_count"] = len(user_review)
	stamped["reconstructable_count"] = len(reconstructable)
	stamped["waiting_upstream_count"] = len(waiting)
	stamped["actionable_count"] = len(actionable)
	stamped["by_kpi_bucket"] = _count(raw_rows, "kpi_bucket")
	stamped["by_purpose"] = _count(raw_rows, "purpose")
	stamped["true_zero_corruption_count"] = sum(
		1
		for r in actionable
		if (r.get("zero_reason") or "")
		in (
			"TRUE_ZERO_RATE_CORRUPTION",
			"MISSING_SOURCE_RATE",
			"AUTHORITATIVE_SOURCE_EXISTS_BUT_RATE_IS_ZERO",
		)
		or r.get("eligible")
		or r.get("status") == STATUS_RECONSTRUCTABLE
	)
	return stamped


def classify_zero_row(row, _cache=None) -> dict:
	purpose = g(row, "purpose") or ""
	item = g(row, "item_code")
	qty = flt(g(row, "qty") or g(row, "transfer_qty"))
	warehouse = g(row, "s_warehouse") or g(row, "t_warehouse")
	batch = g(row, "batch_no")
	parent = g(row, "parent")
	detail = g(row, "name")
	sem = purpose_semantics(purpose)

	# Contextual scrap/reject warehouse flag only — NEVER primary NO_ACTION.
	from erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse import (
		is_scrap_reject_waste_warehouse,
		scrap_valuation_role,
	)

	scrap_wh_context = bool(
		is_scrap_reject_waste_warehouse(g(row, "t_warehouse"), company=g(row, "company"))
		or is_scrap_reject_waste_warehouse(g(row, "s_warehouse"), company=g(row, "company"))
	)
	scrap_role = scrap_valuation_role(row) if scrap_wh_context or is_scrap_row(row) else None

	if (
		g(row, "allow_zero_valuation_rate")
		or purpose == "Stock Reconciliation"
		or g(row, "voucher_type") == "Stock Reconciliation"
	):
		from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

		return attach_rate_analysis(
			{
				"topic": "ZERO_RATE",
				"voucher": parent,
				"voucher_detail": detail,
				"idx": g(row, "idx"),
				"purpose": purpose,
				"item": item,
				"warehouse": warehouse,
				"s_warehouse": g(row, "s_warehouse"),
				"t_warehouse": g(row, "t_warehouse"),
				"batch": batch,
				"sabb": g(row, "serial_and_batch_bundle"),
				"qty": qty,
				"current_rate": flt(g(row, "basic_rate")),
				"current_amount": flt(g(row, "amount")),
				"historical_rate": 0.0,
				"proposed_rate": 0.0,
				"proposed_amount": 0.0,
				"source_of_truth": "allow_zero_valuation_rate"
				if g(row, "allow_zero_valuation_rate")
				else "document_authoritative",
				"confidence": CONFIDENCE_EXACT,
				"zero_class": Z0_LEGITIMATE_ZERO,
				"zero_reason": Z0_LEGITIMATE_ZERO,
				"status": NO_ACTION_REQUIRED if g(row, "allow_zero_valuation_rate") else Z0_LEGITIMATE_ZERO,
				"actionable": False,
				"patient_zero": None,
				"eligible": False,
				"work_order": g(row, "work_order"),
				"job_card": g(row, "job_card"),
				"is_finished_item": g(row, "is_finished_item"),
				"secondary_item_type": g(row, "secondary_item_type"),
				"reconstruction_sources": {},
				"rate_source": "document_authoritative",
				"no_action_required": True,
				"kpi_bucket": "NO_ACTION",
				"purpose_policy": sem.get("policy"),
				"scrap_warehouse_context": scrap_wh_context,
				"scrap_valuation_role": scrap_role,
			},
			row,
		)

	version_rate, version_amount = _version_rate(parent, detail)
	batch_rate = _batch_inward_rate(item, batch, warehouse)
	prev_sle_rate = _previous_healthy_sle_rate(item, warehouse, g(row, "posting_date"), g(row, "posting_time"))
	# Material Issue: valuation identity is the source warehouse chain.
	if purpose == "Material Issue" and g(row, "s_warehouse"):
		issue_prev = _previous_healthy_sle_rate(
			item, g(row, "s_warehouse"), g(row, "posting_date"), g(row, "posting_time")
		)
		if abs(issue_prev) > RATE_EPS:
			prev_sle_rate = issue_prev
	issued = 0.0
	if purpose == "Manufacture" and (is_scrap_row(row) or g(row, "secondary_item_type") == "Scrap"):
		doc = frappe.get_doc("Stock Entry", parent)
		issued = flt(_issued_rate_for_component(doc, row))
	repack_rate = 0.0
	if purpose == "Repack" and (sem.get("policy") == RECONSTRUCT_REPACK or True):
		repack_rate = _repack_allocated_rate(row)

	current_basic = flt(g(row, "basic_rate"))
	if abs(current_basic) > RATE_EPS:
		from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
			is_authoritative_healthy_rate,
			rate_integrity_reason,
			reclassify_false_complete_row,
		)
		from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

		complete_row = {
			"topic": "ZERO_RATE",
			"voucher": parent,
			"voucher_detail": detail,
			"idx": g(row, "idx"),
			"purpose": purpose,
			"item": item,
			"warehouse": warehouse,
			"s_warehouse": g(row, "s_warehouse"),
			"t_warehouse": g(row, "t_warehouse"),
			"batch": batch,
			"sabb": g(row, "serial_and_batch_bundle"),
			"qty": qty,
			"current_rate": current_basic,
			"current_amount": flt(g(row, "amount")),
			"historical_rate": current_basic,
			"proposed_rate": current_basic,
			"proposed_amount": current_basic * qty,
			"source_of_truth": "already_valued",
			"confidence": CONFIDENCE_EXACT,
			"zero_class": Z0_LEGITIMATE_ZERO,
			"status": STATUS_RATE_REBUILD_COMPLETE,
			"patient_zero": None,
			"eligible": False,
			"work_order": g(row, "work_order"),
			"job_card": g(row, "job_card"),
			"is_finished_item": g(row, "is_finished_item"),
			"secondary_item_type": g(row, "secondary_item_type"),
			"reconstruction_sources": {},
			"rate_source": "already_valued",
			"allow_zero_valuation_rate": g(row, "allow_zero_valuation_rate"),
			"kpi_bucket": "NO_ACTION",
			"purpose_policy": sem.get("policy"),
			"scrap_warehouse_context": scrap_wh_context,
			"scrap_valuation_role": scrap_role,
		}
		if not is_authoritative_healthy_rate(current_basic, allow_zero=False, allow_negative=False):
			complete_row = reclassify_false_complete_row(complete_row)
			complete_row["zero_class"] = Z6_UNKNOWN
			complete_row["integrity_reason"] = rate_integrity_reason(current_basic)
		return attach_rate_analysis(complete_row, row)

	zero_class = Z6_UNKNOWN
	proposed = 0.0
	source = None
	confidence = CONFIDENCE_AMBIGUOUS
	transfer_sle = _source_transfer_sle_rate(row)

	if abs(version_rate) > RATE_EPS and abs(batch_rate) > RATE_EPS and abs(version_rate - batch_rate) <= 1:
		proposed, source, confidence = version_rate, "version+batch_inward", CONFIDENCE_EXACT
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif abs(version_rate) > RATE_EPS and abs(transfer_sle) > RATE_EPS and abs(version_rate - transfer_sle) <= 1:
		proposed, source, confidence = version_rate, "version+source_transfer_sle", CONFIDENCE_EXACT
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif abs(version_rate) > RATE_EPS and abs(prev_sle_rate) > RATE_EPS and abs(version_rate - prev_sle_rate) <= 1:
		proposed, source, confidence = version_rate, "version+previous_healthy_sle", CONFIDENCE_EXACT
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif abs(version_rate) > RATE_EPS and abs(batch_rate) > RATE_EPS:
		proposed, source, confidence = version_rate, "version_vs_batch_mismatch", CONFIDENCE_AMBIGUOUS
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif abs(version_rate) > RATE_EPS:
		proposed, source, confidence = version_rate, "version", CONFIDENCE_LIKELY
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif abs(transfer_sle) > RATE_EPS:
		proposed, source, confidence = transfer_sle, "source_transfer_sle", CONFIDENCE_EXACT
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif purpose == "Repack" and abs(repack_rate) > RATE_EPS:
		proposed, source, confidence = repack_rate, "repack_allocated", CONFIDENCE_EXACT
		zero_class = Z1_HISTORICAL_RATE_LOST
	elif purpose == "Manufacture" and issued > RATE_EPS and is_scrap_row(row):
		fg = _finished_item(parent)
		if is_product_reject(row, fg):
			proposed, source, confidence = 0.0, "product_reject_pool", CONFIDENCE_LIKELY
			zero_class = Z6_UNKNOWN
		else:
			proposed, source, confidence = issued, "same_voucher_issued_rate", CONFIDENCE_EXACT
			zero_class = Z1_HISTORICAL_RATE_LOST
	elif abs(batch_rate) > RATE_EPS:
		if abs(prev_sle_rate) > RATE_EPS and abs(batch_rate - prev_sle_rate) > 1:
			proposed, source, confidence = batch_rate, "batch_vs_previous_mismatch", CONFIDENCE_AMBIGUOUS
			zero_class = Z4_BATCH_SABB_LOOKUP_ZERO
		else:
			proposed, source, confidence = batch_rate, "batch_inward", CONFIDENCE_EXACT
			zero_class = Z4_BATCH_SABB_LOOKUP_ZERO
	elif abs(prev_sle_rate) > RATE_EPS:
		proposed, source, confidence = prev_sle_rate, "previous_healthy_sle", CONFIDENCE_EXACT
		zero_class = Z3_MISSING_INCOMING_VALUATION
	else:
		zero_class = Z3_MISSING_INCOMING_VALUATION
		confidence = CONFIDENCE_AMBIGUOUS

	# Purpose-first: Material Receipt must not invent rates from warehouse MA / batch alone.
	if sem.get("policy") == NO_INVENT_RATE:
		if not (source and may_auto_propose_rate(purpose, source) and abs(proposed) > RATE_EPS):
			from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

			return attach_rate_analysis(
				{
					"topic": "ZERO_RATE",
					"voucher": parent,
					"voucher_detail": detail,
					"idx": g(row, "idx"),
					"purpose": purpose,
					"item": item,
					"warehouse": warehouse,
					"s_warehouse": g(row, "s_warehouse"),
					"t_warehouse": g(row, "t_warehouse"),
					"batch": batch,
					"sabb": g(row, "serial_and_batch_bundle"),
					"qty": qty,
					"current_rate": flt(g(row, "basic_rate")),
					"current_amount": flt(g(row, "amount")),
					"historical_rate": 0.0,
					"proposed_rate": 0.0,
					"proposed_amount": 0.0,
					"source_of_truth": NO_AUTHORITATIVE_SOURCE,
					"confidence": CONFIDENCE_MANUAL,
					"zero_class": Z_MATERIAL_RECEIPT_USER_REVIEW,
					"zero_reason": ZERO_REASON_MATERIAL_RECEIPT_USER,
					"status": STATUS_MATERIAL_RECEIPT_USER_REVIEW,
					"planner_status": STATUS_MATERIAL_RECEIPT_USER_REVIEW,
					"actionable": False,
					"auto_repairable": False,
					"user_action_required": True,
					"patient_zero": None,
					"eligible": False,
					"work_order": g(row, "work_order"),
					"job_card": g(row, "job_card"),
					"is_finished_item": g(row, "is_finished_item"),
					"secondary_item_type": g(row, "secondary_item_type"),
					"posting_date": g(row, "posting_date"),
					"posting_time": g(row, "posting_time"),
					"reconstruction_sources": {
						k: v
						for k, v in {
							"version": version_rate,
							"batch_inward_not_used": batch_rate,
							"previous_healthy_sle_not_used": prev_sle_rate,
						}.items()
						if abs(flt(v)) > RATE_EPS
					},
					"rate_source": NO_AUTHORITATIVE_SOURCE,
					"message": material_receipt_user_message(),
					"recommended_user_action": material_receipt_user_message(),
					"kpi_bucket": "MATERIAL_RECEIPT_ZERO_USER_REVIEW",
					"purpose_policy": sem.get("policy"),
					"source_kind": NO_AUTHORITATIVE_SOURCE,
					"scrap_warehouse_context": scrap_wh_context,
					"scrap_valuation_role": scrap_role,
					"company": g(row, "company"),
				},
				row,
			)

	if source and not may_auto_propose_rate(purpose, source):
		proposed, source, confidence = 0.0, None, CONFIDENCE_AMBIGUOUS
		zero_class = Z3_MISSING_INCOMING_VALUATION

	as_of = _row_posting_datetime(row)
	patient = _patient_for_identity(item, warehouse, batch, cache=_cache, as_of=as_of)
	if (
		patient
		and patient.get("voucher_no")
		and patient["voucher_no"] != parent
		and as_of
		and patient.get("posting_datetime")
		and str(patient["posting_datetime"]) > str(as_of)
	):
		patient = None
	status = STATUS_MANUAL_REVIEW
	zero_reason = ZERO_REASON_MANUAL
	kpi_bucket = "ZERO_RATE_BLOCKED"
	if zero_class == Z0_LEGITIMATE_ZERO:
		status = Z0_LEGITIMATE_ZERO
		zero_reason = Z0_LEGITIMATE_ZERO
		kpi_bucket = "NO_ACTION"
	elif patient and patient.get("voucher_no") and patient["voucher_no"] != parent:
		status = STATUS_DEPENDENCY_REPAIR_REQUIRED
		zero_reason = ZERO_REASON_MANUFACTURE_DEP if purpose == "Manufacture" else ZERO_REASON_MISSING_SOURCE
		kpi_bucket = "ZERO_RATE_WAITING_UPSTREAM"
	elif _identity_poisoned(item, warehouse, cache=_cache):
		status = STATUS_VALUATION_POISON_DEPENDENCY
		zero_reason = ZERO_REASON_UPSTREAM_POISONED
		kpi_bucket = "ZERO_RATE_WAITING_UPSTREAM"
	elif confidence == CONFIDENCE_EXACT and proposed > RATE_EPS:
		status = STATUS_RECONSTRUCTABLE
		zero_reason = AUTHORITATIVE_SOURCE_EXISTS_BUT_RATE_IS_ZERO
		kpi_bucket = "ZERO_RATE_RECONSTRUCTABLE"
	elif confidence == CONFIDENCE_LIKELY:
		status = STATUS_MANUAL_REVIEW
		zero_reason = ZERO_REASON_MISSING_SOURCE if zero_class == Z3_MISSING_INCOMING_VALUATION else ZERO_REASON_MANUAL
		kpi_bucket = "ZERO_RATE_TOOL_LIMIT"
	else:
		status = STATUS_MANUAL_REVIEW
		zero_reason = (
			ZERO_REASON_NO_AUTHORITATIVE_SOURCE
			if zero_class == Z3_MISSING_INCOMING_VALUATION and not source
			else ZERO_REASON_TRUE_CORRUPTION
			if zero_class == Z1_HISTORICAL_RATE_LOST
			else ZERO_REASON_MANUAL
		)
		kpi_bucket = "ZERO_RATE_BLOCKED" if not source else "ZERO_RATE_TOOL_LIMIT"

	amount = flt(proposed) * qty
	sources = {}
	if abs(version_rate) > RATE_EPS:
		sources["version"] = version_rate
	if abs(prev_sle_rate) > RATE_EPS:
		sources["previous_healthy_sle"] = prev_sle_rate
	if abs(batch_rate) > RATE_EPS:
		sources["batch_inward"] = batch_rate
	if abs(transfer_sle) > RATE_EPS:
		sources["transfer_source"] = transfer_sle
	if abs(issued) > RATE_EPS:
		sources["manufacture_pool"] = issued
	if abs(repack_rate) > RATE_EPS:
		sources["repack_allocated"] = repack_rate
	from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

	result = attach_rate_analysis(
		{
			"topic": "ZERO_RATE",
			"voucher": parent,
			"voucher_detail": detail,
			"idx": g(row, "idx"),
			"purpose": purpose,
			"item": item,
			"warehouse": warehouse,
			"s_warehouse": g(row, "s_warehouse"),
			"t_warehouse": g(row, "t_warehouse"),
			"batch": batch,
			"sabb": g(row, "serial_and_batch_bundle"),
			"qty": qty,
			"current_rate": flt(g(row, "basic_rate")),
			"current_amount": flt(g(row, "amount")),
			"historical_rate": version_rate or batch_rate or prev_sle_rate,
			"proposed_rate": proposed,
			"proposed_amount": amount,
			"source_of_truth": source,
			"confidence": confidence,
			"zero_class": zero_class,
			"zero_reason": zero_reason,
			"status": status,
			"actionable": status
			not in (
				Z0_LEGITIMATE_ZERO,
				NO_ACTION_REQUIRED,
				STATUS_RATE_REBUILD_COMPLETE,
				STATUS_MATERIAL_RECEIPT_USER_REVIEW,
			),
			"patient_zero": patient,
			"eligible": status == STATUS_RECONSTRUCTABLE and confidence == CONFIDENCE_EXACT,
			"work_order": g(row, "work_order"),
			"job_card": g(row, "job_card"),
			"is_finished_item": g(row, "is_finished_item"),
			"secondary_item_type": g(row, "secondary_item_type"),
			"posting_date": g(row, "posting_date"),
			"posting_time": g(row, "posting_time"),
			"reconstruction_sources": sources,
			"rate_source": source,
			"kpi_bucket": kpi_bucket,
			"purpose_policy": sem.get("policy"),
			"source_kind": (
				AUTHORITATIVE_SOURCE_EXISTS_BUT_RATE_IS_ZERO
				if source and abs(proposed) > RATE_EPS
				else NO_AUTHORITATIVE_SOURCE
			),
			"scrap_warehouse_context": scrap_wh_context,
			"scrap_valuation_role": scrap_role,
			"company": g(row, "company"),
		},
		row,
	)
	# Phase 5B: authoritative transfer reconstruction upgrades MANUAL_TRANSFER
	# and never trusts target incoming SLE as source of truth.
	if purpose in TRANSFER_PURPOSES or purpose in ("Material Issue", "Material Consumption for Manufacture"):
		from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
			apply_transfer_reconstruction_to_row,
		)

		result = apply_transfer_reconstruction_to_row(result, cache=_cache)
	return result


def preview_zero_row(row: dict) -> dict:
	return {**row, "dry_run": True}


def _version_rate(voucher, detail) -> tuple[float, float]:
	versions = frappe.db.sql(
		"""
		SELECT data FROM `tabVersion`
		WHERE ref_doctype='Stock Entry' AND docname=%s
		ORDER BY creation
		""",
		voucher,
		as_dict=True,
	)
	best_rate = 0.0
	best_amount = 0.0
	for ver in versions:
		mapped = version_row_rates(parse_version_blob(ver.data))
		changes = mapped.get(detail) or {}
		rate = last_nonzero_from_version(changes)
		if rate > RATE_EPS:
			best_rate = rate
		amt = changes.get("amount") or changes.get("basic_amount")
		if amt:
			old, new = amt
			cand = old if abs(old) > RATE_EPS else new
			if abs(cand) > RATE_EPS:
				best_amount = cand
	return best_rate, best_amount


def _batch_inward_rate(item, batch, warehouse=None) -> float:
	if not batch:
		return 0.0
	rows = frappe.db.sql(
		"""
		SELECT sle.incoming_rate, sbe.incoming_rate sbe_rate, sle.warehouse
		FROM `tabSerial and Batch Entry` sbe
		JOIN `tabSerial and Batch Bundle` sabb ON sabb.name = sbe.parent
		JOIN `tabStock Ledger Entry` sle
			ON sle.serial_and_batch_bundle = sabb.name AND sle.is_cancelled=0
		WHERE sbe.batch_no=%s AND sle.item_code=%s AND sle.actual_qty > 0
		  AND ABS(IFNULL(sle.incoming_rate,0)) > %s
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT 8
		""",
		(batch, item, RATE_EPS),
		as_dict=True,
	)
	if not rows:
		rows = frappe.db.sql(
			"""
			SELECT incoming_rate, warehouse
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND batch_no=%s AND is_cancelled=0 AND actual_qty>0
			  AND ABS(IFNULL(incoming_rate,0)) > %s
			ORDER BY posting_datetime, creation
			LIMIT 5
			""",
			(item, batch, RATE_EPS),
			as_dict=True,
		)
	if not rows:
		return 0.0
	if warehouse:
		for r in rows:
			if r.warehouse == warehouse:
				return flt(r.get("incoming_rate") or r.get("sbe_rate"))
	return flt(rows[0].get("incoming_rate") or rows[0].get("sbe_rate"))


def _repack_allocated_rate(row) -> float:
	"""Native ERPNext-style Repack output rate from input stock value.

	Mirrors ``StockEntry.get_basic_rate_for_repacked_items``:
	outgoing_items_cost (source rows with healthy rates) / finished output qty.
	Source/input rows use previous_healthy_sle at s_warehouse; outputs get allocated rate.
	Returns 0 when inputs lack authoritative valuation (WAITING_UPSTREAM / no invent).
	"""
	purpose = g(row, "purpose") or ""
	if purpose != "Repack":
		return 0.0
	parent = g(row, "parent")
	if not parent:
		return 0.0
	# Only allocate onto target/finished rows (have t_warehouse). Source rows use chain rate.
	if g(row, "s_warehouse") and not g(row, "t_warehouse"):
		return _previous_healthy_sle_rate(
			g(row, "item_code"),
			g(row, "s_warehouse"),
			g(row, "posting_date"),
			g(row, "posting_time"),
		)
	details = frappe.db.sql(
		"""
		SELECT name, idx, item_code, qty, transfer_qty, s_warehouse, t_warehouse,
		       basic_rate, valuation_rate, is_finished_item, allow_zero_valuation_rate,
		       additional_cost
		FROM `tabStock Entry Detail`
		WHERE parent=%s
		ORDER BY idx
		""",
		parent,
		as_dict=True,
	)
	if not details:
		return 0.0
	posting_date = g(row, "posting_date")
	posting_time = g(row, "posting_time")
	outgoing_cost = 0.0
	any_source = False
	for d in details:
		s_wh = g(d, "s_warehouse")
		if not s_wh:
			continue
		any_source = True
		qty = flt(g(d, "transfer_qty") or g(d, "qty"))
		rate = flt(g(d, "basic_rate") or g(d, "valuation_rate"))
		if abs(rate) <= RATE_EPS:
			rate = _previous_healthy_sle_rate(g(d, "item_code"), s_wh, posting_date, posting_time)
		if abs(rate) <= RATE_EPS:
			# Missing authoritative source valuation — cannot invent.
			return 0.0
		outgoing_cost += abs(qty) * abs(rate)
		outgoing_cost += flt(g(d, "additional_cost"))
	if not any_source or outgoing_cost <= VALUE_EPS:
		return 0.0

	from frappe.utils import cint

	finished = [
		d
		for d in details
		if g(d, "t_warehouse")
		and (cint(g(d, "is_finished_item")) or not g(d, "s_warehouse"))
		and not flt(g(d, "allow_zero_valuation_rate"))
	]
	if not finished:
		finished = [d for d in details if g(d, "t_warehouse") and not g(d, "s_warehouse")]
	if not finished:
		return 0.0
	this_name = g(row, "name")
	this = next((d for d in finished if g(d, "name") == this_name), None)
	if not this:
		if g(row, "t_warehouse"):
			this_qty = flt(g(row, "transfer_qty") or g(row, "qty"))
			if this_qty > QTY_EPS and len(finished) == 1:
				return flt(outgoing_cost / this_qty)
		return 0.0
	this_qty = flt(g(this, "transfer_qty") or g(this, "qty"))
	if this_qty <= QTY_EPS:
		return 0.0
	if len(finished) == 1:
		return flt(outgoing_cost / this_qty)
	total_fg_qty = sum(flt(g(d, "transfer_qty") or g(d, "qty")) for d in finished)
	if total_fg_qty <= QTY_EPS:
		return 0.0
	# Equal rate per unit across finished outputs (ERPNext multi-FG path).
	return flt(outgoing_cost / total_fg_qty)


def _source_transfer_sle_rate(row) -> float:
	"""Authoritative outgoing SLE rate on the transfer voucher (source → target).

	Never uses the target/incoming SLE rate as authority — that may be the
	corrupt value being repaired (MATCHED_BUT_CORRUPT / transfer propagation).
	"""
	purpose = g(row, "purpose") or ""
	if purpose not in TRANSFER_PURPOSES:
		return 0.0
	parent = g(row, "parent")
	item = g(row, "item_code")
	if not parent or not item:
		return 0.0
	s_wh = g(row, "s_warehouse")
	conds = [
		"voucher_type='Stock Entry'",
		"voucher_no=%s",
		"item_code=%s",
		"is_cancelled=0",
		"actual_qty < 0",
	]
	args: list = [parent, item]
	if s_wh:
		conds.append("warehouse=%s")
		args.append(s_wh)
	found = frappe.db.sql(
		f"""
		SELECT actual_qty, outgoing_rate, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	if not found:
		return 0.0
	r = found[0]
	qty = flt(r.actual_qty)
	svd = flt(r.stock_value_difference)
	if abs(qty) > QTY_EPS and abs(svd) > VALUE_EPS:
		return abs(svd / qty)
	return abs(flt(r.outgoing_rate))

def _previous_healthy_sle_rate(item, warehouse, posting_date, posting_time) -> float:
	if not item or not warehouse or not posting_date:
		return 0.0
	row = frappe.db.sql(
		"""
		SELECT incoming_rate, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime < TIMESTAMP(%s, %s)
		  AND (
		        ABS(IFNULL(incoming_rate,0)) > %s
		     OR ABS(IFNULL(valuation_rate,0)) > %s
		  )
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(item, warehouse, posting_date, posting_time or "00:00:00", VALUE_EPS, VALUE_EPS),
		as_dict=True,
	)
	if not row:
		return 0.0
	return flt(row[0].incoming_rate or row[0].valuation_rate)


def _finished_item(voucher) -> str | None:
	return frappe.db.get_value(
		"Stock Entry Detail",
		{"parent": voucher, "is_finished_item": 1},
		"item_code",
	)


def _row_posting_datetime(row) -> str | None:
	"""Compose posting_datetime for as-of patient-zero clipping."""
	dt = g(row, "posting_datetime")
	if dt:
		return str(dt)
	d = g(row, "posting_date")
	if not d:
		return None
	t = g(row, "posting_time") or "00:00:00"
	return f"{d} {t}"


def _patient_for_identity(item, warehouse, batch, cache=None, as_of=None) -> dict | None:
	if not item or not warehouse:
		return None
	key = (item, warehouse, batch or "", str(as_of) if as_of else "")
	if cache is not None and key in cache:
		return cache[key]
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, item_code, warehouse, actual_qty, incoming_rate,
		       valuation_rate, stock_value, stock_value_difference, qty_after_transaction,
		       posting_datetime, batch_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		(item, warehouse),
		as_dict=True,
	)
	found = find_patient_zero(rows, batch=batch, as_of=as_of)
	if cache is not None:
		cache[key] = found
	return found


def _identity_poisoned(item, warehouse, cache=None) -> bool:
	if not item or not warehouse:
		return False
	key = ("poison", item, warehouse)
	if cache is not None and key in cache:
		return cache[key]
	rows = frappe.db.sql(
		"""
		SELECT actual_qty, incoming_rate, valuation_rate, stock_value,
		       stock_value_difference, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 12
		""",
		(item, warehouse),
		as_dict=True,
	)
	found = any(sle_poison_reason(r) for r in rows)
	if cache is not None:
		cache[key] = found
	return found


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)
