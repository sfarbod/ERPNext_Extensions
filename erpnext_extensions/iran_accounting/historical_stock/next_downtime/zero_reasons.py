# Copyright (c) 2026, ERPNext Extensions contributors
"""Zero-rate causal classes. Never force a nonzero rate."""

from __future__ import annotations

LEGITIMATE_ZERO_RECEIPT = "LEGITIMATE_ZERO_RECEIPT"
LEGITIMATE_FREE_RECEIPT = "LEGITIMATE_FREE_RECEIPT"
LEGITIMATE_SCRAP = "LEGITIMATE_SCRAP"
LEGITIMATE_REJECT = "LEGITIMATE_REJECT"
LEGITIMATE_AFTER_DEPLETION = "LEGITIMATE_AFTER_DEPLETION"
LEFTOVER_MA = "LEFTOVER_MA"
VALUED_SOURCE_ZERO_OUTGOING = "VALUED_SOURCE_ZERO_OUTGOING"
TRANSFER_VALUE_LOSS = "TRANSFER_VALUE_LOSS"
SABB_VALUE_LOSS = "SABB_VALUE_LOSS"
MANUFACTURE_VALUE_LOSS = "MANUFACTURE_VALUE_LOSS"
WAITING_UPSTREAM = "WAITING_UPSTREAM"
TECHNICAL_TOOL_GAP = "TECHNICAL_TOOL_GAP"
MANUAL_BUSINESS_EVIDENCE_REQUIRED = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"

_SCRAP_TOKENS = ("ضایعات", "scrap", "waste")
_REJECT_TOKENS = ("reject", "ریجکت")


def classify_zero_rate(row: dict) -> str:
	prov = str(row.get("zero_provenance") or row.get("classification") or "").upper()
	purpose = str(row.get("purpose") or "")
	wh = str(row.get("warehouse") or "").lower()
	allow_zero = int(row.get("allow_zero_valuation_rate") or 0)
	qty = float(row.get("qty") or row.get("actual_qty") or 0)
	value = abs(float(row.get("stock_value") or 0))
	rate = abs(float(row.get("valuation_rate") or row.get("outgoing_rate") or 0))
	source_value = abs(float(row.get("source_stock_value") or 0))
	source_rate = abs(float(row.get("source_rate") or 0))
	is_scrap = int(row.get("is_scrap_item") or 0) or any(t in wh for t in _SCRAP_TOKENS)
	is_reject = int(row.get("is_reject") or 0) or any(t in wh for t in _REJECT_TOKENS)
	if "LEGITIMATE" in prov or prov.startswith("ZP_PROVEN"):
		return LEGITIMATE_ZERO_RECEIPT
	if allow_zero and qty > 0 and rate <= 1e-6 and value <= 1e-6:
		return LEGITIMATE_FREE_RECEIPT if "FREE" in prov or purpose == "Material Receipt" else LEGITIMATE_ZERO_RECEIPT
	if is_scrap and allow_zero:
		return LEGITIMATE_SCRAP
	if is_reject and allow_zero:
		return LEGITIMATE_REJECT
	if abs(qty) <= 1e-9 and value <= 1e-6:
		return LEGITIMATE_AFTER_DEPLETION
	if qty > 0 and value > 1 and rate <= 1e-6:
		return LEFTOVER_MA
	if (
		qty < 0
		and rate <= 1e-6
		and not allow_zero
		and (source_rate > 1e-6 or source_value > 1)
		and purpose in ("Manufacture", "Material Consumption for Manufacture")
	):
		return VALUED_SOURCE_ZERO_OUTGOING
	if purpose == "Material Transfer for Manufacture" and "SABB" in prov:
		return SABB_VALUE_LOSS
	if purpose in ("Material Transfer", "Material Transfer for Manufacture") and source_rate > 1e-6:
		return TRANSFER_VALUE_LOSS
	if purpose == "Manufacture":
		return MANUFACTURE_VALUE_LOSS
	if int(row.get("is_descendant") or 0):
		return WAITING_UPSTREAM
	if row.get("sources_disagree") and not row.get("has_authoritative_source"):
		return MANUAL_BUSINESS_EVIDENCE_REQUIRED
	return TECHNICAL_TOOL_GAP
