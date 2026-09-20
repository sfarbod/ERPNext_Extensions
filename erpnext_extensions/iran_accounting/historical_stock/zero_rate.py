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
	NO_ACTION_REQUIRED,
	QTY_EPS,
	RATE_EPS,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
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
	ZERO_REASON_LEGITIMATE_SCRAP,
	ZERO_REASON_MANUAL,
	ZERO_REASON_MANUFACTURE_DEP,
	ZERO_REASON_MISSING_SOURCE,
	ZERO_REASON_TRUE_CORRUPTION,
	ZERO_REASON_UPSTREAM_POISONED,
)
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero
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
	# KPI semantics: RAW vs ACTIONABLE (legitimate scrap zeros are NO_ACTION).
	from erpnext_extensions.iran_accounting.historical_stock import (
		NO_ACTION_REQUIRED,
		Z0_LEGITIMATE_SCRAP_ZERO,
		Z0_LEGITIMATE_ZERO,
	)

	raw_rows = stamped["rows"]
	no_action = [
		r
		for r in raw_rows
		if r.get("no_action_required")
		or r.get("status") in (NO_ACTION_REQUIRED, Z0_LEGITIMATE_ZERO, Z0_LEGITIMATE_SCRAP_ZERO)
		or r.get("zero_class") in (Z0_LEGITIMATE_SCRAP_ZERO, Z0_LEGITIMATE_ZERO)
	]
	actionable = [r for r in raw_rows if r not in no_action and r.get("actionable", True)]
	stamped["raw_count"] = len(raw_rows)
	stamped["no_action_required_count"] = len(no_action)
	stamped["legitimate_scrap_zero_count"] = sum(
		1 for r in raw_rows if r.get("zero_class") == Z0_LEGITIMATE_SCRAP_ZERO
	)
	stamped["actionable_count"] = len(actionable)
	stamped["true_zero_corruption_count"] = sum(
		1
		for r in actionable
		if (r.get("zero_reason") or "")
		in (
			"TRUE_ZERO_RATE_CORRUPTION",
			"MISSING_SOURCE_RATE",
			"Z1_HISTORICAL_RATE_LOST",
			"Z3_MISSING_INCOMING_VALUATION",
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

	# Rule 1 — zero-valued receipt into Scrap/Reject/Waste is NO_ACTION_REQUIRED.
	from erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse import (
		is_legitimate_scrap_zero_rate,
		scrap_valuation_role,
	)

	if is_legitimate_scrap_zero_rate(row, company=g(row, "company")):
		from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

		role = scrap_valuation_role(row)
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
				"source_of_truth": "legitimate_scrap_reject_waste_warehouse",
				"confidence": CONFIDENCE_EXACT,
				"zero_class": Z0_LEGITIMATE_SCRAP_ZERO,
				"zero_reason": ZERO_REASON_LEGITIMATE_SCRAP,
				"scrap_valuation_role": role,
				"status": NO_ACTION_REQUIRED,
				"planner_status": NO_ACTION_REQUIRED,
				"actionable": False,
				"patient_zero": None,
				"eligible": False,
				"work_order": g(row, "work_order"),
				"job_card": g(row, "job_card"),
				"is_finished_item": g(row, "is_finished_item"),
				"secondary_item_type": g(row, "secondary_item_type"),
				"reconstruction_sources": {},
				"rate_source": "legitimate_scrap_zero",
				"no_action_required": True,
			},
			row,
		)

	if g(row, "allow_zero_valuation_rate") or purpose == "Stock Reconciliation" or g(row, "voucher_type") == "Stock Reconciliation":
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
			},
			row,
		)

	version_rate, version_amount = _version_rate(parent, detail)
	batch_rate = _batch_inward_rate(item, batch, warehouse)
	prev_sle_rate = _previous_healthy_sle_rate(item, warehouse, g(row, "posting_date"), g(row, "posting_time"))
	issued = 0.0
	if purpose == "Manufacture" and (is_scrap_row(row) or g(row, "secondary_item_type") == "Scrap"):
		doc = frappe.get_doc("Stock Entry", parent)
		issued = flt(_issued_rate_for_component(doc, row))

	# Idempotent: already has a non-zero basic rate → not a zero-rate defect.
	# v5.3.0: refuse false COMPLETE when the non-zero rate is poisoned/negative/non-finite.
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

	as_of = _row_posting_datetime(row)
	patient = _patient_for_identity(item, warehouse, batch, cache=_cache, as_of=as_of)
	# Later identity poison must not block an earlier EXACT reconstructable root.
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
	if zero_class == Z0_LEGITIMATE_ZERO:
		status = Z0_LEGITIMATE_ZERO
		zero_reason = Z0_LEGITIMATE_ZERO
	elif patient and patient.get("voucher_no") and patient["voucher_no"] != parent:
		status = STATUS_DEPENDENCY_REPAIR_REQUIRED
		zero_reason = ZERO_REASON_MANUFACTURE_DEP if purpose == "Manufacture" else ZERO_REASON_MISSING_SOURCE
	elif _identity_poisoned(item, warehouse, cache=_cache):
		status = STATUS_VALUATION_POISON_DEPENDENCY
		zero_reason = ZERO_REASON_UPSTREAM_POISONED
	elif confidence == CONFIDENCE_EXACT and proposed > RATE_EPS:
		status = STATUS_RECONSTRUCTABLE
		zero_reason = ZERO_REASON_TRUE_CORRUPTION
	elif confidence == CONFIDENCE_LIKELY:
		status = STATUS_MANUAL_REVIEW
		zero_reason = ZERO_REASON_MISSING_SOURCE if zero_class == Z3_MISSING_INCOMING_VALUATION else ZERO_REASON_MANUAL
	else:
		status = STATUS_MANUAL_REVIEW
		zero_reason = (
			ZERO_REASON_MISSING_SOURCE
			if zero_class == Z3_MISSING_INCOMING_VALUATION
			else ZERO_REASON_TRUE_CORRUPTION
			if zero_class == Z1_HISTORICAL_RATE_LOST
			else ZERO_REASON_MANUAL
		)

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
			"historical_rate": version_rate or batch_rate or prev_sle_rate,
			"proposed_rate": proposed,
			"proposed_amount": amount,
			"source_of_truth": source,
			"confidence": confidence,
			"zero_class": zero_class,
			"zero_reason": zero_reason,
			"status": status,
			"actionable": status
			not in (Z0_LEGITIMATE_ZERO, NO_ACTION_REQUIRED, STATUS_RATE_REBUILD_COMPLETE),
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
		},
		row,
	)


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


def _source_transfer_sle_rate(row) -> float:
	"""Nonzero incoming SLE on the same voucher (transfer target) or prior source SLE."""
	purpose = g(row, "purpose") or ""
	if purpose not in TRANSFER_PURPOSES:
		return 0.0
	parent = g(row, "parent")
	item = g(row, "item_code")
	if not parent or not item:
		return 0.0
	found = frappe.db.sql(
		"""
		SELECT incoming_rate
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s
		  AND is_cancelled=0 AND actual_qty > 0 AND ABS(IFNULL(incoming_rate,0)) > %s
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		(parent, item, RATE_EPS),
		as_dict=True,
	)
	if found:
		return flt(found[0].incoming_rate)
	return 0.0


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
