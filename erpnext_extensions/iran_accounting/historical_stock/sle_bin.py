# Copyright (c) 2026, ERPNext Extensions contributors
"""SLE / Bin integrity scan (read-only)."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	I4_LEFTOVER_REPAIR,
	I4_READY,
	I4_WAITING,
	QTY_EPS,
	SLE_AMBIGUOUS,
	SLE_HEALTHY,
	SLE_PATIENT_ZERO_REQUIRED,
	SLE_POISONED_CHAIN,
	SLE_REPLAY_REQUIRED,
	SLE_WAITING_DOWNSTREAM_REPAIR,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero
from erpnext_extensions.iran_accounting.historical_stock.scan_filters import (
	append_sle_scope,
	filter_rows_by_planner,
	normalize_scope,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason


def scan_sle_bin(
	company=None,
	item_code=None,
	warehouse=None,
	voucher=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
	repair_class=None,
	planner_status=None,
	patient_zero=None,
	limit=500,
) -> dict:
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
	conds = ["sle.is_cancelled=0"]
	args: list = []
	conds, args, join_sql = append_sle_scope(conds, args, scope)
	if scope.get("repair_class") == I4_LEFTOVER_REPAIR:
		conds.append(f"ABS(sle.qty_after_transaction) < {QTY_EPS}")
		conds.append("ABS(IFNULL(sle.stock_value,0)) > 1")
		poison_filter = "1=1"
	else:
		poison_filter = f"""
		  (
		        (sle.actual_qty > {QTY_EPS} AND sle.incoming_rate < 0)
		     OR (ABS(sle.qty_after_transaction) < {QTY_EPS} AND ABS(IFNULL(sle.stock_value,0)) > 1)
		     OR (ABS(IFNULL(sle.valuation_rate,0)) > 1000000000000)
		     OR (sle.qty_after_transaction > 1 AND ABS(IFNULL(sle.stock_value,0)) < {VALUE_EPS}
		         AND ABS(sle.actual_qty) > {QTY_EPS})
		  )
		"""
	poison_rows = frappe.db.sql(
		f"""
		SELECT sle.name, sle.item_code, sle.warehouse, sle.voucher_no, sle.voucher_type,
		       sle.actual_qty, sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate, sle.stock_value,
		       sle.stock_value_difference, sle.qty_after_transaction, sle.posting_datetime,
		       sle.batch_no, sle.serial_and_batch_bundle, sle.posting_date
		FROM `tabStock Ledger Entry` sle
		{join_sql}
		WHERE {" AND ".join(conds)}
		  AND {poison_filter}
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	rows = []
	for sle in poison_rows:
		reason = sle_poison_reason(sle) or "unexpected_zero_value"
		patient = _patient(sle.item_code, sle.warehouse)
		status = SLE_POISONED_CHAIN
		if patient and patient.get("voucher_no") != sle.voucher_no:
			status = SLE_PATIENT_ZERO_REQUIRED
		elif reason:
			status = SLE_REPLAY_REQUIRED
		is_i4 = reason in ("qty_after_zero_nonzero_value", "qty_zero_nonzero_value")
		this_is_pz = bool(patient and patient.get("voucher_no") == sle.voucher_no)
		i4_status = None
		repair_cls = None
		residual = flt(sle.stock_value) if is_i4 else 0.0
		if is_i4:
			repair_cls = I4_LEFTOVER_REPAIR
			i4_status = I4_READY if this_is_pz else I4_WAITING
			topic = "I4_LEFTOVER"
		else:
			topic = "SLE_BIN"
		rows.append(
			{
				"topic": topic,
				"repair_class": repair_cls,
				"voucher": sle.voucher_no,
				"sle": sle.name,
				"item": sle.item_code,
				"warehouse": sle.warehouse,
				"batch": sle.batch_no,
				"serial_and_batch_bundle": sle.serial_and_batch_bundle,
				"reason": reason,
				"status": status,
				"i4_status": i4_status,
				"patient_zero": patient,
				"confidence": CONFIDENCE_EXACT if (is_i4 and this_is_pz) else CONFIDENCE_LIKELY,
				"eligible": False,
				"qty_after": flt(sle.qty_after_transaction),
				"current_value": flt(sle.stock_value),
				"expected_value": 0.0 if is_i4 else None,
				"residual_value": residual,
				"qty_becomes_zero": is_i4,
				"message": "Identity leftover detected." if is_i4 else "",
				"posting_datetime": str(sle.posting_datetime) if sle.posting_datetime else None,
				"posting_date": str(sle.posting_date) if sle.posting_date else None,
			}
		)
	if scope.get("repair_class") != I4_LEFTOVER_REPAIR:
		bin_rows = scan_bin_mismatches(company=scope.get("company"), limit=200)
		for b in bin_rows:
			if scope.get("item_code") and b["item"] != scope["item_code"]:
				continue
			if scope.get("warehouse") and b["warehouse"] != scope["warehouse"]:
				continue
			rows.append(
				{
					"topic": "SLE_BIN",
					"voucher": b.get("last_voucher"),
					"sle": None,
					"item": b["item"],
					"warehouse": b["warehouse"],
					"batch": None,
					"reason": b.get("reason") or "bin_last_sle_mismatch",
					"status": b.get("status") or SLE_REPLAY_REQUIRED,
					"bin_class": b.get("bin_class"),
					"ui_label": b.get("ui_label") or "Broken Bin",
					"last_sle_poison": b.get("last_sle_poison"),
					"patient_zero": None,
					"confidence": CONFIDENCE_LIKELY,
					"eligible": False,
					"bin_qty": b.get("bin_qty"),
					"sle_qty": b.get("sle_qty"),
					"bin_value": b.get("bin_value"),
					"sle_value": b.get("sle_value"),
				}
			)
	else:
		bin_rows = []
	for row in rows:
		if row.get("repair_class") == I4_LEFTOVER_REPAIR and row.get("i4_status") == I4_READY:
			try:
				from erpnext_extensions.iran_accounting.historical_stock.i4_repair import preview_i4_replay

				sim = preview_i4_replay(row["item"], row["warehouse"], from_dt=row.get("posting_datetime"))
				row["replay_count"] = int(sim.get("rows") or 0)
				row["sql_updates_estimate"] = int(sim.get("sql_updates") or 0)
			except Exception:
				row["replay_count"] = 0
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	stamped = stamp_scan_result(
		{
			"count": len(rows),
			"rows": rows,
			"bin_mismatches": bin_rows,
			"by_status": _count(rows, "status"),
		}
	)
	stamped["rows"] = filter_rows_by_planner(
		stamped["rows"],
		repair_class=scope.get("repair_class"),
		planner_status=scope.get("planner_status"),
		patient_zero=scope.get("patient_zero"),
	)
	stamped["count"] = len(stamped["rows"])
	stamped["i4_count"] = sum(1 for r in stamped["rows"] if r.get("repair_class") == I4_LEFTOVER_REPAIR)
	stamped["ready_i4"] = sum(1 for r in stamped["rows"] if r.get("planner_status") == I4_READY)
	return stamped


