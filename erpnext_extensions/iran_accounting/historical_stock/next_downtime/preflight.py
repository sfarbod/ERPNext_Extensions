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

# Causal detail lists (optional). When present, numeric counters are derived
# from these so precision-dust / historical-only labels cannot stale-block L6.
_CAUSAL_LIST_KEYS = (
	"manufacturing_dependency_roots",
	"known_riv_poison_roots",
	"real_terminal_roots",
	"cross_time_exact_roots",
)


def _normalize_causal_counts(snap: dict) -> dict:
	"""Prefer causal root lists over stale scanner label counts."""
	out = dict(snap)
	if "manufacturing_dependency_roots" in out:
		roots = out.get("manufacturing_dependency_roots") or []
		out["manufacturing_dependency"] = len(roots)
	if "known_riv_poison_roots" in out:
		roots = out.get("known_riv_poison_roots") or []
		# Dust / legitimate openings do not block Full Repost.
		blocking = [
			r
			for r in roots
			if str((r or {}).get("status") or "")
			not in (
				"PRECISION_DUST",
				"LEGITIMATE",
				"LEGITIMATE_ORDER",
				# READY_I4 bridge — actionable via I4 replay, not a permanent poison gap.
				"I4_REPAIRABLE",
				# Pending Purchase Invoice — PR zero accepted until PI posts.
				"PENDING_PURCHASE_INVOICE_VALUATION",
				"LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING",
			)
		]
		out["known_riv_poison_root"] = len(blocking)
	if "real_terminal_roots" in out:
		out["real_terminal_leftover"] = len(out.get("real_terminal_roots") or [])
	if "cross_time_exact_roots" in out:
		out["cross_time_exact"] = len(out.get("cross_time_exact_roots") or [])
	return out


def full_repost_preflight(snapshot: dict | None = None) -> dict:
	"""Pure function: inspect a KPI/blocker snapshot. No writes.

	Evaluates CAUSAL blockers. Precision dust and historical-only labels must
	not block when the snapshot supplies causal root lists.
	"""
	snap = _normalize_causal_counts(dict(snapshot or {}))
	blockers = []
	detail = []
	for key, label in _BLOCKER_KEYS:
		val = snap.get(key)
		n = 0
		if isinstance(val, (list, tuple, set)):
			n = len(val)
		elif isinstance(val, (int, float)):
			n = int(val)
		elif val:
			n = 1
		if not n:
			continue
		entry = {"code": key, "label": label, "n": n}
		# Attach causal root samples when available
		list_key = {
			"manufacturing_dependency": "manufacturing_dependency_roots",
			"known_riv_poison_root": "known_riv_poison_roots",
			"real_terminal_leftover": "real_terminal_roots",
			"cross_time_exact": "cross_time_exact_roots",
		}.get(key)
		if list_key and snap.get(list_key):
			sample = []
			for r in list(snap[list_key])[:8]:
				if not isinstance(r, dict):
					continue
				sample.append(
					{
						"item": r.get("item") or r.get("item_code"),
						"warehouse": r.get("warehouse"),
						"causal_root": r.get("patient_zero")
						or r.get("earliest_voucher")
						or r.get("voucher"),
						"status": r.get("status") or r.get("class") or r.get("origin"),
						"economic_reason": (r.get("reason") or r.get("riv_reproduce_reason") or "")[
							:180
						],
						"expected_replay_failure": r.get("expected_replay_failure")
						or r.get("riv_reproduce_reason"),
					}
				)
			entry["causal_sample"] = sample
		blockers.append(entry)
		detail.append(entry)
	status = FULL_REPOST_BLOCKED if blockers else FULL_REPOST_READY
	return {
		"status": status,
		"blockers": blockers,
		"ready": status == FULL_REPOST_READY,
		"causal_detail": detail,
		"normalized_snapshot": {
			k: snap.get(k)
			for k in (
				"i1",
				"broken_bin",
				"broken_gl",
				"open_riv",
				"manufacturing_dependency",
				"known_riv_poison_root",
				"real_terminal_leftover",
				"cross_time_exact",
			)
		},
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
