# Copyright (c) 2026, ERPNext Extensions contributors
"""Attempt to eliminate rate / warehouse mathematical ambiguity."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import CONFIDENCE_EXACT, RATE_EPS
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	ASSISTED_READY,
	DEFAULT_ASSISTED_THRESHOLD,
	OPERATOR_DECISION,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.confidence import (
	score_rate_candidates,
)


def resolve_rate_ambiguity(row: dict, evidence: dict, *, threshold: float = DEFAULT_ASSISTED_THRESHOLD) -> dict:
	"""Re-score with full evidence; promote unique answers toward READY."""
	sources = dict(evidence.get("rate_sources") or {})
	# Drop weak disagreeing version when strong purchase/reco/prev agree
	scored = score_rate_candidates(sources, threshold=threshold)
	outcome = scored.get("outcome")

	# Future / warehouse consistency: prefer cluster matching previous SLE qty path
	prev = evidence.get("previous_sle") or {}
	prev_rate = max(abs(flt(prev.get("valuation_rate"))), abs(flt(prev.get("incoming_rate"))))
	if prev_rate > RATE_EPS and scored.get("options"):
		for opt in scored["options"]:
			if abs(opt["rate"] - prev_rate) <= max(1.0, prev_rate * 1e-6):
				# Boost: if previous SLE agrees with a cluster, force unique if only that matches
				agreeing = [
					o
					for o in scored["options"]
					if abs(o["rate"] - prev_rate) <= max(1.0, prev_rate * 1e-6)
				]
				if len(agreeing) == 1 and agreeing[0]["confidence"] >= 0.5:
					return {
						"resolved": True,
						"promote_to": "READY",
						"confidence": CONFIDENCE_EXACT,
						"expected": agreeing[0]["rate"],
						"source": f"ambiguity_resolved+{agreeing[0]['top_source']}+previous_sle",
						"scored": scored,
						"reason": "Ambiguity eliminated — previous SLE agrees with single cluster",
					}

	# Purchase / RECO exact match with batch inward
	prs = evidence.get("purchase_receipts") or []
	bi = evidence.get("batch_inward") or {}
	if prs and bi and abs(flt(prs[0].get("rate")) - flt(bi.get("rate"))) <= 1:
		rate = abs(flt(prs[0]["rate"]))
		return {
			"resolved": True,
			"promote_to": "READY",
			"confidence": CONFIDENCE_EXACT,
			"expected": rate,
			"source": "purchase_receipt+batch_inward",
			"scored": scored,
			"reason": "Purchase Receipt agrees with batch inward",
		}

	if outcome == "READY_CANDIDATE":
		rec = scored["recommended"]
		return {
			"resolved": True,
			"promote_to": "READY",
			"confidence": CONFIDENCE_EXACT,
			"expected": rec["rate"],
			"source": "+".join(rec["sources"][:2]),
			"scored": scored,
			"reason": scored.get("reason"),
		}
	if outcome == ASSISTED_READY:
		rec = scored["recommended"]
		return {
			"resolved": False,
			"promote_to": ASSISTED_READY,
			"confidence": scored["confidence"],
			"expected": rec["rate"],
			"source": rec["top_source"],
			"scored": scored,
			"reason": scored.get("reason"),
		}
	return {
		"resolved": False,
		"promote_to": OPERATOR_DECISION,
		"confidence": scored.get("confidence") or 0.0,
		"expected": (scored.get("recommended") or {}).get("rate"),
		"source": (scored.get("recommended") or {}).get("top_source"),
		"scored": scored,
		"reason": scored.get("reason") or "Ambiguity unresolved",
	}


def resolve_warehouse_transfer_cycle(item: str, warehouse: str, reason: str = "") -> dict:
	"""Classify packaging oscillation leftovers — still ambiguity unless new evidence."""
	# Global solver already proved contradictory inbound/outbound role reversal.
	# Assisted phase documents the decision surface; does not invent an order.
	return {
		"resolved": False,
		"promote_to": OPERATOR_DECISION,
		"reason": reason
		or "Contradictory cross-warehouse transfer order cycle — operator must choose posting chronology",
		"choices": [
			{
				"id": "keep_current",
				"label": "Keep current posting order",
				"consequence": "Sister warehouse may remain negative; no SLE timestamp rewrite",
			},
			{
				"id": "prefer_inbound_first",
				"label": "Force inbound-before-outbound on both warehouses",
				"consequence": "May clear one warehouse and create shortage on the other",
			},
			{
				"id": "accept_shortage",
				"label": "Accept as real stock shortage / business gap",
				"consequence": "Classifies as REAL_STOCK_SHORTAGE; stops warehouse campaigns",
			},
		],
		"item": item,
		"warehouse": warehouse,
	}
