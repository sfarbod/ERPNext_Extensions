# Copyright (c) 2026, ERPNext Extensions contributors
"""Stock Entry purpose → valuation-source semantics for Historical Repair (v5.3.0).

Decision principle (not warehouse name):

  TRANSACTION SEMANTICS + WHETHER AN AUTHORITATIVE VALUATION SOURCE EXISTS

Warehouse Scrap/Reject/Waste is contextual only — never the first branch.
"""

from __future__ import annotations

from typing import Any

# Purposes where Historical Repair must NOT invent a rate when no document-local
# authoritative source exists (e.g. Material Receipt opening/inbound).
NO_INVENT_RATE = "no_invent_rate"
# Purposes that normally carry valuation from a source warehouse / SLE.
RECONSTRUCT_FROM_SOURCE = "reconstruct_from_source"
# Purposes that use native manufacturing cost contract.
RECONSTRUCT_MANUFACTURE = "reconstruct_manufacture"
# Repack redistributes input stock value (+ additional costs) onto outputs.
RECONSTRUCT_REPACK = "reconstruct_repack"
# Document itself authorises zero (reconciliation / allow_zero flag handled separately).
DOCUMENT_AUTHORITATIVE_ZERO_OK = "document_authoritative_zero_ok"
# Unknown / rare purposes — require explicit source or user review.
REVIEW_IF_NO_SOURCE = "review_if_no_source"

# Authoritative source classes allowed for Material Receipt reconstruction.
# Never: previous warehouse moving average, unrelated later transactions, batch
# inward from a different voucher solely to invent a rate.
RECEIPT_ALLOWED_SOURCES = frozenset(
	{
		"version",
		"version+batch_inward",  # version agrees with batch — document-local corroboration
		"version+previous_healthy_sle",
		"version+source_transfer_sle",
		"purchase_receipt",
		"document_authoritative",
		"allow_zero_valuation_rate",
	}
)

# Source SLE / transfer reconstruction for transfers.
TRANSFER_PREFERRED_SOURCES = frozenset(
	{
		"source_transfer_sle",
		"version+source_transfer_sle",
		"version",
		"version+batch_inward",
		"batch_inward",
		"previous_healthy_sle",
		"version+previous_healthy_sle",
	}
)

# Material Issue consumes existing stock — valuation from source chain at posting time.
ISSUE_PREFERRED_SOURCES = frozenset(
	{
		"previous_healthy_sle",
		"version+previous_healthy_sle",
		"source_transfer_sle",
		"version+source_transfer_sle",
		"version",
		"version+batch_inward",
		"batch_inward",
	}
)

# Native ERPNext Repack: outgoing input cost allocated across finished outputs.
REPACK_PREFERRED_SOURCES = frozenset(
	{
		"repack_allocated",
		"version+repack_allocated",
		"version",
		"version+batch_inward",
		"batch_inward",
		"previous_healthy_sle",
		"source_transfer_sle",
	}
)


