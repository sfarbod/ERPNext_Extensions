# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only impact analysis. Nothing executes until the operator confirms."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_MANUAL,
	STATUS_VALUATION_POISON_DEPENDENCY,
)

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
	"""Count identity-scoped documents that Repair Selected would touch. Read-only."""
	rows = rows or []
	vouchers = collect_vouchers(rows)
	blocked = []
	for row in rows:
		conf = row.get("confidence")
		status = row.get("status")
		if status == STATUS_VALUATION_POISON_DEPENDENCY:
			blocked.append({"voucher": row.get("voucher"), "reason": "poison dependency"})
		elif conf in (CONFIDENCE_AMBIGUOUS, CONFIDENCE_MANUAL):
			blocked.append({"voucher": row.get("voucher"), "reason": f"abort on {conf}"})
		elif conf and conf != CONFIDENCE_EXACT and row.get("eligible"):
			blocked.append({"voucher": row.get("voucher"), "reason": f"abort on {conf}"})
	items = sorted({(r.get("item") or r.get("item_code")) for r in rows if r.get("item") or r.get("item_code")})
	warehouses = sorted({r.get("warehouse") for r in rows if r.get("warehouse")})
	batches = sorted({r.get("batch") for r in rows if r.get("batch")})

	sle = _count_sle(vouchers, items, warehouses)
	sabb = _count_sabb(vouchers)
	sbe = _count_sbe(vouchers)
	bins = _count_bins(items, warehouses)
	gl = _count_gl(vouchers)
	riv = _count_failed_riv(items, warehouses)
	chain = _chain_for(rows)
	depth = max(0, len(chain) - 1)
	sql_updates = (
		len(vouchers)
		+ sle
		+ sabb
		+ sbe
		+ bins
		+ (gl if False else 0)  # GL not auto-written by rate repair
	)
	# Rate repair writes SE + SLE + SABB + SBE + Bin. GL/RIV only if those ops are chosen.
	estimated_s = max(1.0, (sle * MS_PER_SLE) / 1000.0)
	gl_or_riv = bool(
		rows
		and any(
			(r.get("topic") in ("GL", "FAILED_RIV") or r.get("gl_class") or r.get("riv_name")) for r in rows
		)
	)
	full_rollback_possible = not gl_or_riv
	abort = bool(blocked)
	plan = {
		"dry_run": True,
		"repairing": vouchers,
		"stock_entries": len(vouchers),
		"sle": int(sle),
		"sabb": int(sabb),
		"sbe": int(sbe),
		"bin": int(bins),
		"gl": int(gl),
		"failed_riv": int(riv),
		"items": items,
		"warehouses": warehouses,
		"batches": batches,
		"estimated_replay_seconds": round(estimated_s, 2),
		"estimated_sql_updates": int(sql_updates),
		"estimated_replay_depth": depth,
		"replay_chain": chain,
		"replay_order": chain,
		"aborted": abort,
		"abort_reasons": blocked,
		"database_backup_recommended": True,
		"database_backup_required": True,
		"full_rollback_possible": full_rollback_possible and not abort,
		"warning": DATABASE_BACKUP_REQUIRED,
		"global_riv": False,
	}
	plan["preview_text"] = format_impact(plan)
	return plan


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
		"Estimated replay chain:",
		chain_txt,
		"",
		DATABASE_BACKUP_REQUIRED,
	]
	if plan.get("aborted"):
		lines.append("ABORTED — ambiguity or poison dependency. Nothing will execute.")
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
	from erpnext_extensions.iran_accounting.historical_stock.graph import repair_graph

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
	item = row.get("item") or row.get("item_code")
	voucher = (
		row.get("voucher")
		or row.get("voucher_no")
		or row.get("inbound_document")
		or row.get("outbound_document")
	)
	if not any((item, row.get("batch"), row.get("work_order"), voucher)):
		return collect_vouchers(rows)
	g = repair_graph(
		item=item,
		batch=row.get("batch"),
		work_order=row.get("work_order"),
		voucher=voucher,
		warehouse=row.get("warehouse"),
		limit=40,
	)
	nodes = [n.get("voucher") for n in (g.get("nodes") or []) if n.get("voucher")]
	return nodes or collect_vouchers(rows)