def scan_bin_mismatches(company=None, limit=200) -> list[dict]:
	try:
		rows = frappe.db.sql(
			"""
			SELECT item_code, warehouse, bin_qty, bin_value, sle_qty, sle_value, voucher_no,
			       actual_qty, incoming_rate, outgoing_rate, valuation_rate, stock_value_difference,
			       qty_after_transaction, stock_value
			FROM (
				SELECT
					b.item_code,
					b.warehouse,
					b.actual_qty bin_qty,
					b.stock_value bin_value,
					sle.qty_after_transaction sle_qty,
					sle.stock_value sle_value,
					sle.voucher_no,
					sle.actual_qty,
					sle.incoming_rate,
					sle.outgoing_rate,
					sle.valuation_rate,
					sle.stock_value_difference,
					sle.qty_after_transaction,
					sle.stock_value,
					ROW_NUMBER() OVER (
						PARTITION BY sle.item_code, sle.warehouse
						ORDER BY sle.posting_datetime DESC, sle.creation DESC
					) rn
				FROM `tabBin` b
				JOIN `tabStock Ledger Entry` sle
					ON sle.item_code = b.item_code AND sle.warehouse = b.warehouse
					AND sle.is_cancelled = 0
			) t
			WHERE rn = 1
			  AND (
			        ABS(IFNULL(bin_qty,0) - IFNULL(sle_qty,0)) > 0.5
			     OR ABS(IFNULL(bin_value,0) - IFNULL(sle_value,0)) > %s
			  )
			LIMIT %s
			""",
			(VALUE_EPS, int(limit)),
			as_dict=True,
		)
		out = []
		for r in rows:
			classified = _classify_bin_mismatch(r)
			out.append(classified)
		return out
	except Exception:
		return _scan_bin_mismatches_fallback(company=company, limit=limit)


