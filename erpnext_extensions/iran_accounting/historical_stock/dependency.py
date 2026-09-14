# Copyright (c) 2026, ERPNext Extensions contributors
"""Single Historical Repair dependency walker.

Scan, Dashboard, Graph, Impact, Planner, and Repair must call this module.
No other surface may invent a different tree or repair order.
"""

from __future__ import annotations

import re

from frappe.utils import cint

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_MANUAL_REVIEW,
	STATUS_VALUATION_POISON_DEPENDENCY,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	INVERSION_ARTIFACT_POISONS,
	sle_poison_reason,
)

MAX_DEPTH = 12
MS_PER_STEP = 50
VOUCHER_RE = re.compile(r"MAT-(?:STE|SLE|RECO)-\d{4}-\d+(?:-\d+)?")

PLAN_NO_REPAIR_PATH = "NO_REPAIR_PATH"
PLAN_READY_BATCH_SCOPED = "READY_BATCH_SCOPED_REPAIR"

STOP_MANUAL = "Manual"
STOP_AMBIGUOUS = "Ambiguous"
STOP_EXTERNAL = "External dependency"
STOP_CROSS_ITEM = "Cross item"
STOP_CROSS_WAREHOUSE = "Cross warehouse"
STOP_CIRCULAR = "Circular dependency"


def voucher_of(row: dict | None) -> str | None:
	row = row or {}
	for key in ("voucher", "voucher_no", "outbound_document", "inbound_document"):
		val = row.get(key)
		if isinstance(val, dict):
			val = val.get("voucher_no") or val.get("voucher")
		if val:
			return str(val)
	return None


def patient_of(row: dict | None) -> str | None:
	pz = (row or {}).get("patient_zero")
	if isinstance(pz, dict):
		return pz.get("voucher_no") or pz.get("voucher")
	if pz:
		return str(pz)
	return None


def resolve_dependencies(row: dict | None, *, cache: dict | None = None, _decision: dict | None = None) -> dict:
	"""Walk blockers until the first repairable root, a terminal stop, or a cycle."""
	cache = cache if cache is not None else {}
	row = dict(row or {})
	walked = _walk(row, cache=cache, stack=[], depth=0, decision=_decision)
	nodes = walked.get("nodes") or []
	tree = walked.get("tree") or _leaf_node(row, _decision or {})
	real_nodes = [n for n in nodes if n.get("status") != PLAN_NO_REPAIR_PATH]
	repair_order = []
	seen = set()
	for n in reversed(real_nodes):
		v = n.get("voucher")
		if v and v not in seen:
			seen.add(v)
			repair_order.append(v)
	root = real_nodes[-1] if real_nodes else (nodes[-1] if nodes else tree)
	root_status = (root or {}).get("status") or ""
	root_voucher = (root or {}).get("voucher")
	circular = bool(walked.get("circular"))
	immediate = None
	if len(nodes) >= 2:
		immediate = nodes[1].get("voucher")
	elif (nodes and nodes[0].get("waits_for")):
		immediate = nodes[0].get("waits_for")
	actionable = _first_actionable(real_nodes or nodes)
	no_path, stop_reason = _stop(root_status, circular, actionable, walked)
	required = _required_action(
		root_status=root_status,
		root_voucher=root_voucher,
		circular=circular,
		stop_reason=stop_reason,
		actionable=actionable,
		waiting=nodes[0].get("status") if nodes else root_status,
		immediate=immediate,
	)
	preview = _chain_preview(repair_order, required)
	isolation = analyze_batch_isolation(row, cache=cache)
	count = max(len(repair_order), 1)
	runtime_s = max(0.05, (count * MS_PER_STEP) / 1000.0)
	return {
		"current_voucher": voucher_of(row),
		"status": (nodes[0].get("status") if nodes else root_status),
		"immediate_blocker": immediate,
		"root_blocker": root_voucher,
		"root_status": root_status,
		"root_topic": (root or {}).get("topic") or _topic_of(root or {}),
		"root_row": walked.get("root_row"),
		"dependency_depth": max(0, len(repair_order) - 1),
		"repair_order": repair_order,
		"repair_sequence": " → ".join(repair_order),
		"estimated_repair_count": count,
		"estimated_runtime": f"{runtime_s:.2f}s",
		"estimated_runtime_seconds": runtime_s,
		"required_action": required,
		"no_repair_path": no_path,
		"stop_reason": stop_reason,
		"circular": circular,
		"blocked_because": (nodes[0].get("blocked_because") if nodes else None) or walked.get("blocked_because"),
		"dependency_tree": tree,
		"tree_text": format_tree(tree, count),
		"chain_preview": preview,
		"batch_isolation": isolation,
		"first_actionable": (actionable or {}).get("voucher"),
		"nodes": nodes,
	}


