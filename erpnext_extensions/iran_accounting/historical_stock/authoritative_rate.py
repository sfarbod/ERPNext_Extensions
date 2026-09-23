# Copyright (c) 2026, ERPNext Extensions contributors
"""Authoritative-rate validation for Historical Repair Master Plan V2 (5.3.0).

Prevents false ``RATE_REBUILD_COMPLETE`` / ``already_valued`` when Stock Entry
Detail and SLE agree on a poisoned rate (negative, non-finite, or exploded).

Matching corrupt rates are not "complete" — they are a valuation integrity defect.
"""

from __future__ import annotations

import math

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	POISON_RATE,
	RATE_EPS,
	STATUS_MANUAL_REVIEW,
	STATUS_RATE_REBUILD_COMPLETE,
	STATUS_RECONSTRUCTABLE,
	STATUS_VALUATION_POISON_DEPENDENCY,
)


def is_finite_rate(rate) -> bool:
	try:
		value = float(rate)
	except (TypeError, ValueError):
		return False
	return math.isfinite(value)


def is_poison_rate(rate, *, poison_ceiling: float | None = None) -> bool:
	"""True when rate is non-finite, or magnitude exceeds the poison ceiling."""
	ceiling = float(poison_ceiling if poison_ceiling is not None else POISON_RATE)
	if not is_finite_rate(rate):
		return True
	return abs(flt(rate)) >= ceiling


def is_negative_inbound_rate(rate) -> bool:
	"""Inbound / FG rates must not be negative (I1 surface)."""
	if not is_finite_rate(rate):
		return True
	return flt(rate) < -RATE_EPS


def rate_integrity_reason(rate, *, allow_zero: bool = False, allow_negative: bool = False) -> str | None:
	"""Return a short blocker tag, or None when the rate is authoritative-healthy."""
	if not is_finite_rate(rate):
		return "NON_FINITE_RATE"
	value = flt(rate)
	if is_poison_rate(value):
		return "EXPLODED_RATE"
	if not allow_negative and value < -RATE_EPS:
		return "NEGATIVE_RATE"
	if not allow_zero and abs(value) <= RATE_EPS:
		return "ZERO_RATE"
	return None


def is_authoritative_healthy_rate(rate, *, allow_zero: bool = False, allow_negative: bool = False) -> bool:
	return rate_integrity_reason(rate, allow_zero=allow_zero, allow_negative=allow_negative) is None


def refuse_false_rate_rebuild_complete(
	*,
	current_rate,
	expected_rate=None,
	source: str | None = None,
	status: str | None = None,
	allow_zero: bool = False,
) -> dict | None:
	"""If a COMPLETE/already_valued claim is based on poisoned rates, return a blocker.

	Returns ``None`` when the complete claim is valid.
	"""
	source = str(source or "")
	status = str(status or "")
	claims_complete = (
		status == STATUS_RATE_REBUILD_COMPLETE
		or source in ("already_valued", "document_authoritative")
		or "already_valued" in source
	)
	if not claims_complete:
		return None

	cur = flt(current_rate) if current_rate is not None else 0.0
	exp = flt(expected_rate) if expected_rate is not None else cur
	cur_reason = rate_integrity_reason(cur, allow_zero=allow_zero, allow_negative=False)
	exp_reason = rate_integrity_reason(exp, allow_zero=allow_zero, allow_negative=False)
	if not cur_reason and not exp_reason:
		return None

	reason = cur_reason or exp_reason
	return {
		"false_complete": True,
		"blocker": reason,
		"status": STATUS_VALUATION_POISON_DEPENDENCY if reason == "EXPLODED_RATE" else STATUS_RECONSTRUCTABLE,
		"planner_hint": "FALSE_RATE_REBUILD_COMPLETE",
		"message": (
			f"FALSE_RATE_REBUILD_COMPLETE — rates match but are not authoritative "
			f"({reason}; current={cur}, expected={exp})"
		),
		"current_rate": cur,
		"expected_rate": exp,
		"source": source,
		"prior_status": status,
	}


