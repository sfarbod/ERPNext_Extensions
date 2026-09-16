# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse scope analyzer — minimum safe replay scope for a candidate."""

from __future__ import annotations

from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock.scope import (
	SCOPE_BATCH,
	SCOPE_IDENTITY,
	SCOPE_LOCAL_VOUCHER,
	SCOPE_UNSAFE,
	SCOPE_WAREHOUSE,
	SCOPE_WORK_ORDER,
	evaluate_minimal_scope,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	SCOPE_BATCH_SCOPED,
	SCOPE_IDENTITY_SCOPED,
	SCOPE_LOCAL_VOUCHER as WE_LOCAL,
	SCOPE_UNSAFE_GLOBAL,
	SCOPE_WAREHOUSE_VALUATION,
	SCOPE_WORK_ORDER_SCOPED,
)


def analyze_candidate(row: dict, *, cache: dict | None = None) -> dict:
	"""Answer: batch / identity / warehouse minimum safe scope for ``row``."""
	row = dict(row or {})
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse")
	batch = row.get("batch") or row.get("batch_no") or ""
	scope = evaluate_minimal_scope(row, cache=cache)
	smallest = scope.get("smallest_safe_scope") or SCOPE_UNSAFE
	mapped = _map_scope(smallest)

	other_batches = scope.get("other_batch_changes") or []
	other_wos = sorted(
		{
			str(h.get("work_order") or "")
			for h in other_batches
			if h.get("work_order")
		}
	)
	affected_vouchers = sorted(
		{
			*(scope.get("repair_order") or []),
			*[h.get("voucher") for h in other_batches if h.get("voucher")],
			row.get("inbound_document"),
			row.get("outbound_document"),
		}
		- {None, ""}
	)
	from_dt = _window_start(row, scope)
	cross_wh = _cross_warehouse_deps(item, warehouse)

	can_batch = mapped in (WE_LOCAL, SCOPE_BATCH_SCOPED) and not scope.get("escalation_required")
	can_identity = mapped in (WE_LOCAL, SCOPE_BATCH_SCOPED, SCOPE_WORK_ORDER_SCOPED, SCOPE_IDENTITY_SCOPED)
	ma_forces_other = bool(scope.get("other_batch_rewrite_count"))
	required = mapped
	if ma_forces_other and required not in (SCOPE_WAREHOUSE_VALUATION, SCOPE_UNSAFE_GLOBAL):
		required = SCOPE_WAREHOUSE_VALUATION

	return {
		"selected_voucher": row.get("outbound_document") or row.get("negative_voucher") or row.get("voucher"),
		"item": item,
		"warehouse": warehouse,
		"batch": batch,
		"work_order": row.get("work_order"),
		"inbound_document": row.get("inbound_document"),
		"outbound_document": row.get("outbound_document"),
		"patient_zero": row.get("patient_zero"),
		"current_replay_scope": mapped,
		"required_replay_scope": required,
		"smallest_safe_scope": required,
		"can_repair_at_batch_scope": bool(can_batch),
		"can_repair_at_identity_scope": bool(can_identity) and not ma_forces_other,
		"ma_forces_other_batches": ma_forces_other,
		"other_batches_in_window": [
			{"voucher": h.get("voucher"), "batch": h.get("batch"), "fields": h.get("fields"), "sle": h.get("sle")}
			for h in other_batches
		],
		"other_work_orders_in_window": other_wos,
		"cross_warehouse_movements": cross_wh,
		"moving_average_dependencies": other_batches,
		"affected_vouchers": affected_vouchers,
		"affected_sle_estimate": int(scope.get("other_batch_rewrite_count") or 0)
		+ len(scope.get("repair_order") or []),
		"from_datetime": str(from_dt) if from_dt else None,
		"batch_qty_ok": scope.get("batch_qty_ok"),
		"warehouse_qty": scope.get("warehouse_qty"),
		"pair_end_value_equal": scope.get("pair_end_value_equal"),
		"warehouse_still_negative": scope.get("warehouse_still_negative"),
		"cyclic": scope.get("cyclic"),
		"edges": scope.get("edges"),
		"repair_order": scope.get("repair_order"),
		"escalation_required": scope.get("escalation_required"),
		"legacy_scope": scope,
		"moves": row.get("moves") or [],
		"proposed_outbound_time": row.get("proposed_outbound_time"),
		"proposed_inbound_time": row.get("proposed_inbound_time"),
		"reason": scope.get("reason") or scope.get("escalation_reason"),
	}


def _map_scope(smallest: str) -> str:
	return {
		SCOPE_LOCAL_VOUCHER: WE_LOCAL,
		SCOPE_BATCH: SCOPE_BATCH_SCOPED,
		SCOPE_WORK_ORDER: SCOPE_WORK_ORDER_SCOPED,
		SCOPE_IDENTITY: SCOPE_IDENTITY_SCOPED,
		SCOPE_WAREHOUSE: SCOPE_WAREHOUSE_VALUATION,
		SCOPE_UNSAFE: SCOPE_UNSAFE_GLOBAL,
	}.get(smallest, SCOPE_UNSAFE_GLOBAL)


def _window_start(row, scope):
	# Prefer earliest of inbound/outbound current times
	candidates = [
		row.get("current_outbound_time"),
		row.get("current_inbound_time"),
		row.get("proposed_outbound_time"),
		row.get("proposed_inbound_time"),
	]
	for m in row.get("moves") or []:
		candidates.append(m.get("old"))
		candidates.append(m.get("new"))
	parsed = []
	for c in candidates:
		if not c:
			continue
		try:
			parsed.append(get_datetime(c))
		except Exception:
			pass
	return min(parsed) if parsed else None


def _cross_warehouse_deps(item, warehouse) -> list[dict]:
	if not item or not warehouse:
		return []
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT DISTINCT warehouse, COUNT(*) AS n
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse!=%s AND is_cancelled=0
		GROUP BY warehouse
		LIMIT 20
		""",
		(item, warehouse),
		as_dict=True,
	)
	return [{"warehouse": r.warehouse, "sle_count": int(r.n)} for r in rows]