def stamp_dependency(row: dict, *, cache: dict | None = None, decision: dict | None = None) -> dict:
	"""Overlay the canonical tree onto a planner-stamped row."""
	out = dict(row or {})
	resolution = resolve_dependencies(out, cache=cache, _decision=decision)
	out["immediate_blocker"] = resolution["immediate_blocker"]
	out["root_blocker"] = resolution["root_blocker"]
	out["root_patient_zero"] = resolution["root_blocker"]
	out["root_status"] = resolution["root_status"]
	out["root_topic"] = resolution["root_topic"]
	out["dependency_depth"] = resolution["dependency_depth"]
	out["repair_order"] = resolution["repair_sequence"]
	out["repair_order_list"] = resolution["repair_order"]
	out["estimated_repair_count"] = resolution["estimated_repair_count"]
	out["estimated_runtime"] = resolution["estimated_runtime"]
	out["estimated_runtime_seconds"] = resolution["estimated_runtime_seconds"]
	out["required_action"] = resolution["required_action"]
	out["no_repair_path"] = resolution["no_repair_path"]
	out["stop_reason"] = resolution["stop_reason"]
	out["circular_dependency"] = resolution["circular"]
	out["blocked_because"] = resolution["blocked_because"]
	out["dependency_tree"] = resolution["dependency_tree"]
	out["tree_text"] = resolution["tree_text"]
	out["chain_preview"] = resolution["chain_preview"]
	out["batch_isolation"] = resolution["batch_isolation"]
	out["first_actionable"] = resolution["first_actionable"]
	status = out.get("planner_status")
	from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_BLOCKED, PLAN_READY

	if status == PLAN_BLOCKED and resolution["immediate_blocker"]:
		out["planner_status"] = _waiting_status_for(resolution)
		if isinstance(out.get("planner"), dict):
			out["planner"]["planner_status"] = out["planner_status"]
			out["planner"]["required_prerequisite"] = resolution["immediate_blocker"]
	elif status == PLAN_BLOCKED and resolution["no_repair_path"]:
		out["planner_status"] = PLAN_NO_REPAIR_PATH
		if isinstance(out.get("planner"), dict):
			out["planner"]["planner_status"] = PLAN_NO_REPAIR_PATH
	if status != PLAN_READY:
		out["eligible"] = False
		out["blocked"] = True
		out["sql_updates"] = 0
	return out


