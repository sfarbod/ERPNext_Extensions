# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only impact analysis. Nothing executes until the operator confirms."""

from __future__ import annotations

import frappe
from frappe.utils import cint

MS_PER_SLE = 50
DATABASE_BACKUP_REQUIRED = "DATABASE BACKUP REQUIRED"


def collect_vouchers(rows: list[dict]) -> list[str]:
	out = []
	seen = set()
	for row in rows or []:
		for key in (
			"voucher",
			"voucher_no",
			"inbound_document",
			"outbound_document",
			"patient_zero",
		):
			val = row.get(key)
			if isinstance(val, dict):
				val = val.get("voucher_no")
			if val and val not in seen:
				seen.add(val)
				out.append(val)
	return out


def plan_repair_impact(rows: list[dict]) -> dict:
	"""Impact is the Repair Planner. No independent eligibility."""
	from erpnext_extensions.iran_accounting.historical_stock.planner import plan_selection

	return plan_selection(rows)


def format_impact(plan: dict) -> str:
	chain = plan.get("replay_chain") or plan.get("replay_order") or []
	chain_txt = "\n↓\n".join(chain) if chain else "(no chain)"
	lines = [
		"Repairing:",
		", ".join(plan.get("repairing") or []) or "(none)",
		"",
		"will affect:",
		f"{plan.get('stock_entries') or 0} Stock Entries",
		f"{plan.get('sle') or 0} SLE",
		f"{plan.get('sabb') or 0} SABB",
		f"{plan.get('sbe') or 0} Serial & Batch Entry",
		f"{plan.get('bin') or 0} Bin",
		f"{plan.get('gl') or 0} GL",
		f"{plan.get('failed_riv') or 0} Failed RIV",
		"",
		f"Estimated replay: {plan.get('estimated_replay_seconds')} seconds",
		f"Estimated SQL updates: {plan.get('estimated_sql_updates')} rows",
		f"Estimated replay depth: {plan.get('estimated_replay_depth')}",
		"",
		f"Planner status: {plan.get('planner_status') or ''}",
		f"Current Scope: {'BATCH_SCOPED' if (plan.get('rows') or [{}])[0].get('inbound_document') else 'LOCAL_VOUCHER'}",
		f"Required Scope: {plan.get('smallest_safe_scope') or ''}",
		f"Reason: {plan.get('escalation_reason') or plan.get('reason') or plan.get('skip_reason') or ''}",
		f"Effect: {plan.get('escalation_reason') and 'WAREHOUSE_MA rewrite' or 'smallest safe scope'}",
		f"Estimated affected vouchers: {plan.get('estimated_repair_count') or len(plan.get('repair_order') or [])}",
		f"Estimated replay count: {plan.get('replay_count') or plan.get('estimated_replay_depth') or 0}",
		f"Estimated SQL updates: {plan.get('estimated_sql_updates') if plan.get('estimated_sql_updates') is not None else plan.get('sql_updates')}",
		f"Eligible: {bool(plan.get('executable'))}",
		f"Blocked: {bool(plan.get('aborted'))}",
		f"Required action: {plan.get('required_action') or ''}",
		f"Immediate blocker: {plan.get('immediate_blocker') or ''}",
		f"Root blocker: {plan.get('root_blocker') or ''}",
		f"Root status: {plan.get('root_status') or ''}",
		f"Dependency depth: {plan.get('dependency_depth') if plan.get('dependency_depth') is not None else ''}",
		f"Repair sequence: {plan.get('repair_sequence') or ''}",
		f"Estimated repair count: {plan.get('estimated_repair_count') or 0}",
		f"SQL updates: {plan.get('sql_updates') if plan.get('sql_updates') is not None else plan.get('estimated_sql_updates')}",
		f"Replay count: {plan.get('replay_count') or 0}",
		f"Rebuild count: {plan.get('rebuild_count') or 0}",
		f"Dependency: {plan.get('dependency') or plan.get('blocked_because') or ''}",
		f"Patient Zero: {plan.get('patient_zero') or plan.get('root_blocker') or ''}",
		f"Required prerequisite: {plan.get('required_prerequisite') or plan.get('immediate_blocker') or ''}",
		"",
		"Repair order:",
	]
	preview_steps = plan.get("chain_preview") or []
	if preview_steps:
		lines.extend(preview_steps)
	else:
		lines.append(chain_txt)
	lines.extend(
		[
			"",
			"Dependency tree:",
			plan.get("tree_text") or "(none)",
			"",
			"Estimated replay chain:",
			chain_txt,
			"",
			DATABASE_BACKUP_REQUIRED,
		]
	)
	if plan.get("aborted"):
		lines.append("ABORTED — ambiguity or poison dependency. Nothing will execute.")
		for b in plan.get("abort_reasons") or []:
			lines.append(f"  {b.get('voucher') or ''}: {b.get('reason')}")
	if plan.get("skip_reason") and not plan.get("aborted"):
		lines.append(f"SKIPPED — {plan.get('skip_reason')}")
	if (plan.get("estimated_sql_updates") or 0) <= 0:
		lines.append("SQL updates planned: 0. Repair Selected is disabled.")
	if not plan.get("full_rollback_possible"):
		lines.append("Full in-app rollback is not possible for this plan. Restore from database backup if needed.")
	lines.append("Nothing executes before operator confirmation.")
	return "\n".join(lines)


