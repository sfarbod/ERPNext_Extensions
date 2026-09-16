# Copyright (c) 2026, ERPNext Extensions contributors
"""Global shared-voucher warehouse solver.

Packaging Stock Entries often touch many items. Per-item campaigns then fight
over the same voucher (±2s endless bumps). This solver:

1. Builds a connected component over pairs that share ANY Stock Entry voucher
2. Collects hard ordering constraints (inbound before outbound, supply before
   starved consumption)
3. Assigns each voucher ONE feasible posting time (prefer live times)
4. Simulates every touched identity once
5. Emits a single READY_GLOBAL_WAREHOUSE_SOLVER campaign — or refuses

Never re-bumps a voucher that already satisfies all constraints.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from time import perf_counter

from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	GLOBAL_WAREHOUSE_OSCILLATION,
	READY_GLOBAL_WAREHOUSE_SOLVER,
	READY_WAREHOUSE_CAMPAIGN,
	UNSAFE_CROSS_IDENTITY,
	WAREHOUSE_AMBIGUOUS,
	WAREHOUSE_CAMPAIGN_COMPLETE,
	WAREHOUSE_REAL_SHORTAGE,
)
from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
	CAMPAIGN_ELIGIBLE_OPTS,
	merge_proposed_times,
	verify_touched_identities_from_times,
	_drop_noop_moves,
	_moves_from_proposed,
)

READY_GLOBAL_WAREHOUSE_SOLVER = "READY_GLOBAL_WAREHOUSE_SOLVER"
GLOBAL_WAREHOUSE_OSCILLATION = "GLOBAL_WAREHOUSE_OSCILLATION"
MIN_BUMP_SECONDS = 1.0
# Micro-rebumps smaller than this after constraints are already met are refused.
OSCILLATION_EPS_SECONDS = 2.5


def solve_shared_voucher_universe(
	rows: list[dict] | None = None,
	*,
	company: str | None = None,
	scan: dict | None = None,
) -> dict:
	"""Discover connected shared-voucher components and solve each globally."""
	t0 = perf_counter()
	company = resolve_company(company)
	if rows is None:
		from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
			_load_posting_order_rows,
		)

		rows = _load_posting_order_rows(company, scan=scan)

	eligible = _eligible_pairs(rows)
	components = build_shared_voucher_components(eligible)
	solutions = []
	for comp in components:
		solutions.append(solve_component(comp))

	ready = [s for s in solutions if s.get("eligible")]
	complete = [s for s in solutions if s.get("planner_status") == WAREHOUSE_CAMPAIGN_COMPLETE]
	blocked = [s for s in solutions if not s.get("eligible") and s not in complete]

	return {
		"company": company,
		"elapsed_seconds": round(perf_counter() - t0, 3),
		"n_eligible_pairs": len(eligible),
		"n_components": len(components),
		"n_ready": len(ready),
		"n_complete": len(complete),
		"n_blocked": len(blocked),
		"solutions": solutions,
		"ready_solutions": ready,
		"message": (
			"Global shared-voucher solver — one graph per multi-item Stock Entry component. "
			"No per-item ±2s oscillation."
		),
	}


def build_shared_voucher_components(pairs: list[dict]) -> list[dict]:
	"""Union-find components linked by shared inbound/outbound/move vouchers."""
	n = len(pairs)
	parent = list(range(n))

	def find(i):
		while parent[i] != i:
			parent[i] = parent[parent[i]]
			i = parent[i]
		return i

	def union(a, b):
		ra, rb = find(a), find(b)
		if ra != rb:
			parent[rb] = ra

	voucher_owner: dict[str, int] = {}
	for i, p in enumerate(pairs):
		vouchers = _pair_vouchers(p)
		# Also link via multi-item Stock Entries already in the pair set
		for vn in vouchers:
			if vn in voucher_owner:
				union(i, voucher_owner[vn])
			else:
				voucher_owner[vn] = i

	# Expand: any Stock Entry that appears in a component and touches other
	# eligible pairs' items is already covered via shared voucher keys above.
	groups: dict[int, list[dict]] = defaultdict(list)
	for i, p in enumerate(pairs):
		groups[find(i)].append(p)

	components = []
	for members in groups.values():
		vouchers = sorted({v for p in members for v in _pair_vouchers(p)})
		idents = sorted(
			{
				(p.get("item") or p.get("item_code") or "", p.get("warehouse") or "")
				for p in members
			}
		)
		components.append(
			{
				"n_pairs": len(members),
				"n_identities": len(idents),
				"n_vouchers": len(vouchers),
				"vouchers": vouchers,
				"identities": [{"item": i, "warehouse": w} for i, w in idents if i and w],
				"pairs": members,
				"multi_item_vouchers": _multi_item_vouchers(vouchers),
			}
		)
	# Prefer larger components first
	components.sort(key=lambda c: (-c["n_pairs"], -c["n_vouchers"]))
	return components


def solve_component(component: dict) -> dict:
	"""Solve one connected component to a single global posting-order map."""
	pairs = list(component.get("pairs") or [])
	if not pairs:
		return {
			"ok": False,
			"eligible": False,
			"planner_status": WAREHOUSE_AMBIGUOUS,
			"reason": "empty component",
		}

	# Seed proposed times from all pairs (pair constraints)
	proposed, move_rows, from_dt, synth_notes = merge_proposed_times(pairs)
	constraints = _pair_constraints(pairs)

	# Contradictory transfer constraints (A before B AND B before A) → stop
	cycle = _constraint_cycle(constraints)
	if cycle:
		anchor = pairs[0]
		return {
			"ok": False,
			"eligible": False,
			"planner_status": WAREHOUSE_AMBIGUOUS,
			"reason": (
				"MATHEMATICAL_AMBIGUITY — contradictory cross-warehouse transfer order "
				f"cycle {cycle[0]} ⇄ {cycle[1]} (inbound/outbound roles reverse across warehouses)"
			),
			"required_action": "Operator decision — cannot satisfy both warehouses with one posting order",
			"campaign_id": f"WHGLOBAL-CYCLE-{abs(hash(cycle)) % 10_000_000:07d}",
			"solver": "global_shared_voucher",
			"n_pairs": len(pairs),
			"n_identities": component.get("n_identities"),
			"n_vouchers": component.get("n_vouchers"),
			"multi_item_vouchers": component.get("multi_item_vouchers"),
			"identities": component.get("identities"),
			"constraint_cycle": list(cycle),
			"moves": [],
			"proposed_times": {},
			"notes": synth_notes,
			"pairs": pairs,
			"item": anchor.get("item") or anchor.get("item_code"),
			"warehouse": anchor.get("warehouse"),
			"group_class": "UNSAFE_GROUP",
			"expected_sql": 0,
			"expected_replay": 0,
			"expected_runtime_seconds": 0,
			"expected_risk": "CRITICAL",
			"rollback_scope": "none",
			"dependency_depth": max(0, len(pairs) - 1),
			"blocker_class": "MATHEMATICAL_AMBIGUITY",
		}

	# Iteratively enforce supply-before-starvation without micro-rebumping
	solved, solve_notes, verification = _solve_constraints(
		proposed, from_dt, constraints, max_rounds=12
	)
	proposed = solved
	# Re-enforce pair constraints after expansion (never let expansion invert a pair)
	proposed, pair_notes = _enforce_pair_constraints(proposed, constraints)
	solve_notes = list(solve_notes) + pair_notes
	# If enforcement recreates a cycle conflict vs live multi-warehouse roles, detect
	if _constraint_cycle(constraints):
		pass  # already checked; constraints themselves are static
	move_rows = _moves_from_proposed(proposed, move_rows)
	proposed, move_rows, noop_notes = _drop_noop_moves(proposed, move_rows)
	notes = list(synth_notes) + list(solve_notes) + list(noop_notes)

	anchor = pairs[0]
	item = anchor.get("item") or anchor.get("item_code")
	warehouse = anchor.get("warehouse")
	campaign_id = f"WHGLOBAL-{abs(hash(tuple(component.get('vouchers') or []))) % 10_000_000:07d}-{len(pairs)}"

	if not proposed:
		# Constraints already satisfied on live ledger
		return {
			"ok": True,
			"eligible": False,
			"planner_status": WAREHOUSE_CAMPAIGN_COMPLETE,
			"reason": "global constraints already satisfied — no write needed",
			"required_action": "None",
			"campaign_id": campaign_id,
			"solver": "global_shared_voucher",
			"n_pairs": len(pairs),
			"n_identities": component.get("n_identities"),
			"n_vouchers": component.get("n_vouchers"),
			"multi_item_vouchers": component.get("multi_item_vouchers"),
			"identities": component.get("identities"),
			"moves": [],
			"proposed_times": {},
			"from_datetime": str(from_dt) if from_dt else None,
			"notes": notes,
			"pairs": pairs,
			"item": item,
			"warehouse": warehouse,
			"group_class": "SAFE_GROUP",
			"expected_sql": 0,
			"expected_replay": 0,
			"expected_runtime_seconds": 0,
			"expected_risk": "NONE",
			"rollback_scope": "savepoint_global_solver",
			"dependency_depth": max(0, len(pairs) - 1),
			"multi_identity": verification,
		}

	if not from_dt:
		from_candidates = [get_datetime(v) for v in proposed.values()]
		from_dt = min(from_candidates) if from_candidates else None

	# Re-verify after noop drop (only remaining moves)
	if proposed and from_dt:
		# Re-attach dropped anchors for simulation (live times for unmoved vouchers
		# are already in the ledger; sim only needs remapped keys)
		verification = verify_touched_identities_from_times(proposed, from_dt)

	if not verification or not verification.get("all_clear"):
		# Distinguish oscillation (only micro diffs left that don't clear) vs shortage
		status = UNSAFE_CROSS_IDENTITY
		reason = (verification or {}).get("reason") or "global simulation failed"
		if _is_micro_oscillation(proposed):
			status = GLOBAL_WAREHOUSE_OSCILLATION
			reason = (
				"GLOBAL_WAREHOUSE_OSCILLATION — remaining diffs are ≤"
				f"{OSCILLATION_EPS_SECONDS}s micro-rebumps that do not clear sisters; "
				"refusing endless loop"
			)
		elif verification and any(
			s.get("introduced") for s in (verification.get("identities") or []) if not s.get("clears")
		):
			# Still have introduced negatives after full solve → shortage/ambiguity
			failed = [s for s in (verification.get("identities") or []) if not s.get("clears")]
			if failed and all(s.get("introduced") for s in failed):
				status = WAREHOUSE_REAL_SHORTAGE
				reason = f"WAREHOUSE_REAL_SHORTAGE after global solve — {reason}"

		return {
			"ok": False,
			"eligible": False,
			"planner_status": status,
			"reason": reason,
			"required_action": "Do not auto-apply — operator / evidence required"
			if status != GLOBAL_WAREHOUSE_OSCILLATION
			else "Stop oscillation; leave ledger as-is",
			"campaign_id": campaign_id,
			"solver": "global_shared_voucher",
			"n_pairs": len(pairs),
			"n_identities": component.get("n_identities"),
			"n_vouchers": component.get("n_vouchers"),
			"multi_item_vouchers": component.get("multi_item_vouchers"),
			"identities": component.get("identities"),
			"moves": [
				{
					"document": m.get("document"),
					"old": str(m.get("old")) if m.get("old") else None,
					"new": str(m.get("new")) if m.get("new") else None,
					"synthesized": m.get("synthesized"),
				}
				for m in move_rows
			],
			"proposed_times": {k: str(v) for k, v in proposed.items()},
			"from_datetime": str(from_dt) if from_dt else None,
			"notes": notes,
			"pairs": pairs,
			"item": item,
			"warehouse": warehouse,
			"group_class": "UNSAFE_GROUP",
			"multi_identity": verification,
			"expected_sql": 0,
			"expected_replay": 0,
			"expected_runtime_seconds": 0,
			"expected_risk": "HIGH",
			"rollback_scope": "savepoint_global_solver",
			"dependency_depth": max(0, len(pairs) - 1),
		}

	# All clear with real moves
	row_count = sum(int(s.get("row_count") or 0) for s in (verification.get("identities") or []))
	sql = max(1, len(move_rows) + row_count)
	return {
		"ok": True,
		"eligible": True,
		"planner_status": READY_GLOBAL_WAREHOUSE_SOLVER,
		"reason": (
			f"READY_GLOBAL_WAREHOUSE_SOLVER — {len(pairs)} pairs, "
			f"{verification.get('n_identities')} identities, "
			f"{len(proposed)} moves, multi_item={len(component.get('multi_item_vouchers') or [])}"
		),
		"required_action": (
			"Apply global shared-voucher solve: one timestamp map + multi-identity MA replay"
		),
		"campaign_id": campaign_id,
		"solver": "global_shared_voucher",
		"n_pairs": len(pairs),
		"n_identities": verification.get("n_identities"),
		"n_vouchers": component.get("n_vouchers"),
		"multi_item_vouchers": component.get("multi_item_vouchers"),
		"identities": component.get("identities"),
		"moves": [
			{
				"document": m.get("document"),
				"old": str(m.get("old")) if m.get("old") else None,
				"new": str(m.get("new")) if m.get("new") else None,
				"synthesized": m.get("synthesized"),
			}
			for m in move_rows
		],
		"proposed_times": {k: str(v) for k, v in proposed.items()},
		"from_datetime": str(from_dt) if from_dt else None,
		"notes": notes,
		"pairs": pairs,
		"item": item,
		"warehouse": warehouse,
		"pair_inbounds": [p.get("inbound_document") for p in pairs],
		"pair_outbounds": [p.get("outbound_document") for p in pairs],
		"group_class": "SAFE_GROUP",
		"multi_identity": verification,
		"expected_sql": sql,
		"expected_replay": row_count,
		"expected_runtime_seconds": round(0.05 * max(1, row_count), 2),
		"expected_risk": "LOW",
		"rollback_scope": "savepoint_global_solver",
		"dependency_depth": max(0, len(pairs) - 1),
		# Compatibility with apply_warehouse_campaign
		"planner_status_compat": READY_WAREHOUSE_CAMPAIGN,
	}


def apply_global_solution(solution: dict, *, dry_run=True) -> dict:
	"""Apply a READY_GLOBAL_WAREHOUSE_SOLVER solution under one savepoint."""
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.campaign import (
		_assert_outbound_non_negative,
		_selective_gl,
		_update_posting_datetime,
	)
	from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
		replay_item_warehouse,
		sync_transfer_incoming_rates,
	)
	import frappe

	t0 = perf_counter()
	# Fresh re-solve to avoid stale plans
	fresh = solve_component(
		{
			"pairs": solution.get("pairs") or [],
			"n_identities": solution.get("n_identities"),
			"n_vouchers": solution.get("n_vouchers"),
			"vouchers": list((solution.get("proposed_times") or {}).keys()),
			"identities": solution.get("identities") or [],
			"multi_item_vouchers": solution.get("multi_item_vouchers") or [],
		}
	)
	if not fresh.get("eligible") or fresh.get("planner_status") != READY_GLOBAL_WAREHOUSE_SOLVER:
		return {
			"ok": False,
			"aborted": True,
			"dry_run": dry_run,
			"planner_status": fresh.get("planner_status"),
			"reason": fresh.get("reason"),
			"required_action": fresh.get("required_action"),
		}

	moves = fresh.get("moves") or []
	proposed = {m["document"]: get_datetime(m["new"]) for m in moves if m.get("document") and m.get("new")}
	from_dt = get_datetime(fresh.get("from_datetime"))
	multi = fresh.get("multi_identity") or {}

	if dry_run:
		return {
			"ok": True,
			"dry_run": True,
			"planner_status": READY_GLOBAL_WAREHOUSE_SOLVER,
			"campaign_id": fresh.get("campaign_id"),
			"n_moves": len(moves),
			"n_pairs": fresh.get("n_pairs"),
			"n_identities": multi.get("n_identities"),
			"expected_sql": fresh.get("expected_sql"),
			"message": "Dry run only — no writes",
		}

	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = f"whglob_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	checkpoint = {
		"savepoint": savepoint,
		"from_datetime": str(from_dt),
		"proposed_times": {k: str(v) for k, v in proposed.items()},
		"n_moves": len(moves),
		"campaign_id": fresh.get("campaign_id"),
	}
	try:
		for m in moves:
			_update_posting_datetime(m["document"], get_datetime(m["new"]))

		# Replay every cleared identity once (primaries first = component identities)
		primary_keys = {
			(i.get("item"), i.get("warehouse")) for i in (fresh.get("identities") or [])
		}
		idents = list(multi.get("identities") or [])
		idents.sort(key=lambda i: 0 if (i.get("item"), i.get("warehouse")) in primary_keys else 1)
		replays = []
		for ident in idents:
			if not ident.get("clears"):
				continue
			is_primary = (ident.get("item"), ident.get("warehouse")) in primary_keys
			row_count = int(ident.get("row_count") or 0)
			write_set = None if is_primary or row_count <= 250 else set(proposed)
			rep = replay_item_warehouse(
				ident["item"],
				ident["warehouse"],
				from_dt,
				ignore_inversion_artifacts=True,
				write_vouchers=write_set,
				allow_unrelated_poison=True,
				trust_simulated_series=True,
			)
			replays.append(
				{
					"item": ident["item"],
					"warehouse": ident["warehouse"],
					"ok": rep.get("ok"),
					"written": rep.get("written"),
				}
			)
			if not rep.get("ok"):
				frappe.db.rollback(save_point=savepoint)
				return {
					"ok": False,
					"aborted": True,
					"dry_run": False,
					"reason": f"replay failed on {ident['item']}: {rep.get('reason') or rep.get('status')}",
					"checkpoint": checkpoint,
					"replays": replays,
				}

		for vn in set(fresh.get("pair_inbounds") or []) | set(fresh.get("pair_outbounds") or []):
			if vn and frappe.db.exists("Stock Entry", vn):
				sync_transfer_incoming_rates(vn)

		for p in fresh.get("pairs") or []:
			_assert_outbound_non_negative(
				p.get("outbound_document"),
				p.get("item") or p.get("item_code"),
				p.get("warehouse"),
			)

		gl = _selective_gl(sorted(set(proposed)))
		frappe.db.commit()

		# Idempotency: re-solve must be COMPLETE / not eligible
		after = solve_component(
			{
				"pairs": fresh.get("pairs") or [],
				"n_identities": fresh.get("n_identities"),
				"n_vouchers": fresh.get("n_vouchers"),
				"vouchers": list(proposed.keys()),
				"identities": fresh.get("identities") or [],
				"multi_item_vouchers": fresh.get("multi_item_vouchers") or [],
			}
		)
		return {
			"ok": True,
			"dry_run": False,
			"planner_status": WAREHOUSE_CAMPAIGN_COMPLETE,
			"campaign_id": fresh.get("campaign_id"),
			"n_moves": len(moves),
			"n_pairs": fresh.get("n_pairs"),
			"replays": replays,
			"gl": gl,
			"checkpoint": checkpoint,
			"after_plan_status": after.get("planner_status"),
			"after_eligible": after.get("eligible"),
			"sql_updates_executed": sum(int(r.get("written") or 0) for r in replays)
			+ len(moves)
			+ int(gl.get("rebuilt") or 0),
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"riv": "NOT_INVOKED",
			"idempotent": not after.get("eligible"),
		}
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		return {"ok": False, "aborted": True, "dry_run": False, "error": str(exc), "checkpoint": checkpoint}
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def _eligible_pairs(rows: list[dict]) -> list[dict]:
	out = []
	for r in rows or []:
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		if opt not in CAMPAIGN_ELIGIBLE_OPTS:
			continue
		conf = str(r.get("confidence") or "")
		if conf and conf not in ("EXACT", ""):
			continue
		if not (r.get("inbound_document") and r.get("outbound_document")):
			continue
		if not (r.get("item") or r.get("item_code")) or not r.get("warehouse"):
			continue
		out.append(r)
	return out


def _pair_vouchers(p: dict) -> set[str]:
	vs = set()
	for k in ("inbound_document", "outbound_document"):
		if p.get(k):
			vs.add(p[k])
	for m in p.get("moves") or []:
		if m.get("document"):
			vs.add(m["document"])
	return vs


def _pair_constraints(pairs: list[dict]) -> list[tuple[str, str]]:
	"""Return (before, after) voucher constraints: before must post earlier.

	Also infers the reverse constraint on the sister warehouse of a transfer
	pair. If inbound posts +qty on W and -qty elsewhere, and outbound posts
	-qty on W and +qty elsewhere, the sister warehouse needs the opposite
	order — which is a mathematical contradiction for a single posting_datetime.
	"""
	out = []
	for p in pairs:
		inn = p.get("inbound_document")
		outb = p.get("outbound_document")
		wh = p.get("warehouse")
		if inn and outb and inn != outb:
			out.append((inn, outb))
			if wh and _is_opposite_transfer_pair(inn, outb, wh):
				# Sister warehouse requires reverse order → explicit cycle edge
				out.append((outb, inn))
	return out


def _is_opposite_transfer_pair(inbound: str, outbound: str, warehouse: str) -> bool:
	"""True when inbound is + on warehouse and outbound is - on warehouse,
	and both vouchers also post the opposite sign on some other warehouse."""
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT voucher_no, warehouse, SUM(actual_qty) AS qty
		FROM `tabStock Ledger Entry`
		WHERE voucher_no IN %s AND is_cancelled=0
		GROUP BY voucher_no, warehouse
		""",
		((inbound, outbound),),
		as_dict=True,
	)
	by_v = defaultdict(dict)
	for r in rows:
		by_v[r.voucher_no][r.warehouse] = float(r.qty or 0)
	in_map = by_v.get(inbound) or {}
	out_map = by_v.get(outbound) or {}
	in_here = in_map.get(warehouse, 0)
	out_here = out_map.get(warehouse, 0)
	if not (in_here > 0 and out_here < 0):
		return False
	# Opposite signs elsewhere
	in_elsewhere = any(w != warehouse and q < 0 for w, q in in_map.items())
	out_elsewhere = any(w != warehouse and q > 0 for w, q in out_map.items())
	return bool(in_elsewhere and out_elsewhere)