def analyze_batch_isolation(row: dict | None, *, cache: dict | None = None) -> dict:
	"""ERPNext SLE identity is item+warehouse. Batch-scoped replay is fail-closed."""
	row = row or {}
	cache = cache if cache is not None else {}
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse")
	batch = row.get("batch")
	why = {
		"A": "No — SLE qty_after / valuation_rate / moving average are item+warehouse, not batch.",
		"B": "Yes — a warehouse replay would rewrite unrelated batches in the same identity.",
		"C": "Yes — later lots would inherit a stale warehouse moving average and GL would diverge.",
		"classification": None,
		"can_isolate_batch": False,
		"would_affect_unrelated_batches": True,
		"would_corrupt_gl_or_ma": True,
	}
	if not (row.get("inbound_document") and row.get("outbound_document")):
		why["classification"] = "NOT_POSTING_ORDER"
		return why
	if not item or not warehouse:
		why["classification"] = "WAREHOUSE_WIDE_POISON"
		why["detail"] = "Posting-order identity is incomplete; warehouse-wide gate stays closed."
		return why
	batches = _window_batches(item, warehouse, row.get("current_outbound_time") or row.get("current_inbound_time"), cache)
	other = sorted({b for b in batches if b and b != (batch or "")})
	why["window_batches"] = batches
	why["unrelated_batches"] = other
	if other:
		why["classification"] = "WAREHOUSE_WIDE_POISON"
		why["detail"] = (
			f"Window contains {len(other)} unrelated batch(es); "
			"cannot isolate this Batch/SABB/Work Order from warehouse qty_after."
		)
		return why
	# Even a single-batch window still shares warehouse qty_after with any future
	# receipt of another lot. Never promote to READY_BATCH_SCOPED_REPAIR.
	why["classification"] = "WAREHOUSE_WIDE_POISON"
	why["detail"] = (
		"Single-batch window still uses warehouse identity. "
		f"{PLAN_READY_BATCH_SCOPED} is not proven safe."
	)
	return why


def apply_dependency_chain(row: dict, *, dry_run: bool = True) -> dict:
	"""Repair only the current READY root. Never the warehouse, item, or descendants."""
	from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, attach_plan

	resolution = resolve_dependencies(row)
	root_row = resolution.get("root_row")
	if resolution.get("no_repair_path") or resolution.get("root_status") != PLAN_READY or not root_row:
		reason = resolution.get("required_action") or "NO REPAIR PATH"
		return {
			"dry_run": dry_run,
			"aborted": True,
			"executable": False,
			"reason": reason,
			"skip_reason": reason,
			"planner_status": resolution.get("root_status") or PLAN_NO_REPAIR_PATH,
			"repairing": [],
			"applied": [],
			"sql_updates_executed": 0,
			"sql_updates_planned": 0,
			"savepoint_created": False,
			"transaction_committed": False,
			"database_backup_recommended": True,
			"chain_preview": resolution.get("chain_preview"),
			"repair_order": resolution.get("repair_order"),
			"dependency_tree": resolution.get("dependency_tree"),
			"tree_text": resolution.get("tree_text"),
			"required_action": reason,
		}
	planned = attach_plan(root_row)
	if planned.get("planner_status") != PLAN_READY or cint(planned.get("sql_updates")) <= 0:
		reason = planned.get("reason") or "Root is not READY"
		return {
			"dry_run": dry_run,
			"aborted": True,
			"executable": False,
			"reason": reason,
			"skip_reason": reason,
			"planner_status": planned.get("planner_status"),
			"repairing": [],
			"applied": [],
			"sql_updates_executed": 0,
			"repair_order": resolution.get("repair_order"),
			"chain_preview": resolution.get("chain_preview"),
			"dependency_tree": resolution.get("dependency_tree"),
			"tree_text": resolution.get("tree_text"),
		}
	preview = {
		"dry_run": True,
		"aborted": False,
		"executable": True,
		"planner_status": PLAN_READY,
		"repairing": [voucher_of(planned)],
		"reason": f"Repair only {voucher_of(planned)}. Descendants stay untouched until the next scan.",
		"sql_updates_planned": cint(planned.get("sql_updates")),
		"chain_preview": resolution.get("chain_preview"),
		"repair_order": resolution.get("repair_order"),
		"dependency_tree": resolution.get("dependency_tree"),
		"tree_text": resolution.get("tree_text"),
		"required_action": resolution.get("required_action"),
		"database_backup_recommended": True,
		"database_backup_required": True,
		"warning": "DATABASE BACKUP REQUIRED",
		"root_row": planned,
	}
	if dry_run:
		return preview
	applied = _dispatch_root_repair(planned, dry_run=False)
	applied["repair_order"] = resolution.get("repair_order")
	applied["chain_preview"] = resolution.get("chain_preview")
	applied["dependency_tree"] = resolution.get("dependency_tree")
	applied["tree_text"] = resolution.get("tree_text")
	applied["repaired_root"] = voucher_of(planned)
	applied["skipped_descendants"] = [v for v in (resolution.get("repair_order") or []) if v != voucher_of(planned)]
	return applied


