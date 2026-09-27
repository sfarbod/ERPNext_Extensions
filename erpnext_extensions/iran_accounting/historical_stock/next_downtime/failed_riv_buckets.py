# Copyright (c) 2026, ERPNext Extensions contributors
"""Failed RIV lanes. Never delete audit history. Never blind-retry."""

from __future__ import annotations

HISTORICAL_ONLY = "HISTORICAL_ONLY"
SUPERSEDED = "SUPERSEDED"
WAITING_UPSTREAM = "WAITING_UPSTREAM"
SAFE_TO_RETRY = "SAFE_TO_RETRY"
TECHNICAL_TOOL_GAP = "TECHNICAL_TOOL_GAP"
MANUAL_BUSINESS_EVIDENCE_REQUIRED = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"

_SAFE_ERRORS = frozenset({"DEADLOCK", "TIMEOUT", "RIV_DEADLOCK", "RIV_TIMEOUT"})
_WAIT = frozenset(
	{
		"RIV_WAITING_RATE",
		"RIV_WAITING_PATIENT_ZERO",
		"RIV_WAITING_REPLAY",
		"RIV_WAITING_SLE",
		"RIV_WAITING_GL",
		"WAITING_UPSTREAM",
	}
)


def classify_failed_riv_row(row: dict) -> str:
	recon = str(row.get("riv_reconcile_status") or row.get("reconcile") or "")
	if recon in ("HISTORICAL_ONLY",):
		return HISTORICAL_ONLY
	if recon in ("SUPERSEDED_BY_SUCCESSFUL_REPAIR", "SUPERSEDED"):
		return SUPERSEDED
	err = str(row.get("error_class") or row.get("riv_status") or "").upper()
	if recon == "CURRENT_LEDGER_IMPACT" and err in _SAFE_ERRORS:
		return SAFE_TO_RETRY
	if err in _WAIT or recon in _WAIT:
		return WAITING_UPSTREAM
	if err in ("VALUATION_INTEGRITY", "RIV_VALUATION_INTEGRITY", "RIV_NEGATIVE_STOCK"):
		return TECHNICAL_TOOL_GAP
	if row.get("sources_disagree") and not row.get("has_authoritative_source"):
		return MANUAL_BUSINESS_EVIDENCE_REQUIRED
	if recon == "CURRENT_LEDGER_IMPACT":
		return TECHNICAL_TOOL_GAP
	return HISTORICAL_ONLY
