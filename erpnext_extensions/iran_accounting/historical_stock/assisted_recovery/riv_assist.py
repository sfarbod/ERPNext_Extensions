# Copyright (c) 2026, ERPNext Extensions contributors
"""Refine Failed RIV root-cause into waiting classes (no blind retry)."""

from __future__ import annotations

# Assisted waiting labels (map onto existing riv_status where possible)
WAITING_SLE = "WAITING_SLE"
WAITING_GL = "WAITING_GL"
WAITING_RATE = "WAITING_RATE"
WAITING_WAREHOUSE = "WAITING_WAREHOUSE"
WAITING_NEGATIVE = "WAITING_NEGATIVE"
WAITING_REPLAY = "WAITING_REPLAY"
WAITING_PATIENT_ZERO = "WAITING_PATIENT_ZERO"


def refine_riv_root_cause(row: dict) -> dict:
	"""Return a precise waiting bucket + guidance without retrying UNKNOWN."""
	status = str(row.get("riv_status") or row.get("status") or "")
	err = str(row.get("error_head") or "")
	sle = str(row.get("sle_state") or row.get("sle_status") or "")
	gl = str(row.get("gl_class") or row.get("gl_status") or "")

	if status in ("NEGATIVE_STOCK",) or "negative stock" in err.lower():
		bucket = WAITING_NEGATIVE
		guidance = "Clear negative stock / posting order / warehouse campaign before RIV retry"
	elif status in ("WAITING_PATIENT_ZERO",) or row.get("patient_zero"):
		bucket = WAITING_PATIENT_ZERO
		guidance = f"Repair patient zero {row.get('patient_zero')} first"
	elif status in ("WAITING_RATE",) or row.get("zero_rate_dependency") or row.get("wrong_rate_dependency"):
		bucket = WAITING_RATE
		guidance = "Repair Zero/Wrong Rate on this identity first"
	elif status in ("WAITING_SLE",) or sle in ("POISONED_CHAIN", "PATIENT_ZERO_REQUIRED"):
		bucket = WAITING_SLE
		guidance = "Repair SLE poison / leftover before RIV"
	elif status in ("WAITING_GL",) or gl in ("G3_UNBALANCED", "G4_BUILT_FROM_POISONED_SLE"):
		bucket = WAITING_GL
		guidance = "Repair GL after SLE is healthy"
	elif "warehouse" in err.lower() or status == "VALUATION_INTEGRITY":
		bucket = WAITING_WAREHOUSE
		guidance = "Warehouse Engine / valuation integrity must clear first"
	elif status in ("WAITING_REPLAY",):
		bucket = WAITING_REPLAY
		guidance = "Complete identity replay before RIV"
	elif status == "SAFE_TO_RETRY":
		bucket = "SAFE_TO_RETRY"
		guidance = "Chain healthy — safe controlled retry"
	elif status == "UNKNOWN":
		bucket = "UNKNOWN"
		guidance = "Do not retry — root cause unexplained on healthy chain"
	else:
		bucket = status or "UNKNOWN"
		guidance = "See riv_status / error_head"

	return {
		"riv_name": row.get("riv_name") or row.get("name"),
		"item": row.get("item"),
		"warehouse": row.get("warehouse"),
		"voucher": row.get("voucher"),
		"riv_status": status,
		"waiting_bucket": bucket,
		"guidance": guidance,
		"safe_to_retry": bucket == "SAFE_TO_RETRY",
		"auto_retry": False,  # Phase 3 never blind-retries
	}