def format_tree(tree: dict | None, estimated_count: int | None = None) -> str:
	tree = tree or {}
	lines: list[str] = []

	def walk(node, prefix, is_root):
		voucher = node.get("voucher") or "(unknown)"
		status = node.get("status") or ""
		because = node.get("blocked_because")
		if is_root:
			lines.append(voucher)
		else:
			extra = f"  ({because})" if because else (f"  {status}" if status else "")
			lines.append(f"{prefix}├── waits for {voucher}{extra}")
		children = node.get("children") or []
		child_prefix = "│   " if is_root else prefix + "│   "
		for child in children:
			walk(child, child_prefix, False)
		if children and not (children[-1].get("children")):
			lines.append(f"{child_prefix}└── {children[-1].get('status') or ''}")

	walk(tree, "", True)
	count = estimated_count if estimated_count is not None else 1 + _child_count(tree)
	lines.append("└── estimated chain:")
	lines.append(f"{count} vouchers")
	return "\n".join(lines)


def tree_as_graph(resolution: dict) -> dict:
	nodes = []
	edges = []
	seen = set()

	def walk(node, order):
		if not node:
			return order
		voucher = node.get("voucher")
		if voucher and voucher not in seen:
			seen.add(voucher)
			nodes.append(
				{
					"id": voucher,
					"voucher": voucher,
					"planner_status": node.get("status"),
					"repair_status": node.get("status"),
					"status": node.get("status"),
					"blocker": node.get("blocked_because") or node.get("reason"),
					"reason": node.get("reason"),
					"replay_order": order,
					"replay_depth": node.get("depth") or 0,
					"item": node.get("item"),
					"warehouse": node.get("warehouse"),
					"batch": node.get("batch"),
					"required_action": node.get("required_action"),
				}
			)
			order += 1
		for child in node.get("children") or []:
			if voucher and child.get("voucher"):
				edges.append({"from": voucher, "to": child.get("voucher")})
			order = walk(child, order)
		return order

	walk(resolution.get("dependency_tree") or {}, 1)
	return {
		"nodes": nodes,
		"edges": edges,
		"count": len(nodes),
		"replay_depth": resolution.get("dependency_depth") or 0,
		"dependency_tree": resolution.get("dependency_tree"),
		"tree_text": resolution.get("tree_text"),
		"repair_order": resolution.get("repair_order"),
		"required_action": resolution.get("required_action"),
		"planner_status": resolution.get("status"),
		"root_blocker": resolution.get("root_blocker"),
		"immediate_blocker": resolution.get("immediate_blocker"),
	}


def load_blocker_row(voucher: str, *, cache: dict | None = None, hint: dict | None = None) -> dict | None:
	"""Load a blocker as a planner row without going through stamp_scan_result."""
	cache = cache if cache is not None else {}
	rows_cache = cache.setdefault("blocker_rows", {})
	if voucher in rows_cache:
		return rows_cache[voucher]
	rows_cache[voucher] = None
	row = _fetch_blocker_row(voucher, hint or {})
	rows_cache[voucher] = row
	return row