def _constraint_cycle(constraints: list[tuple[str, str]]) -> tuple[str, str] | None:
	"""Detect A→B and B→A contradictions (cross-warehouse transfer role reversal)."""
	fwd = set()
	for a, b in constraints:
		if (b, a) in fwd:
			return (a, b) if a < b else (b, a)
		fwd.add((a, b))
	return None


def _enforce_pair_constraints(proposed: dict, constraints: list[tuple[str, str]]):
	notes = []
	proposed = dict(proposed or {})
	for inn, outb in constraints:
		in_t = proposed.get(inn) or _live_time(inn)
		out_t = proposed.get(outb) or _live_time(outb)
		if in_t and out_t and get_datetime(out_t) <= get_datetime(in_t):
			proposed[outb] = get_datetime(in_t) + timedelta(seconds=MIN_BUMP_SECONDS)
			if inn not in proposed:
				proposed[inn] = get_datetime(in_t)
			notes.append(f"re-enforce pair {outb} after {inn}")
	return proposed, notes


def _multi_item_vouchers(vouchers: list[str]) -> list[str]:
	if not vouchers:
		return []
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT voucher_no, COUNT(DISTINCT item_code) AS n
		FROM `tabStock Ledger Entry`
		WHERE voucher_no IN %s AND is_cancelled=0
		GROUP BY voucher_no
		HAVING n > 1
		""",
		(tuple(vouchers),),
		as_dict=True,
	)
	return [r.voucher_no for r in rows]


def _solve_constraints(proposed, from_dt, pair_constraints, *, max_rounds=12):
	"""Enforce pair order + supply-before-starvation without endless micro-bumps."""
	import frappe

	proposed = {k: get_datetime(v) for k, v in (proposed or {}).items()}
	from_dt = get_datetime(from_dt) if from_dt else (min(proposed.values()) if proposed else None)
	notes = []
	verification = None
	if not from_dt or not proposed:
		return proposed, notes, verification

	# Enforce pair constraints first
	for inn, outb in pair_constraints:
		if inn not in proposed and outb not in proposed:
			continue
		in_t = proposed.get(inn)
		out_t = proposed.get(outb)
		if in_t is None:
			cur = _live_time(inn)
			if cur:
				proposed[inn] = cur
				in_t = cur
		if out_t is None:
			cur = _live_time(outb)
			if cur:
				proposed[outb] = cur
				out_t = cur
		if in_t and out_t and out_t <= in_t:
			proposed[outb] = in_t + timedelta(seconds=MIN_BUMP_SECONDS)
			notes.append(f"pair-order {outb} after {inn}")

	for round_i in range(max_rounds):
		verification = verify_touched_identities_from_times(proposed, from_dt)
		if verification.get("all_clear"):
			return proposed, notes, verification

		changed = False
		for s in verification.get("identities") or []:
			if s.get("clears"):
				continue
			for vn in s.get("introduced") or []:
				# Supply = positive actual_qty vouchers already in proposed map
				pos = frappe.db.sql(
					"""
					SELECT voucher_no FROM `tabStock Ledger Entry`
					WHERE item_code=%s AND warehouse=%s AND voucher_no IN %s
					  AND is_cancelled=0 AND actual_qty>0
					""",
					(s["item"], s["warehouse"], tuple(proposed.keys())),
					as_dict=True,
				)
				latest_supply = max(proposed.values())
				for p in pos:
					if p.voucher_no in proposed:
						latest_supply = max(latest_supply, proposed[p.voucher_no])
				target = latest_supply + timedelta(seconds=MIN_BUMP_SECONDS)
				cur_prop = proposed.get(vn)
				live = _live_time(vn)
				# Prefer live time if it already satisfies the constraint
				if live and live >= target:
					if cur_prop != live:
						proposed[vn] = live
						changed = True
						notes.append(f"R{round_i}: keep live {vn} (>= supply)")
					continue
				if cur_prop and cur_prop >= target:
					continue
				# Only move if improvement is meaningful (> oscillation eps from live)
				if live and abs((target - live).total_seconds()) <= OSCILLATION_EPS_SECONDS:
					# Micro-bump would not stabilize — pin to live and stop fighting
					proposed[vn] = live
					notes.append(f"R{round_i}: refuse micro-bump {vn} (≤{OSCILLATION_EPS_SECONDS}s)")
					changed = True
					continue
				proposed[vn] = target
				changed = True
				notes.append(f"R{round_i}: place {vn} after supply → {target}")

		if not changed:
			break

	verification = verify_touched_identities_from_times(proposed, from_dt)
	return proposed, notes, verification


def _live_time(voucher: str):
	import frappe

	if not voucher:
		return None
	row = frappe.db.sql(
		"""
		SELECT posting_datetime FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0
		ORDER BY posting_datetime LIMIT 1
		""",
		(voucher,),
	)
	return get_datetime(row[0][0]) if row else None


def _is_micro_oscillation(proposed: dict) -> bool:
	"""True when every remaining proposed change is a tiny bump from live time."""
	if not proposed:
		return False
	for doc, dt in proposed.items():
		live = _live_time(doc)
		if not live:
			return False
		if abs((get_datetime(dt) - live).total_seconds()) > OSCILLATION_EPS_SECONDS:
			return False
	return True
