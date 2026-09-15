# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 Campaign clustering + SAFE_GROUP types (read/apply helpers)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from frappe.utils import flt

SAFE_GROUP = "SAFE_GROUP"
SAFE_SEQUENTIAL_GROUP = "SAFE_SEQUENTIAL_GROUP"
UNSAFE_GROUP = "UNSAFE_GROUP"


def identity_key(row) -> tuple:
	item = row.get("item") or row.get("item_code")
	wh = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	return (item or "", wh or "")


def patient_key(row) -> str:
	pz = row.get("patient_zero")
	if isinstance(pz, dict):
		return str(pz.get("voucher_no") or "")
	return str(pz or row.get("root_patient_zero") or row.get("voucher") or "")


def cluster_independent_roots(rows: list[dict], *, max_cluster=20) -> dict:
	"""Build independent identity clusters for EXACT/READY roots.

	A SAFE_GROUP may only contain roots with distinct (item, warehouse) and
	distinct patient_zero vouchers (no shared unresolved dependency).
	"""
	ready = []
	for r in rows or []:
		ps = str(r.get("planner_status") or r.get("status") or "")
		conf = str(r.get("confidence") or "")
		if not (r.get("eligible") or "READY" in ps):
			continue
		if conf and conf not in ("EXACT", ""):
			# allow EXACT-only for rate classes; I4 uses EXACT via eligible
			if conf != "EXACT" and r.get("topic") in ("ZERO_RATE", "WRONG_RATE"):
				continue
		ready.append(r)

	# Group by patient_zero first
	by_pz = defaultdict(list)
	for r in ready:
		by_pz[patient_key(r) or f"self:{r.get('voucher')}"].append(r)

	# Independent = single-identity patient zeros (or self-PZ)
	independent = []
	sequential = []
	unsafe = []
	for pz, members in by_pz.items():
		idents = {identity_key(m) for m in members}
		if len(members) == 1 and len(idents) == 1:
			independent.append(members[0])
		elif len(idents) == 1:
			# same identity, multiple vouchers → sequential oldest-first
			sequential.append(
				{
					"group_class": SAFE_SEQUENTIAL_GROUP,
					"patient_zero": pz,
					"identity": list(idents)[0],
					"roots": sorted(members, key=lambda x: str(x.get("posting_datetime") or x.get("posting_date") or "")),
					"n_roots": len(members),
					"repair_order": [
						r.get("voucher")
						for r in sorted(members, key=lambda x: str(x.get("posting_datetime") or x.get("posting_date") or ""))
					],
				}
			)
		else:
			# Shared PZ across identities: only the PZ voucher itself may enter a
			# SAFE pack (one identity). Downstream identities wait.
			pz_roots = [m for m in members if str(m.get("voucher") or m.get("voucher_no") or "") == str(pz)]
			if pz_roots:
				root = sorted(pz_roots, key=lambda x: str(x.get("posting_datetime") or x.get("posting_date") or ""))[0]
				independent.append(root)
				remaining = [m for m in members if m is not root]
				if remaining:
					unsafe.append(
						{
							"group_class": UNSAFE_GROUP,
							"patient_zero": pz,
							"identities": list({identity_key(m) for m in remaining}),
							"roots": remaining,
							"reason": "waiting on shared patient zero — PZ promoted separately",
						}
					)
			else:
				unsafe.append(
					{
						"group_class": UNSAFE_GROUP,
						"patient_zero": pz,
						"identities": list(idents),
						"roots": members,
						"reason": "shared patient zero across multiple identities; PZ not in READY set",
					}
				)

	# Pack independent roots into SAFE_GROUPs of size <= max_cluster
	# Also ensure no duplicate identities across the pack
	groups = []
	used_id = set()
	pack = []
	for r in sorted(independent, key=lambda x: str(x.get("posting_datetime") or "")):
		ik = identity_key(r)
		if ik in used_id or not ik[0]:
			continue
		used_id.add(ik)
		pack.append(r)
		if len(pack) >= max_cluster:
			groups.append(_safe_group(pack))
			pack = []
	if pack:
		groups.append(_safe_group(pack))

	groups.extend(sequential)
	groups.extend(unsafe)
	return {
		"independent_root_count": len(independent),
		"safe_groups": [g for g in groups if g.get("group_class") == SAFE_GROUP],
		"sequential_groups": [g for g in groups if g.get("group_class") == SAFE_SEQUENTIAL_GROUP],
		"unsafe_groups": [g for g in groups if g.get("group_class") == UNSAFE_GROUP],
		"smallest_safe_group": (sorted([g for g in groups if g.get("group_class") == SAFE_GROUP], key=lambda g: g["n_roots"]) or [None])[0],
		"recommended_first_group": _pick_first(groups),
	}


def _safe_group(roots: list[dict]) -> dict:
	sql = sum(int(r.get("sql_updates") or r.get("sql_updates_estimate") or 1) for r in roots)
	replay = sum(int(r.get("replay_count") or 0) for r in roots)
	return {
		"group_id": f"SAFE-{datetime.utcnow().strftime('%H%M%S')}-{len(roots)}",
		"group_class": SAFE_GROUP,
		"n_roots": len(roots),
		"repair_order": [r.get("voucher") for r in roots],
		"affected_identities": [{"item": identity_key(r)[0], "warehouse": identity_key(r)[1]} for r in roots],
		"estimated_sql_updates": sql,
		"estimated_sle_replay": replay,
		"estimated_runtime_seconds": round(len(roots) * 0.5 + replay * 0.02, 2),
		"risk": "LOW",
		"roots": roots,
	}


def _pick_first(groups):
	safes = [g for g in groups if g.get("group_class") == SAFE_GROUP]
	if safes:
		sized = sorted(safes, key=lambda g: (abs(g["n_roots"] - 12), g["n_roots"]))
		return sized[0]
	# Fall back: single-root from SAFE_SEQUENTIAL (oldest first)
	seqs = [g for g in groups if g.get("group_class") == SAFE_SEQUENTIAL_GROUP and g.get("roots")]
	if seqs:
		g = sorted(seqs, key=lambda x: str((x.get("roots") or [{}])[0].get("posting_datetime") or ""))[0]
		root = g["roots"][0]
		return _safe_group([root])
	return None


def classify_group(roots: list[dict]) -> str:
	idents = [identity_key(r) for r in roots]
	# Same identity, multiple vouchers → sequential (even if shared PZ)
	if len(set(idents)) == 1 and len(roots) > 1:
		return SAFE_SEQUENTIAL_GROUP
	if len(idents) != len(set(idents)):
		return UNSAFE_GROUP
	pzs = [patient_key(r) for r in roots if patient_key(r)]
	if len(pzs) != len(set(pzs)):
		# duplicate PZ across distinct identities
		return UNSAFE_GROUP
	return SAFE_GROUP
