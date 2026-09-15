# Copyright (c) 2026, ERPNext Extensions contributors
"""Wrong Rate root classifier — taxonomy + patient-zero awareness (no Bin truth)."""

from __future__ import annotations

from collections import defaultdict

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import CONFIDENCE_EXACT, RATE_EPS
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine import (
	AMBIGUOUS_RATE,
	DOWNSTREAM_RATE_SYMPTOM,
	FLAG_TO_BUCKET,
	MANUAL_RATE,
	PATIENT_ZERO_RATE,
	RATE_AMBIGUOUS,
	RATE_MANUAL,
	RATE_POISONED_OPENING,
	RATE_REPLAY_REQUIRED,
	RATE_WAREHOUSE_ESCALATION,
	READY_WRONG_RATE,
	WAITING_PATIENT_ZERO,
	WAITING_RATE_DEPENDENCY,
	WRONG_OUTGOING_RATE,
)


def classify_wrong_rate_row(row: dict) -> dict:
	"""Stamp Phase-2 taxonomy + planner-facing rate_status on a scanned row."""
	row = dict(row or {})
	flags = list(row.get("flags") or [])
	mismatch = row.get("mismatch_class") or (flags[0] if flags else None)
	bucket = FLAG_TO_BUCKET.get(str(mismatch), None)
	purpose = str(row.get("purpose") or "")
	if not bucket:
		if row.get("confidence") == "AMBIGUOUS":
			bucket = AMBIGUOUS_RATE
		elif row.get("confidence") in ("MANUAL", "LIKELY"):
			bucket = MANUAL_RATE
		else:
			bucket = PATIENT_ZERO_RATE if mismatch else MANUAL_RATE

	# Context refinements
	if purpose == "Manufacture" and row.get("is_finished_item"):
		bucket = "WRONG_MANUFACTURE_FG_RATE"
	elif purpose == "Manufacture" and (
		row.get("is_scrap_item") or row.get("secondary_item_type") == "Scrap"
	):
		bucket = "WRONG_COMPONENT_SCRAP_RATE"
	elif purpose in ("Material Transfer", "Send to Subcontractor") and row.get("t_warehouse"):
		if "INCOMING" in str(mismatch or ""):
			bucket = "WRONG_TRANSFER_DEST_RATE"

	ps = str(row.get("planner_status") or "")
	conf = str(row.get("confidence") or "")
	pz = row.get("patient_zero")
	pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
	voucher = row.get("voucher")

	if conf == "AMBIGUOUS" or ps == "AMBIGUOUS":
		rate_status = RATE_AMBIGUOUS
	elif conf in ("LIKELY", "MANUAL") or ps == "MANUAL":
		rate_status = RATE_MANUAL
	elif "WAREHOUSE" in ps:
		rate_status = RATE_WAREHOUSE_ESCALATION
	elif ps in ("WAITING_PATIENT_ZERO",) or (pz_v and voucher and pz_v != voucher and not row.get("eligible")):
		rate_status = WAITING_PATIENT_ZERO
		if bucket not in (AMBIGUOUS_RATE, MANUAL_RATE):
			bucket = DOWNSTREAM_RATE_SYMPTOM
	elif ps in ("WAITING_RATE_REPAIR", "NO_REPAIR_PATH") and not row.get("eligible"):
		rate_status = WAITING_RATE_DEPENDENCY if "WAITING" in ps else RATE_MANUAL
	elif row.get("eligible") and conf == CONFIDENCE_EXACT and (
		ps.startswith("READY") or ps == "READY" or row.get("status") == "RECONSTRUCTABLE"
	):
		if int(row.get("sql_updates") or 0) <= 0 and not ps.startswith("READY"):
			rate_status = RATE_MANUAL
		elif int(row.get("sql_updates") or 0) <= 0 and ps.startswith("READY"):
			# Planner READY without write surface — treat as replay-required, not auto-apply
			rate_status = RATE_REPLAY_REQUIRED
		else:
			rate_status = READY_WRONG_RATE
			if not pz_v or pz_v == voucher:
				bucket = PATIENT_ZERO_RATE if bucket == DOWNSTREAM_RATE_SYMPTOM else bucket
	elif ps.startswith("READY") and int(row.get("sql_updates") or 0) > 0:
		rate_status = READY_WRONG_RATE
	elif ps.startswith("READY"):
		rate_status = RATE_REPLAY_REQUIRED
	else:
		rate_status = RATE_REPLAY_REQUIRED if row.get("eligible") else RATE_MANUAL

	row["rate_bucket"] = bucket
	row["rate_status"] = rate_status
	row["current_value"] = flt(row.get("current") or row.get("current_rate"))
	row["expected_value"] = flt(row.get("expected") or row.get("proposed_rate"))
	row["rate_difference"] = row["expected_value"] - row["current_value"]
	row["expected_source"] = row.get("source") or row.get("source_of_truth") or row.get("rate_source")
	# Never Bin
	if str(row.get("expected_source") or "").lower() == "bin":
		row["expected_source"] = "manual"
		row["rate_status"] = RATE_MANUAL
		row["rate_bucket"] = MANUAL_RATE
		row["eligible"] = False
	return row


def classify_wrong_rate_universe(company=None, limit=2000) -> dict:
	scan = scan_wrong_rates(company=company, limit=limit)
	rows = [classify_wrong_rate_row(r) for r in scan.get("rows") or []]
	by_bucket = defaultdict(int)
	by_status = defaultdict(int)
	ready = []
	for r in rows:
		by_bucket[r.get("rate_bucket") or ""] += 1
		by_status[r.get("rate_status") or ""] += 1
		if r.get("rate_status") == READY_WRONG_RATE:
			ready.append(r)
	return {
		"count": len(rows),
		"rows": rows,
		"by_bucket": dict(by_bucket),
		"by_rate_status": dict(by_status),
		"by_flag": scan.get("by_flag"),
		"by_confidence": scan.get("by_confidence"),
		"ready_wrong_rate": len(ready),
		"ready_rows": ready,
	}
