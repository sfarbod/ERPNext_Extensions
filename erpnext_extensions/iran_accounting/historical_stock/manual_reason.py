# Copyright (c) 2026, ERPNext Extensions contributors
"""Machine-readable MANUAL reason codes for Wrong Rate (v5.3.0 Phase 5).

MANUAL is never a terminal catch-all — every row must carry ``manual_reason``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

# Canonical reason codes (Phase 5 contract).
MANUAL_MISSING_SOURCE_RATE = "MANUAL_MISSING_SOURCE_RATE"
MANUAL_NEGATIVE_STOCK_HISTORY = "MANUAL_NEGATIVE_STOCK_HISTORY"
MANUAL_AMBIGUOUS_CHRONOLOGY = "MANUAL_AMBIGUOUS_CHRONOLOGY"
MANUAL_MANUFACTURE_DEPENDENCY = "MANUAL_MANUFACTURE_DEPENDENCY"
MANUAL_POSTING_ORDER_DEPENDENCY = "MANUAL_POSTING_ORDER_DEPENDENCY"
MANUAL_I4_DEPENDENCY = "MANUAL_I4_DEPENDENCY"
MANUAL_CONVERTED_DATA = "MANUAL_CONVERTED_DATA"
MANUAL_NO_AUTHORITATIVE_SOURCE = "MANUAL_NO_AUTHORITATIVE_SOURCE"
MANUAL_MATCHED_BUT_CORRUPT = "MANUAL_MATCHED_BUT_CORRUPT"
MANUAL_UPSTREAM_ZERO_RATE = "MANUAL_UPSTREAM_ZERO_RATE"
MANUAL_UPSTREAM_WRONG_RATE = "MANUAL_UPSTREAM_WRONG_RATE"
MANUAL_TRANSFER_PROPAGATION = "MANUAL_TRANSFER_PROPAGATION"
MANUAL_FAILED_RIV_PARTIAL = "MANUAL_FAILED_RIV_PARTIAL"
MANUAL_AMBIGUOUS_BATCH = "MANUAL_AMBIGUOUS_BATCH"
MANUAL_TOOL_LIMIT = "MANUAL_TOOL_LIMIT"
MANUAL_DOWNSTREAM_SYMPTOM = "MANUAL_DOWNSTREAM_SYMPTOM"
MANUAL_HISTORICAL_ONLY = "MANUAL_HISTORICAL_ONLY"
MANUAL_RATE_SOURCES_DISAGREE = "MANUAL_RATE_SOURCES_DISAGREE"
MANUAL_POISONED_OPENING = "MANUAL_POISONED_OPENING"
MANUAL_WAREHOUSE_ESCALATION = "MANUAL_WAREHOUSE_ESCALATION"
MANUAL_UNSPECIFIED = "MANUAL_UNSPECIFIED"

# Lane suggestion after reason assignment.
LANE_USER = "USER_ACTION_REQUIRED"
LANE_TOOL = "TOOL_LIMIT"
LANE_WAITING = "WAITING_UPSTREAM"
LANE_DOWNSTREAM = "DOWNSTREAM_SYMPTOM"
LANE_HISTORICAL = "HISTORICAL_ONLY"
LANE_DETERMINISTIC = "DETERMINISTIC_RECONSTRUCTABLE"


def _pz(row: dict) -> str | None:
	p = row.get("patient_zero") or row.get("root_patient_zero")
	if isinstance(p, dict):
		return p.get("voucher_no") or p.get("voucher")
	if p:
		return str(p)
	return row.get("required_prerequisite") or row.get("prerequisite")


def _voucher(row: dict) -> str | None:
	return row.get("voucher") or row.get("voucher_no") or row.get("name")


def classify_wrong_manual_reason(row: dict) -> dict:
	"""Stamp ``manual_reason`` + ``manual_lane`` on a Wrong Rate MANUAL row."""
	ps = str(row.get("planner_status") or row.get("status") or "")
	flags = set(row.get("flags") or [])
	if row.get("flag"):
		flags.add(row["flag"])
	msg = " ".join(
		str(x or "")
		for x in (
			row.get("message"),
			row.get("blocked_because"),
			row.get("dependency"),
			row.get("reason"),
			ps,
		)
	).lower()
	purpose = str(row.get("purpose") or "")
	src = str(row.get("source_of_truth") or row.get("rate_source") or row.get("source") or "")
	conf = str(row.get("confidence") or "")
	pz = _pz(row)
	v = _voucher(row)

	reason = MANUAL_UNSPECIFIED
	lane = LANE_TOOL

	if "MATCHED_BUT_CORRUPT" in flags:
		reason = MANUAL_MATCHED_BUT_CORRUPT
		lane = LANE_DETERMINISTIC if conf in ("EXACT", "LIKELY") else LANE_TOOL
	elif ps in ("RATE_POISONED_OPENING",) or "poison" in msg:
		reason = MANUAL_POISONED_OPENING
		lane = LANE_WAITING
	elif ps in ("RATE_WAREHOUSE_ESCALATION",) or "escalat" in msg:
		reason = MANUAL_WAREHOUSE_ESCALATION
		lane = LANE_TOOL
	elif "i4" in msg or "leftover" in msg or "qty_after" in msg:
		reason = MANUAL_I4_DEPENDENCY
		lane = LANE_WAITING
	elif "posting" in msg and "order" in msg:
		reason = MANUAL_POSTING_ORDER_DEPENDENCY
		lane = LANE_WAITING
	elif "negative" in msg and "stock" in msg:
		reason = MANUAL_NEGATIVE_STOCK_HISTORY
		lane = LANE_USER
	elif "chronolog" in msg or "ambiguous" in msg and ("time" in msg or "order" in msg):
		reason = MANUAL_AMBIGUOUS_CHRONOLOGY
		lane = LANE_USER if "cannot" in msg or "unproven" in msg else LANE_TOOL
	elif "batch" in msg and ("ambiguous" in msg or "disagree" in msg):
		reason = MANUAL_AMBIGUOUS_BATCH
		lane = LANE_USER
	elif purpose == "Manufacture" or "manufacture" in msg:
		reason = MANUAL_MANUFACTURE_DEPENDENCY
		lane = LANE_WAITING if pz and pz != v else LANE_TOOL
	elif purpose in (
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	) or "transfer" in msg:
		reason = MANUAL_TRANSFER_PROPAGATION
		lane = LANE_WAITING if pz and pz != v else LANE_TOOL
	elif "zero" in msg and "rate" in msg:
		reason = MANUAL_UPSTREAM_ZERO_RATE
		lane = LANE_WAITING
	elif "riv" in msg or "repost" in msg:
		reason = MANUAL_FAILED_RIV_PARTIAL
		lane = LANE_WAITING
	elif "converted" in msg or "migrat" in msg:
		reason = MANUAL_CONVERTED_DATA
		lane = LANE_USER
	elif "disagree" in msg or conf == "AMBIGUOUS":
		reason = MANUAL_RATE_SOURCES_DISAGREE
		lane = LANE_TOOL
	elif not src or src in ("manual", "none", "unknown"):
		if purpose == "Material Receipt":
			reason = MANUAL_NO_AUTHORITATIVE_SOURCE
			lane = LANE_USER
		else:
			reason = MANUAL_MISSING_SOURCE_RATE
			lane = LANE_TOOL
	elif pz and v and pz != v:
		reason = MANUAL_DOWNSTREAM_SYMPTOM
		lane = LANE_DOWNSTREAM
	elif "historical" in msg or "superseded" in msg:
		reason = MANUAL_HISTORICAL_ONLY
		lane = LANE_HISTORICAL
	else:
		reason = MANUAL_TOOL_LIMIT
		lane = LANE_TOOL

	row["manual_reason"] = reason
	row["manual_lane"] = lane
	# Keep legacy status but never leave reason blank.
	if not row.get("zero_reason") and not row.get("wrong_reason"):
		row["wrong_reason"] = reason
	return row


def summarize_manual_groups(rows: list[dict]) -> dict[str, Any]:
	by_reason: Counter = Counter()
	by_lane: Counter = Counter()
	roots = set()
	identities = set()
	pz_set = set()
	samples: dict[str, list] = defaultdict(list)

	for r in rows or []:
		if not r.get("manual_reason"):
			classify_wrong_manual_reason(r)
		reason = r.get("manual_reason") or MANUAL_UNSPECIFIED
		lane = r.get("manual_lane") or LANE_TOOL
		by_reason[reason] += 1
		by_lane[lane] += 1
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse") or r.get("s_warehouse") or r.get("t_warehouse")
		if item and wh:
			identities.add((item, wh))
		pz = _pz(r) or _voucher(r)
		if pz:
			roots.add(pz)
			pz_set.add(pz)
		if len(samples[reason]) < 3:
			samples[reason].append(
				{
					"voucher": _voucher(r),
					"purpose": r.get("purpose"),
					"item": item,
					"warehouse": wh,
					"planner": r.get("planner_status"),
					"confidence": r.get("confidence"),
					"source": r.get("source_of_truth") or r.get("rate_source"),
					"lane": lane,
					"patient_zero": _pz(r),
					"flag": r.get("flag"),
					"message": (r.get("message") or "")[:160],
				}
			)

	return {
		"by_reason": dict(by_reason.most_common()),
		"by_lane": dict(by_lane.most_common()),
		"root_chains": len(roots),
		"unique_item_warehouse": len(identities),
		"unique_patient_zero": len(pz_set),
		"samples": {k: v for k, v in list(samples.items())[:20]},
	}