def _classify_bin_mismatch(r) -> dict:
	"""Distinguish temporary prefix-replay Bin state from a real regression."""
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason

	poison = sle_poison_reason(r) if hasattr(r, "actual_qty") or (isinstance(r, dict) and "actual_qty" in r) else None
	# Rebuild a poison-checkable dict if SQL row lacked fields.
	probe = {
		"actual_qty": r.get("actual_qty") if hasattr(r, "get") else getattr(r, "actual_qty", None),
		"qty_after_transaction": r.get("sle_qty") if hasattr(r, "get") else r.sle_qty,
		"stock_value": r.get("sle_value") if hasattr(r, "get") else r.sle_value,
		"incoming_rate": r.get("incoming_rate") if hasattr(r, "get") else getattr(r, "incoming_rate", 0),
		"outgoing_rate": r.get("outgoing_rate") if hasattr(r, "get") else getattr(r, "outgoing_rate", 0),
		"valuation_rate": r.get("valuation_rate") if hasattr(r, "get") else getattr(r, "valuation_rate", 0),
		"stock_value_difference": r.get("stock_value_difference")
		if hasattr(r, "get")
		else getattr(r, "stock_value_difference", 0),
	}
	poison = poison or sle_poison_reason(probe)
	bin_qty = flt(r.get("bin_qty") if hasattr(r, "get") else r.bin_qty)
	bin_value = flt(r.get("bin_value") if hasattr(r, "get") else r.bin_value)
	sle_qty = flt(r.get("sle_qty") if hasattr(r, "get") else r.sle_qty)
	sle_value = flt(r.get("sle_value") if hasattr(r, "get") else r.sle_value)
	last_voucher = r.get("voucher_no") if hasattr(r, "get") else r.voucher_no
	item = r.get("item_code") if hasattr(r, "get") else r.item_code
	warehouse = r.get("warehouse") if hasattr(r, "get") else r.warehouse

	# Prefix replay: Bin reflects last rewritten healthy terminal (often 0/0) while
	# chronologically last SLE is still poisoned / leftover. Not a silent regression.
	prefix_empty = abs(bin_qty) <= QTY_EPS and abs(bin_value) <= VALUE_EPS
	last_poisoned = bool(poison)
	classification = "REAL_BIN_REGRESSION"
	status = SLE_REPLAY_REQUIRED
	if last_poisoned and (prefix_empty or abs(bin_value - sle_value) > VALUE_EPS):
		classification = "TEMPORARY_PREFIX_REPLAY_STATE"
		status = SLE_WAITING_DOWNSTREAM_REPAIR
	elif last_poisoned:
		classification = "EXPECTED_WAITING_DEPENDENCY"
		status = SLE_WAITING_DOWNSTREAM_REPAIR

	return {
		"item": item,
		"warehouse": warehouse,
		"bin_qty": bin_qty,
		"sle_qty": sle_qty,
		"bin_value": bin_value,
		"sle_value": sle_value,
		"status": status,
		"bin_class": classification,
		"last_voucher": last_voucher,
		"last_sle_poison": poison,
		"ui_label": "WAITING_DOWNSTREAM_REPAIR" if status == SLE_WAITING_DOWNSTREAM_REPAIR else "Broken Bin",
		"reason": (
			"bin_waiting_downstream_repair"
			if status == SLE_WAITING_DOWNSTREAM_REPAIR
			else "bin_last_sle_mismatch"
		),
	}


