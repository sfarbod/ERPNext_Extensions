# Copyright (c) 2026, ERPNext Extensions contributors
"""Estimate downstream repairs unlocked if a residual root is solved."""

from __future__ import annotations

from collections import defaultdict


def estimate_unlock_impact(root: dict, universe: list[dict]) -> dict:
	"""Rank by recovery impact (fan-out), not raw issue count."""
	root_v = root.get("voucher") or root.get("voucher_no")
	item = root.get("item") or root.get("item_code")
	warehouse = root.get("warehouse")

	def _pz(r):
		p = r.get("patient_zero")
		return p.get("voucher_no") if isinstance(p, dict) else p

	waiting = 0
	same_identity = 0
	same_pz = 0
	for r in universe or []:
		ps = str(r.get("planner_status") or r.get("status") or "")
		is_waiting = "WAITING" in ps or ps in ("RATE_AMBIGUOUS", "AMBIGUOUS", "RATE_MANUAL", "MANUAL")
		if not is_waiting:
			continue
		if _pz(r) == root_v or r.get("voucher") == root_v:
			same_pz += 1
			waiting += 1
			continue
		if item and warehouse and r.get("item") == item and r.get("warehouse") == warehouse:
			same_identity += 1
			waiting += 1

	topic_w = {
		"WAREHOUSE": 50,
		"POSTING_ORDER": 40,
		"ZERO_RATE": 30,
		"WRONG_RATE": 25,
		"I4_LEFTOVER": 20,
		"I4_LEFTOVER_REPAIR": 20,
		"GL": 10,
		"FAILED_RIV": 5,
	}
	topic = str(root.get("topic") or "")
	score = same_pz * 10 + same_identity * 3 + topic_w.get(topic, 1)
	return {
		"root": root_v,
		"item": item,
		"warehouse": warehouse,
		"unlocked_estimate": same_pz + max(0, same_identity // 2),
		"waiting_same_pz": same_pz,
		"waiting_same_identity": same_identity,
		"score": score,
	}


def rank_roots(roots: list[dict], universe: list[dict]) -> list[dict]:
	ranked = []
	for r in roots or []:
		imp = estimate_unlock_impact(r, universe)
		ranked.append({**r, "impact": imp})
	ranked.sort(key=lambda x: (-(x.get("impact") or {}).get("score", 0), x.get("voucher") or ""))
	return ranked


def fanout_index(universe: list[dict]) -> dict:
	"""Map patient-zero voucher → dependent count for quick lookups."""
	idx = defaultdict(int)
	for r in universe or []:
		p = r.get("patient_zero")
		pz = p.get("voucher_no") if isinstance(p, dict) else p
		if pz:
			idx[pz] += 1
	return dict(idx)