def _count_sle(vouchers, items, warehouses) -> int:
	if not vouchers:
		return 0
	return cint(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no IN %(v)s AND is_cancelled=0
			""",
			{"v": vouchers},
		)[0][0]
	)


def _count_sabb(vouchers) -> int:
	if not vouchers:
		return 0
	return cint(
		frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT serial_and_batch_bundle) FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no IN %(v)s AND is_cancelled=0
			  AND IFNULL(serial_and_batch_bundle,'') != ''
			""",
			{"v": vouchers},
		)[0][0]
	)


def _count_sbe(vouchers) -> int:
	if not vouchers:
		return 0
	return cint(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabSerial and Batch Entry` sbe
			JOIN `tabStock Ledger Entry` sle ON sle.serial_and_batch_bundle=sbe.parent
			WHERE sle.voucher_type='Stock Entry' AND sle.voucher_no IN %(v)s AND sle.is_cancelled=0
			""",
			{"v": vouchers},
		)[0][0]
	)


def _count_bins(items, warehouses) -> int:
	if not items or not warehouses:
		return len(items) * len(warehouses) if items or warehouses else 0
	return cint(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabBin`
			WHERE item_code IN %(i)s AND warehouse IN %(w)s
			""",
			{"i": items, "w": warehouses},
		)[0][0]
	)


def _count_gl(vouchers) -> int:
	if not vouchers:
		return 0
	return cint(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabGL Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no IN %(v)s AND IFNULL(is_cancelled,0)=0
			""",
			{"v": vouchers},
		)[0][0]
	)


def _count_failed_riv(items, warehouses) -> int:
	if not items:
		return 0
	conds = ["status='Failed'", "docstatus=1", "item_code IN %(i)s"]
	args = {"i": items}
	if warehouses:
		conds.append("warehouse IN %(w)s")
		args["w"] = warehouses
	return cint(
		frappe.db.sql(
			f"SELECT COUNT(*) FROM `tabRepost Item Valuation` WHERE {' AND '.join(conds)}",
			args,
		)[0][0]
	)


def _chain_for(rows) -> list[str]:
	from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies

	row = next(
		(
			r
			for r in rows
			if r.get("item")
			or r.get("item_code")
			or r.get("batch")
			or r.get("voucher")
			or r.get("voucher_no")
			or r.get("inbound_document")
			or r.get("outbound_document")
		),
		None,
	)
	if not row:
		return collect_vouchers(rows)
	if row.get("repair_order_list"):
		return list(row["repair_order_list"])
	try:
		res = resolve_dependencies(row)
		return res.get("repair_order") or collect_vouchers(rows)
	except Exception:
		return collect_vouchers(rows)
