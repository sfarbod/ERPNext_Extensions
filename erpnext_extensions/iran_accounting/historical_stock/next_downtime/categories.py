# Copyright (c) 2026, ERPNext Extensions contributors
"""A–E residual lanes. MANUAL is only insufficient/contradictory business evidence."""

from __future__ import annotations

AUTO_REPAIRABLE = "AUTO_REPAIRABLE"
WAITING_UPSTREAM = "WAITING_UPSTREAM"
LEGITIMATE = "LEGITIMATE"
TECHNICAL_TOOL_GAP = "TECHNICAL_TOOL_GAP"
MANUAL_BUSINESS_EVIDENCE_REQUIRED = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"

# Scanner labels that are NOT business-manual.
_TOOL_OR_WAITING = frozenset(
	{
		"MANUAL_TRANSFER_PROPAGATION",
		"MANUAL_MANUFACTURE_DEPENDENCY",
		"MANUAL_TOOL_LIMIT",
		"MANUAL_POSTING_ORDER_DEPENDENCY",
		"MANUAL_FAILED_RIV_PARTIAL",
		"MANUAL_DOWNSTREAM_SYMPTOM",
		"MANUAL_HISTORICAL_ONLY",
		"MANUAL_UPSTREAM_ZERO_RATE",
		"MANUAL_UPSTREAM_WRONG_RATE",
		"MANUAL_I4_DEPENDENCY",
		"TOOL_LIMIT",
		"WAITING_UPSTREAM",
		"DOWNSTREAM_SYMPTOM",
		"HISTORICAL_ONLY",
	}
)
_LEGITIMATE = frozenset(
	{
		"PRECISION_DUST",
		"PRECISION_DUST_INBOUND",
		"HISTORICAL_INTERMEDIATE_ROW",
		"ZP_PROVEN_LEGITIMATE_ZERO",
		"LEGITIMATE_ZERO_RECEIPT",
		"LEGITIMATE_FREE_RECEIPT",
		"LEGITIMATE_SCRAP",
		"LEGITIMATE_REJECT",
		"LEGITIMATE_AFTER_DEPLETION",
		"NO_REPAIR_NEEDED",
		"NO_REPAIR_PATH",
	}
)


def classify_lane(
	*,
	planner_status: str = "",
	manual_reason: str = "",
	manual_lane: str = "",
	confidence: str = "",
	detection: str = "",
	i4_reason: str = "",
	zero_provenance: str = "",
	has_authoritative_source: bool | None = None,
	sources_disagree: bool = False,
	is_descendant: bool = False,
) -> str:
	"""Map a residual row to exactly one A–E lane. No rate invention."""
	blob = " ".join(
		[
			str(planner_status or ""),
			str(manual_reason or ""),
			str(manual_lane or ""),
			str(i4_reason or ""),
			str(zero_provenance or ""),
		]
	).upper()
	if sources_disagree and has_authoritative_source is False:
		return MANUAL_BUSINESS_EVIDENCE_REQUIRED
	if any(tok in blob for tok in _LEGITIMATE) or "PRECISION_DUST" in blob:
		return LEGITIMATE
	if is_descendant or "WAITING" in blob or str(manual_lane) == "WAITING_UPSTREAM":
		return WAITING_UPSTREAM
	if str(manual_lane) == "TOOL_LIMIT" or any(tok in blob for tok in _TOOL_OR_WAITING):
		return TECHNICAL_TOOL_GAP
	if str(planner_status or "").startswith("READY") and str(confidence).upper() == "EXACT":
		return AUTO_REPAIRABLE
	if str(detection) == "CROSS_TIME" and str(planner_status or "").startswith("READY"):
		return AUTO_REPAIRABLE
	if str(manual_lane) == "USER_ACTION_REQUIRED" and has_authoritative_source is False:
		return MANUAL_BUSINESS_EVIDENCE_REQUIRED
	if "MANUAL" in blob and has_authoritative_source is False and sources_disagree:
		return MANUAL_BUSINESS_EVIDENCE_REQUIRED
	if "MANUAL" in blob:
		return TECHNICAL_TOOL_GAP
	return TECHNICAL_TOOL_GAP
