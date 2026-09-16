# Copyright (c) 2026, ERPNext Extensions contributors
"""Confidence scoring for assisted recovery rate candidates."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import RATE_EPS
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	ASSISTED_READY,
	DEFAULT_ASSISTED_THRESHOLD,
	NO_EVIDENCE,
	OPERATOR_DECISION,
	SOURCE_WEIGHTS,
)


def score_rate_candidates(
	sources: dict[str, float],
	*,
	threshold: float = DEFAULT_ASSISTED_THRESHOLD,
	tol: float | None = None,
) -> dict:
	"""Cluster weighted sources and return ranked options with confidence.

	Returns ASSISTED_READY when top confidence ≥ threshold and ≥1 alternative exists,
	or a unique-cluster READY-like signal when only one cluster has all the weight.
	"""
	clean = {k: flt(v) for k, v in (sources or {}).items() if k != "bin" and abs(flt(v)) > RATE_EPS}
	if not clean:
		return {
			"outcome": NO_EVIDENCE,
			"confidence": 0.0,
			"recommended": None,
			"options": [],
			"reason": "No nonzero reconstruction sources found",
		}

	# Cluster by absolute rate within tolerance
	clusters: list[dict] = []
	for name, rate in clean.items():
		w = float(SOURCE_WEIGHTS.get(name, 0.2))
		abs_rate = abs(rate)
		local_tol = tol if tol is not None else max(1.0, abs_rate * 1e-6)
		placed = False
		for c in clusters:
			if abs(c["rate"] - abs_rate) <= local_tol:
				c["weight"] += w
				c["sources"].append(name)
				# Prefer higher-weight source as cluster label rate
				if w >= c["top_weight"]:
					c["rate"] = abs_rate
					c["top_source"] = name
					c["top_weight"] = w
				placed = True
				break
		if not placed:
			clusters.append(
				{
					"rate": abs_rate,
					"weight": w,
					"sources": [name],
					"top_source": name,
					"top_weight": w,
				}
			)

	total = sum(c["weight"] for c in clusters) or 1.0
	clusters.sort(key=lambda c: (-c["weight"], -c["top_weight"]))

	# Drop noise clusters whose weight is <10% of the leader — weak outliers
	# (e.g. lone version) must not veto a strong purchase/SLE/batch consensus.
	if len(clusters) > 1:
		leader_w = clusters[0]["weight"]
		kept = [c for c in clusters if c["weight"] >= leader_w * 0.15 or c is clusters[0]]
		# Always keep the leader; keep peers that are material
		if len(kept) < len(clusters):
			clusters = kept
			total = sum(c["weight"] for c in clusters) or 1.0

	options = []
	for c in clusters:
		options.append(
			{
				"rate": c["rate"],
				"confidence": round(c["weight"] / total, 6),
				"sources": c["sources"],
				"top_source": c["top_source"],
				"weight": round(c["weight"], 4),
			}
		)

	top = options[0]
	# Unique mathematical answer — all weight in one cluster
	if len(options) == 1:
		return {
			"outcome": "READY_CANDIDATE",
			"confidence": 1.0,
			"recommended": top,
			"options": options,
			"reason": f"Unique reconstruction cluster from {top['sources']}",
			"threshold": threshold,
		}
	if top["confidence"] + 1e-12 >= float(threshold):
		return {
			"outcome": ASSISTED_READY,
			"confidence": top["confidence"],
			"recommended": top,
			"options": options,
			"reason": (
				f"Top candidate {top['rate']} at {top['confidence']:.1%} "
				f"(threshold {threshold:.0%}); requires operator confirm"
			),
			"threshold": threshold,
		}
	# Mid confidence — operator choice between comparable options
	if top["confidence"] >= 0.55:
		return {
			"outcome": OPERATOR_DECISION,
			"confidence": top["confidence"],
			"recommended": top,
			"options": options,
			"reason": "Multiple plausible rates; confidence below assisted threshold",
			"threshold": threshold,
		}
	return {
		"outcome": OPERATOR_DECISION,
		"confidence": top["confidence"],
		"recommended": top,
		"options": options,
		"reason": "Low-confidence multi-source disagreement",
		"threshold": threshold,
	}
