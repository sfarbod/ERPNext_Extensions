# Copyright (c) 2026, ERPNext Extensions contributors
"""SLE / Bin integrity scan (read-only)."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	QTY_EPS,
	SLE_AMBIGUOUS,
	SLE_HEALTHY,
	SLE_PATIENT_ZERO_REQUIRED,
	SLE_POISONED_CHAIN,
	SLE_REPLAY_REQUIRED,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason


def scan_sle_bin(company=None, item_code=None, warehouse=None, limit=500) -> dict:
	conds = ["sle.is_cancelled=0"]
	args: list = []
	if item_code:
		conds.append("sle.item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if company:
		conds.append("sle.company=%s")
		args.append(company)
	# Pull recent-ish anomalies rather than every SLE.
	poison_rows = frappe.db.sql(
		f"""
		SELECT sle.name, sle.item_code, sle.warehouse, sle.voucher_no, sle.voucher_type,
		       sle.actual_qty, sle.incoming_rate, sle.valuation_rate, sle.stock_value,
		       sle.stock_value_difference, sle.qty_after_transaction, sle.posting_datetime,
		       sle.batch_no, sle.serial_and_batch_bundle
		FROM `tabStock Ledger Entry` sle
		WHERE {" AND ".join(conds)}
		  AND (
		        (sle.actual_qty > {QTY_EPS} AND sle.incoming_rate < 0)
		     OR (ABS(sle.qty_after_transaction) < {QTY_EPS} AND ABS(IFNULL(sle.stock_value,0)) > 1)
		     OR (ABS(IFNULL(sle.valuation_rate,0)) > 1000000000000)
		     OR (sle.qty_after_transaction > 1 AND ABS(IFNULL(sle.stock_value,0)) < {VALUE_EPS}
		         AND ABS(sle.actual_qty) > {QTY_EPS})
		  )
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
		rows.append(
			{
				"topic": "SLE_BIN",
				"voucher": sle.voucher_no,
				"sle": sle.name,
				"item": sle.item_code,
				"warehouse": sle.warehouse,
				"batch": sle.batch_no,
				"reason": reason,
				"status": status,
				"patient_zero": patient,
				"confidence": CONFIDENCE_LIKELY if patient else CONFIDENCE_EXACT,
				"eligible": False,
			}
		)
	bin_rows = scan_bin_mismatches(company=company, limit=200)
	for b in bin_rows:
		rows.append(
			{
				"topic": "SLE_BIN",
				"voucher": b.get("last_voucher"),
				"sle": None,
				"item": b["item"],
				"warehouse": b["warehouse"],
				"batch": None,
				"reason": "bin_last_sle_mismatch",
				"status": b.get("status") or SLE_REPLAY_REQUIRED,
				"patient_zero": None,
				"confidence": CONFIDENCE_LIKELY,
				"eligible": False,
				"bin_qty": b.get("bin_qty"),
				"sle_qty": b.get("sle_qty"),
				"bin_value": b.get("bin_value"),
				"sle_value": b.get("sle_value"),
			}
		)
	return {
		"count": len(rows),
		"rows": rows,
		"bin_mismatches": bin_rows,
		"by_status": _count(rows, "status"),
	}


def scan_bin_mismatches(company=None, limit=200) -> list[dict]:
	try:
		rows = frappe.db.sql(
			"""
			SELECT item_code, warehouse, bin_qty, bin_value, sle_qty, sle_value, voucher_no
			FROM (
				SELECT
					b.item_code,
					b.warehouse,
					b.actual_qty bin_qty,
					b.stock_value bin_value,
					sle.qty_after_transaction sle_qty,
					sle.stock_value sle_value,
					sle.voucher_no,
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
		return [
			{
				"item": r.item_code,
				"warehouse": r.warehouse,
				"bin_qty": r.bin_qty,
				"sle_qty": r.sle_qty,
				"bin_value": r.bin_value,
				"sle_value": r.sle_value,
				"status": SLE_REPLAY_REQUIRED,
				"last_voucher": r.voucher_no,
			}
			for r in rows
		]
	except Exception:
		return _scan_bin_mismatches_fallback(company=company, limit=limit)


def _scan_bin_mismatches_fallback(company=None, limit=200) -> list[dict]:
	# Compare last SLE vs Bin for identities that appear in Bin.
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
			SELECT qty_after_transaction, stock_value, valuation_rate, voucher_no, posting_datetime
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
			out.append(
				{
					"item": b.item_code,
					"warehouse": b.warehouse,
					"bin_qty": b.actual_qty,
					"sle_qty": sle.qty_after_transaction,
					"bin_value": b.stock_value,
					"sle_value": sle.stock_value,
					"status": SLE_REPLAY_REQUIRED,
					"last_voucher": sle.voucher_no,
				}
			)
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
	if any(sle_poison_reason(r) in (
		"negative_incoming_rate",
		"sign_inverted_incoming_svd",
		"sign_inverted_outgoing_svd",
		"exploded_rate",
	) for r in last[:8]):
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