def reclassify_false_complete_row(row: dict) -> dict:
	"""Mutate a scan/planner row that falsely claims RATE_REBUILD_COMPLETE."""
	out = dict(row or {})
	refusal = refuse_false_rate_rebuild_complete(
		current_rate=out.get("current_rate", out.get("current")),
		expected_rate=out.get("proposed_rate", out.get("expected")),
		source=out.get("source") or out.get("source_of_truth") or out.get("rate_source"),
		status=out.get("status"),
		allow_zero=bool(out.get("allow_zero_valuation_rate")),
	)
	if not refusal:
		return out
	out["false_rate_rebuild_complete"] = True
	out["status"] = refusal["status"]
	out["blocker"] = refusal["blocker"]
	out["message"] = refusal["message"]
	out["eligible"] = refusal["status"] == STATUS_RECONSTRUCTABLE
	# Force reconstruction — do not leave already_valued as source of truth.
	if out.get("source_of_truth") in ("already_valued", "document_authoritative"):
		out["source_of_truth"] = "requires_authoritative_reconstruction"
	if out.get("source") == "already_valued":
		out["source"] = "requires_authoritative_reconstruction"
	if out.get("rate_source") == "already_valued":
		out["rate_source"] = "requires_authoritative_reconstruction"
	if refusal["blocker"] in ("EXPLODED_RATE", "NON_FINITE_RATE", "NEGATIVE_RATE"):
		out["confidence"] = out.get("confidence") or "LIKELY"
		if refusal["blocker"] == "EXPLODED_RATE":
			out["status"] = STATUS_VALUATION_POISON_DEPENDENCY
			out["eligible"] = False
	return out


def detect_matched_but_corrupt(
	*,
	se_rate,
	sle_rate,
	expected_rate,
	rate_eps: float | None = None,
) -> dict | None:
	"""Rule / MATCHED_BUT_CORRUPT — SE Detail rate == SLE rate, but both disagree with native expected.

	Critical v5.3.0 behaviour: agreement between SE and SLE is not proof of health
	when an independently reconstructed expected rate differs.
	"""
	eps = float(rate_eps if rate_eps is not None else RATE_EPS)
	se_v = flt(se_rate)
	sle_v = flt(sle_rate)
	exp_v = flt(expected_rate) if expected_rate is not None else None
	if exp_v is None:
		return None
	if abs(se_v - sle_v) > max(1.0, eps):
		return None
	# Both agree — check whether that agreed rate is corrupt vs expected.
	if abs(se_v - exp_v) <= max(1.0, eps):
		return None
	agreed_reason = rate_integrity_reason(se_v, allow_zero=False, allow_negative=False)
	return {
		"matched_but_corrupt": True,
		"flag": "MATCHED_BUT_CORRUPT",
		"se_rate": se_v,
		"sle_rate": sle_v,
		"expected_rate": exp_v,
		"difference": se_v - exp_v,
		"agreed_integrity": agreed_reason,
		"message": (
			f"MATCHED_BUT_CORRUPT — SE and SLE agree on {se_v} but native expected is {exp_v}"
		),
	}


def manufacture_after_rates_healthy(changed_rows: list, *, fg_negative_before: bool) -> bool:
	"""True when manufacture contract produces finite non-poison after-rates for changed rows.

	Used to promote fg_negative BEFORE state to EXACT when the deterministic
	Iran manufacture output contract yields a healthy AFTER snapshot.
	"""
	if not changed_rows:
		return not fg_negative_before
	for change in changed_rows:
		after = (change or {}).get("after") or {}
		rate = after.get("basic_rate")
		amount = after.get("amount")
		is_fg = bool(after.get("is_finished_item") or (change or {}).get("before", {}).get("is_finished_item"))
		if rate_integrity_reason(rate, allow_zero=not is_fg, allow_negative=False):
			return False
		if is_fg and rate_integrity_reason(amount, allow_zero=False, allow_negative=False):
			return False
	return True