PURPOSE_REGISTRY: dict[str, dict[str, Any]] = {
	"Material Receipt": {
		"policy": NO_INVENT_RATE,
		"label": "Independent inbound — no inventable upstream valuation",
		"user_review_status": "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW",
		"allowed_auto_sources": RECEIPT_ALLOWED_SOURCES,
		"kpi_bucket": "MATERIAL_RECEIPT_ZERO_USER_REVIEW",
	},
	"Material Transfer": {
		"policy": RECONSTRUCT_FROM_SOURCE,
		"label": "Target valuation must come from source movement",
		"allowed_auto_sources": TRANSFER_PREFERRED_SOURCES,
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
	},
	"Material Transfer for Manufacture": {
		"policy": RECONSTRUCT_FROM_SOURCE,
		"label": "Transfer for Manufacture preserves source valuation",
		"allowed_auto_sources": TRANSFER_PREFERRED_SOURCES,
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
	},
	"Send to Subcontractor": {
		"policy": RECONSTRUCT_FROM_SOURCE,
		"label": "Subcontract send preserves source valuation",
		"allowed_auto_sources": TRANSFER_PREFERRED_SOURCES,
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
	},
	"Material Issue": {
		"policy": RECONSTRUCT_FROM_SOURCE,
		"label": (
			"Material Issue consumes existing stock — expected rate from "
			"Item+Warehouse(+Batch) valuation chain at posting datetime"
		),
		"allowed_auto_sources": ISSUE_PREFERRED_SOURCES,
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
		"zero_not_acceptable": True,
	},
	"Material Consumption for Manufacture": {
		"policy": RECONSTRUCT_FROM_SOURCE,
		"label": "Consumption valuation from source stock chain",
		"allowed_auto_sources": ISSUE_PREFERRED_SOURCES,
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
		"zero_not_acceptable": True,
	},
	"Manufacture": {
		"policy": RECONSTRUCT_MANUFACTURE,
		"label": "Native manufacturing cost / issued-pool reconstruction",
		# Never invent FG/source rates from previous_healthy_sle or unrelated batch
		# inward — that resurrected sibling leftover MA (false EXACT READY). Use
		# manufacture_native residual / same-voucher pool only.
		"allowed_auto_sources": frozenset(
			{
				"same_voucher_issued_rate",
				"manufacture_pool",
				"manufacture_consumed_svd_residual",
				"version",
				"version+batch_inward",
			}
		),
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
	},
	"Repack": {
		"policy": RECONSTRUCT_REPACK,
		"label": (
			"Repack redistributes input stock value (+ additional costs) onto "
			"finished outputs per native ERPNext get_basic_rate_for_repacked_items"
		),
		"allowed_auto_sources": REPACK_PREFERRED_SOURCES,
		"kpi_bucket": "ZERO_RATE_RECONSTRUCTABLE",
		"zero_not_acceptable": True,
	},
	"Stock Reconciliation": {
		"policy": DOCUMENT_AUTHORITATIVE_ZERO_OK,
		"label": "Document may authorise zero valuation",
		"allowed_auto_sources": frozenset({"document_authoritative", "allow_zero_valuation_rate"}),
		"kpi_bucket": "NO_ACTION",
	},
}


def purpose_semantics(purpose: str | None) -> dict[str, Any]:
	"""Return registry entry for a Stock Entry purpose (default: review if no source)."""
	p = (purpose or "").strip()
	if p in PURPOSE_REGISTRY:
		return dict(PURPOSE_REGISTRY[p], purpose=p)
	return {
		"purpose": p or "Unknown",
		"policy": REVIEW_IF_NO_SOURCE,
		"label": "Unregistered purpose — require authoritative source or user review",
		"allowed_auto_sources": TRANSFER_PREFERRED_SOURCES | RECEIPT_ALLOWED_SOURCES,
		"kpi_bucket": "ZERO_RATE_BLOCKED",
		"user_review_status": "ZERO_RATE_PURPOSE_USER_REVIEW",
	}


def may_auto_propose_rate(purpose: str | None, source: str | None) -> bool:
	"""True when proposing ``source`` for ``purpose`` is allowed (not inventing)."""
	sem = purpose_semantics(purpose)
	policy = sem.get("policy")
	if policy == DOCUMENT_AUTHORITATIVE_ZERO_OK:
		return False
	allowed = sem.get("allowed_auto_sources") or frozenset()
	if not source:
		return False
	if policy == NO_INVENT_RATE:
		# Strict: only document-local / explicit receipt sources.
		return source in allowed and not source.startswith("previous_healthy") and source != "batch_inward"
	if policy == RECONSTRUCT_MANUFACTURE:
		# Strict manufacture: no previous_healthy / lone batch_inward invention.
		if source.startswith("previous_healthy") or source in ("batch_inward", "source_transfer_sle"):
			return False
		return source in allowed or source.split("+")[0] in {a.split("+")[0] for a in allowed}
	if policy in (RECONSTRUCT_FROM_SOURCE, RECONSTRUCT_REPACK):
		return source in allowed or source.split("+")[0] in {a.split("+")[0] for a in allowed}
	return source in allowed or source.split("+")[0] in {a.split("+")[0] for a in allowed}


def known_stock_entry_purposes() -> frozenset[str]:
	"""Purposes with an explicit registry entry (no generic Other bucket)."""
	return frozenset(PURPOSE_REGISTRY.keys())


def material_receipt_user_message() -> str:
	return (
		"Material Receipt has no authoritative upstream valuation source. "
		"Historical Repair will not invent a valuation rate. Review the receipt "
		"and enter/correct the appropriate valuation rate if required."
	)