def _scan_bin_mismatches_fallback(company=None, limit=200) -> list[dict]:
	rows = frappe.db.sql(
		"""
		SELECT b.item_code, b.warehouse, b.actual_qty, b.stock_value, b.valuation_rate
		FROM `tabBin` b
		WHERE ABS(IFNULL(b.actual_qty,0)) > %s OR ABS(IFNULL(b.stock_value,0)) > %s
		LIMIT %s
		""",
		(QTY_EPS, VALUE_EPS, int(limit) * 20),
		as_dict=True,
	)
	out = []
	for b in rows:
		last = frappe.db.sql(
			"""
			SELECT qty_after_transaction, stock_value, valuation_rate, voucher_no, posting_datetime,
			       actual_qty, incoming_rate, outgoing_rate, stock_value_difference
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime DESC, creation DESC
			LIMIT 1
			""",
			(b.item_code, b.warehouse),
			as_dict=True,
		)
		if not last:
			continue
		sle = last[0]
		if abs(flt(sle.qty_after_transaction) - flt(b.actual_qty)) > 0.5 or abs(
			flt(sle.stock_value) - flt(b.stock_value)
		) > VALUE_EPS:
			probe = frappe._dict(
				{
					"item_code": b.item_code,
					"warehouse": b.warehouse,
					"bin_qty": b.actual_qty,
					"bin_value": b.stock_value,
					"sle_qty": sle.qty_after_transaction,
					"sle_value": sle.stock_value,
					"voucher_no": sle.voucher_no,
					"actual_qty": sle.actual_qty,
					"incoming_rate": sle.incoming_rate,
					"outgoing_rate": sle.outgoing_rate,
					"valuation_rate": sle.valuation_rate,
					"stock_value_difference": sle.stock_value_difference,
				}
			)
			out.append(_classify_bin_mismatch(probe))
		if len(out) >= limit:
			break
	return out


def classify_identity(item_code, warehouse) -> str:
	if not item_code or not warehouse:
		return SLE_AMBIGUOUS
	last = frappe.db.sql(
		"""
		SELECT actual_qty, incoming_rate, valuation_rate, stock_value,
		       stock_value_difference, qty_after_transaction, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 20
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	if not last:
		return SLE_AMBIGUOUS
	if any(
		sle_poison_reason(r)
		in (
			"negative_incoming_rate",
			"sign_inverted_incoming_svd",
			"sign_inverted_outgoing_svd",
			"exploded_rate",
		)
		for r in last[:8]
	):
		return SLE_POISONED_CHAIN
	if any(sle_poison_reason(r) for r in last):
		return SLE_PATIENT_ZERO_REQUIRED
	zero_in = frappe.db.sql(
		"""
		SELECT name FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND actual_qty > %s AND ABS(IFNULL(incoming_rate,0)) < %s
		  AND ABS(IFNULL(stock_value_difference,0)) < %s
		LIMIT 1
		""",
		(item_code, warehouse, QTY_EPS, QTY_EPS, VALUE_EPS),
	)
	if zero_in:
		return SLE_PATIENT_ZERO_REQUIRED
	return SLE_HEALTHY


def _patient(item, warehouse):
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
	return find_patient_zero(rows)


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)
