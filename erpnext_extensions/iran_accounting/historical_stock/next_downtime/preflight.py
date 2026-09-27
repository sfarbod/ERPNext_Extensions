# Copyright (c) 2026, ERPNext Extensions contributors
"""FULL_REPOST_PREFLIGHT — L6 is blocked until known economic poisons are gone."""

from __future__ import annotations

FULL_REPOST_READY = "FULL_REPOST_READY"
FULL_REPOST_BLOCKED = "FULL_REPOST_BLOCKED"

_BLOCKER_KEYS = (
	("i1", "I1"),
	("broken_gl", "Broken GL"),
	("broken_bin", "Broken Bin"),
	("negative_stock", "Negative Stock"),
	("negative_rate", "Negative Rate"),
	("open_riv", "Open RIV"),
	("cross_time_exact", "CROSS_TIME exact/unsafe dependency"),
	("valued_source_zero_outgoing", "Unresolved valued-source zero outgoing"),
	("manufacturing_dependency", "Unresolved manufacturing dependency"),
	("real_terminal_leftover", "Unresolved real terminal leftover"),
	("known_riv_poison_root", "Known RIV poison root"),
	("p0_manual", "P0 MANUAL_BUSINESS_EVIDENCE_REQUIRED"),
	("p1_manual", "P1 MANUAL_BUSINESS_EVIDENCE_REQUIRED"),
)


def full_repost_preflight(snapshot: dict | None = None) -> dict:
	"""Pure function: inspect a KPI/blocker snapshot. No writes."""
	snap = dict(snapshot or {})
	blockers = []
	for key, label in _BLOCKER_KEYS:
		val = snap.get(key)
		if isinstance(val, (list, tuple, set)):
			if val:
				blockers.append({"code": key, "label": label, "n": len(val)})
		elif isinstance(val, (int, float)):
			if val:
				blockers.append({"code": key, "label": label, "n": int(val)})
		elif val:
			blockers.append({"code": key, "label": label, "n": 1})
	status = FULL_REPOST_BLOCKED if blockers else FULL_REPOST_READY
	return {
		"status": status,
		"blockers": blockers,
		"ready": status == FULL_REPOST_READY,
	}


def plan_full_repost(*, company: str, starting_boundary: str | None = None, estimated_identities: int = 0, risky_roots: list | None = None) -> dict:
	"""Dry description only. Does not predict MariaDB locks."""
	return {
		"company": company,
		"starting_boundary": starting_boundary,
		"estimated_identities": int(estimated_identities or 0),
		"risky_roots": list(risky_roots or []),
		"note": "Ordering is economic; lock/deadlock is not simulated.",
	}