def _walk(row, *, cache, stack, depth, decision=None):
	from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, evaluate_row

	voucher = voucher_of(row)
	decision = decision or evaluate_row(row, cache=cache)
	because = _blocked_because(decision, row)
	node = {
		"voucher": voucher,
		"status": decision.get("planner_status"),
		"reason": decision.get("reason"),
		"blocked_because": because,
		"waits_for": None,
		"item": row.get("item") or row.get("item_code"),
		"warehouse": row.get("warehouse"),
		"batch": row.get("batch"),
		"topic": _topic_of(row),
		"depth": depth,
		"children": [],
		"ready": decision.get("planner_status") == PLAN_READY and cint(decision.get("sql_updates")) > 0,
	}
	nodes = [node]
	if node["ready"] or depth >= MAX_DEPTH:
		return {"tree": node, "nodes": nodes, "circular": False, "root_row": row if node["ready"] else None, "blocked_because": because}
	blocker = _immediate_blocker(decision, row)
	if blocker and blocker == voucher:
		blocker = None
	if not blocker:
		return {"tree": node, "nodes": nodes, "circular": False, "root_row": None, "blocked_because": because}
	node["waits_for"] = blocker
	if blocker in stack or blocker == voucher:
		child = {
			"voucher": blocker,
			"status": PLAN_NO_REPAIR_PATH,
			"reason": STOP_CIRCULAR,
			"blocked_because": STOP_CIRCULAR,
			"children": [],
			"depth": depth + 1,
		}
		node["children"] = [child]
		return {
			"tree": node,
			"nodes": nodes + [child],
			"circular": True,
			"root_row": None,
			"blocked_because": because,
		}
	child_row = load_blocker_row(blocker, cache=cache, hint=row)
	if not child_row:
		child_row = _synthetic(blocker, row)
	child_walk = _walk(child_row, cache=cache, stack=stack + [voucher], depth=depth + 1)
	node["children"] = [child_walk["tree"]]
	return {
		"tree": node,
		"nodes": nodes + (child_walk.get("nodes") or []),
		"circular": bool(child_walk.get("circular")),
		"root_row": child_walk.get("root_row"),
		"blocked_because": because,
	}


def _immediate_blocker(decision, row) -> str | None:
	for val in (
		decision.get("required_prerequisite"),
		decision.get("patient_zero"),
		patient_of(row),
	):
		name = _as_voucher(val)
		if name:
			return name
	reason = str(decision.get("reason") or "")
	found = VOUCHER_RE.findall(reason)
	current = voucher_of(row)
	for token in found:
		if token.startswith("MAT-STE-") and token != current:
			return token
	return None


def _blocked_because(decision, row) -> str | None:
	dep = decision.get("dependency")
	if dep and dep not in (decision.get("required_prerequisite"), decision.get("patient_zero")):
		return str(dep)
	reason = str(decision.get("reason") or "")
	for token in (
		"negative_incoming_rate",
		"sign_inverted_incoming_svd",
		"sign_inverted_outgoing_svd",
		"qty_after_zero_nonzero_value",
		"qty_zero_nonzero_value",
		"exploded_rate",
		"nonzero_to_zero_incoming",
	):
		if token in reason or token == dep:
			return token
	return dep or row.get("mismatch_class") or None


def _first_actionable(nodes: list[dict]) -> dict | None:
	ready = [n for n in reversed(nodes) if n.get("ready")]
	if ready:
		return ready[0]
	for n in reversed(nodes):
		because = str(n.get("blocked_because") or "")
		if because and because not in INVERSION_ARTIFACT_POISONS:
			return n
	return nodes[-1] if nodes else None


def _stop(root_status, circular, actionable, walked):
	if circular:
		return True, STOP_CIRCULAR
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		PLAN_AMBIGUOUS,
		PLAN_MANUAL,
		PLAN_READY,
	)

	if root_status == PLAN_READY:
		return False, None
	if root_status == PLAN_AMBIGUOUS:
		return True, STOP_AMBIGUOUS
	if root_status == PLAN_MANUAL:
		return True, STOP_MANUAL
	if root_status == PLAN_NO_REPAIR_PATH:
		return True, walked.get("blocked_because") or STOP_MANUAL
	if actionable and not actionable.get("ready"):
		return True, STOP_MANUAL
	return True, STOP_EXTERNAL


def _required_action(*, root_status, root_voucher, circular, stop_reason, actionable, waiting, immediate=None):
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		PLAN_READY,
		PLAN_WAITING_GL_REPAIR,
		PLAN_WAITING_SLE_REPAIR,
	)

	target = (actionable or {}).get("voucher") or immediate or root_voucher
	because = (actionable or {}).get("blocked_because") or ""
	if circular:
		if target:
			return (
				f"NO REPAIR PATH: {STOP_CIRCULAR} — Repair Wrong Rate {immediate or target} first "
				"(warehouse poison and leftover patient-zero wait on each other; not auto)"
			)
		return f"NO REPAIR PATH: {STOP_CIRCULAR}"
	if root_status == PLAN_READY and root_voucher:
		return f"Repair {root_voucher} first"
	if waiting == PLAN_WAITING_SLE_REPAIR:
		return "Replay Downstream first"
	if waiting == PLAN_WAITING_GL_REPAIR:
		return "Run Rebuild Documents first"
	if because in ("negative_incoming_rate", "WRONG_INCOMING_RATE", "WRONG_BASIC_RATE") or "incoming" in str(because):
		return f"Repair Wrong Rate {target} first" if target else "Repair Wrong Rate first"
	if stop_reason == STOP_AMBIGUOUS:
		return f"NO REPAIR PATH: {STOP_AMBIGUOUS}"
	if stop_reason == STOP_MANUAL:
		if target:
			return f"Repair Wrong Rate {target} first (manual — not auto)"
		return f"NO REPAIR PATH: {STOP_MANUAL}"
	if stop_reason in (STOP_CROSS_ITEM, STOP_CROSS_WAREHOUSE, STOP_EXTERNAL):
		return f"NO REPAIR PATH: {stop_reason}"
	if target:
		return f"Repair {target} first"
	return f"NO REPAIR PATH: {stop_reason or STOP_MANUAL}"


def _chain_preview(repair_order: list[str], required: str) -> list[str]:
	steps = []
	for i, voucher in enumerate(repair_order, start=1):
		steps.append(f"{i}. Repair {voucher}")
	n = len(steps)
	steps.append(f"{n + 1}. Replay downstream")
	steps.append(f"{n + 2}. Integrity")
	if required:
		steps.append(f"Required action: {required}")
	return steps


def _waiting_status_for(resolution) -> str:
	from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_WAITING_RATE_REPAIR

	because = str(resolution.get("blocked_because") or "")
	if because in INVERSION_ARTIFACT_POISONS:
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_WAITING_SLE_REPAIR

		return PLAN_WAITING_SLE_REPAIR
	return PLAN_WAITING_RATE_REPAIR


def _fetch_blocker_row(voucher: str, hint: dict) -> dict:
	sles = []
	try:
		import frappe

		sles = frappe.db.sql(
			"""
			SELECT name, voucher_no, voucher_detail_no, item_code, warehouse, actual_qty,
			       incoming_rate, outgoing_rate, valuation_rate, stock_value_difference,
			       stock_value, qty_after_transaction, posting_datetime, creation,
			       serial_and_batch_bundle, batch_no
			FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			""",
			voucher,
			as_dict=True,
		)
	except Exception:
		sles = []
	picked = None
	hint_item = hint.get("item") or hint.get("item_code")
	hint_wh = hint.get("warehouse")
	if hint_item and hint_wh:
		for sle in sles:
			if sle.item_code == hint_item and sle.warehouse == hint_wh:
				picked = sle
				break
	if not picked:
		for sle in sles:
			if sle_poison_reason(sle):
				picked = sle
				break
	picked = picked or (sles[0] if sles else None)
	if not picked:
		return _synthetic(voucher, hint)
	poison = sle_poison_reason(picked)
	pz = None
	try:
		from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero_identity

		pz = find_patient_zero_identity(picked.item_code, picked.warehouse)
	except Exception:
		pz = None
	pz_name = pz.get("voucher_no") if isinstance(pz, dict) else None
	status = STATUS_MANUAL_REVIEW
	if pz_name and pz_name != voucher:
		status = STATUS_DEPENDENCY_REPAIR_REQUIRED
	elif poison:
		status = STATUS_VALUATION_POISON_DEPENDENCY
	return {
		"topic": "WRONG_RATE",
		"surface": "SLE",
		"voucher": voucher,
		"item": picked.item_code,
		"warehouse": picked.warehouse,
		"batch": picked.batch_no,
		"status": status,
		"confidence": CONFIDENCE_LIKELY,
		"eligible": False,
		"patient_zero": pz,
		"poison_reason": poison,
		"posting_datetime": picked.posting_datetime,
		"incoming_rate": picked.incoming_rate,
	}


def _synthetic(voucher: str, hint: dict) -> dict:
	return {
		"topic": "WRONG_RATE",
		"voucher": voucher,
		"item": hint.get("item") or hint.get("item_code"),
		"warehouse": hint.get("warehouse"),
		"batch": hint.get("batch"),
		"status": STATUS_MANUAL_REVIEW,
		"confidence": CONFIDENCE_MANUAL,
		"eligible": False,
	}


def _window_batches(item, warehouse, from_dt, cache) -> list[str]:
	if not item or not warehouse or not from_dt:
		return []
	key = ("batches", item, warehouse, str(from_dt))
	if key in cache:
		return cache[key]
	out = []
	try:
		import frappe
		from frappe.utils import get_datetime

		start = get_datetime(from_dt)
		rows = frappe.db.sql(
			"""
			SELECT DISTINCT IFNULL(sbe.batch_no, IFNULL(sle.batch_no, ''))
			FROM `tabStock Ledger Entry` sle
			LEFT JOIN `tabSerial and Batch Entry` sbe
			       ON sbe.parent = sle.serial_and_batch_bundle
			WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=0
			  AND sle.posting_datetime >= %s
			""",
			(item, warehouse, start),
		)
		out = [r[0] for r in rows if r and r[0]]
	except Exception:
		out = []
	cache[key] = out
	return out


def _dispatch_root_repair(row: dict, *, dry_run: bool):
	if row.get("inbound_document") and row.get("outbound_document"):
		from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs

		return apply_repairs([row], dry_run=dry_run)
	if row.get("gl_class") or row.get("topic") == "GL":
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher

		return rebuild_gl_for_voucher(voucher_of(row), dry_run=dry_run)
	from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
		repair_wrong_rate_selected,
		repair_zero_rate_selected,
	)

	if row.get("topic") == "ZERO_RATE" or row.get("zero_class"):
		return repair_zero_rate_selected([row], dry_run=dry_run)
	return repair_wrong_rate_selected([row], dry_run=dry_run)


def _topic_of(row: dict) -> str:
	if row.get("inbound_document") and row.get("outbound_document"):
		return "POSTING_ORDER"
	if row.get("gl_class"):
		return "GL"
	if row.get("riv_name"):
		return "FAILED_RIV"
	if row.get("topic"):
		return str(row.get("topic"))
	return "WRONG_RATE"


def _as_voucher(val) -> str | None:
	if isinstance(val, dict):
		val = val.get("voucher_no") or val.get("voucher")
	if val:
		return str(val)
	return None


def _leaf_node(row, decision) -> dict:
	return {
		"voucher": voucher_of(row),
		"status": (decision or {}).get("planner_status"),
		"reason": (decision or {}).get("reason"),
		"children": [],
		"depth": 0,
	}


def _child_count(node) -> int:
	n = 0
	for child in node.get("children") or []:
		n += 1 + _child_count(child)
	return n
