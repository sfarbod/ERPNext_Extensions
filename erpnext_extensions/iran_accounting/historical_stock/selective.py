# Copyright (c) 2026, ERPNext Extensions contributors
"""Identity-scoped selective replay / rebuild / RIV. Never global."""

from __future__ import annotations

import frappe
from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.historical_stock import STATUS_UNSAFE_TO_REPOST
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected, repost_selected
from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
	rebuild_chain_valuation,
	write_sle_transaction_rates,
	sync_sabb_from_sle,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import _bin_from_last_sle, replay_item_warehouse


def require_scope(scope: dict) -> None:
	if not any(scope.get(k) for k in ("item", "item_code", "warehouse", "batch", "work_order", "voucher", "voucher_no")):
		frappe.throw("Selective repair requires item, warehouse, batch, work order, or voucher. Global scan-only.")


def resolve_identities(scope: dict) -> list[dict]:
	require_scope(scope)
	item = scope.get("item") or scope.get("item_code")
	warehouse = scope.get("warehouse")
	batch = scope.get("batch")
	voucher = scope.get("voucher") or scope.get("voucher_no")
	wo = scope.get("work_order")
	from_date = scope.get("from_date")
	to_date = scope.get("to_date")
	conds = ["sle.is_cancelled=0", "sle.voucher_type='Stock Entry'"]
	args: list = []
	if item:
		conds.append("sle.item_code=%s")
		args.append(item)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if voucher:
		conds.append("sle.voucher_no=%s")
		args.append(voucher)
	if wo:
		conds.append("se.work_order=%s")
		args.append(wo)
	if from_date:
		conds.append("sle.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("sle.posting_date<=%s")
		args.append(to_date)
	join = "JOIN `tabStock Entry` se ON se.name=sle.voucher_no"
	if batch:
		join += " JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle"
		conds.append("sbe.batch_no=%s")
		args.append(batch)
	rows = frappe.db.sql(
		f"""
		SELECT DISTINCT sle.item_code item, sle.warehouse, sle.voucher_no,
		       MIN(sle.posting_datetime) from_dt
		FROM `tabStock Ledger Entry` sle
		{join}
		WHERE {" AND ".join(conds)}
		GROUP BY sle.item_code, sle.warehouse, sle.voucher_no
		ORDER BY MIN(sle.posting_datetime)
		LIMIT 200
		""",
		args,
		as_dict=True,
	)
	return rows


def selective_pipeline(scope: dict, *, op="replay", dry_run=True) -> dict:
	"""Replay → SLE rates → SABB → Bin → selective GL/RIV. Identity only."""
	require_scope(scope)
	identities = resolve_identities(scope)
	plan = {
		"dry_run": dry_run,
		"op": op,
		"scope": {k: scope.get(k) for k in ("item", "item_code", "warehouse", "batch", "work_order", "voucher", "from_date", "to_date") if scope.get(k)},
		"identity_count": len({(r.item, r.warehouse) for r in identities}),
		"voucher_count": len({r.voucher_no for r in identities}),
		"identities": identities[:50],
		"riv": "NOT_INVOKED",
		"sequence": ["SE", "SLE", "SABB", "forward replay", "Bin", "selective GL", "Failed RIV", "optional selective RIV"],
	}
	if dry_run or op == "preview":
		return plan
	savepoint = f"sel_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	log = start_run("SELECTIVE", dry_run=False)
	applied = []
	try:
		seen = set()
		for row in identities:
			key = (row.item, row.warehouse)
			if op in ("replay", "rebuild") and key not in seen:
				seen.add(key)
				applied.append(
					replay_item_warehouse(row.item, row.warehouse, row.from_dt, ignore_inversion_artifacts=True)
				)
			write_sle_transaction_rates(row.voucher_no, row.item)
			sync_sabb_from_sle(row.voucher_no, row.item)
			_bin_from_last_sle(row.item, row.warehouse)
			append_entry(log, {"voucher": row.voucher_no, "item": row.item, "warehouse": row.warehouse, "topic": op}, written=True)
		if op == "rebuild" and scope.get("inbound_document") and scope.get("outbound_document"):
			applied.append(
				rebuild_chain_valuation(
					scope["inbound_document"],
					scope["outbound_document"],
					item=scope.get("item") or scope.get("item_code"),
					warehouse=scope.get("warehouse"),
					batch=scope.get("batch"),
					dry_run=False,
					allow_riv=False,
				)
			)
		if op == "riv":
			item = scope.get("item") or scope.get("item_code")
			warehouse = scope.get("warehouse")
			if not item or not warehouse:
				raise frappe.ValidationError("Selective RIV requires item and warehouse")
			riv = repost_selected(item, warehouse, from_date=scope.get("from_date"), dry_run=False)
			if riv.get("blocked"):
				raise frappe.ValidationError(riv.get("status") or STATUS_UNSAFE_TO_REPOST)
			plan["riv"] = riv.get("riv_name") or "QUEUED"
			applied.append(riv)
		if op == "downstream":
			from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
				replay_downstream_chain,
			)

			item = scope.get("item") or scope.get("item_code")
			batch = scope.get("batch")
			if not item or not batch:
				raise frappe.ValidationError("Downstream replay requires item and batch")
			from_dt = scope.get("from_date") or scope.get("from_dt")
			if from_dt:
				from_dt = get_datetime(from_dt)
			applied.append(
				replay_downstream_chain(
					item,
					batch,
					from_dt=from_dt,
					warehouse=scope.get("warehouse"),
					work_order=scope.get("work_order"),
					patient_vouchers=[scope.get("voucher")] if scope.get("voucher") else None,
					dry_run=False,
				)
			)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		finish_run(log, applied=0, blocked=1, error="rolled back")
		raise
	finish_run(log, applied=len(applied), blocked=0)
	plan["applied"] = applied
	plan["repair_run_id"] = getattr(log, "repair_run_id", None)
	plan["written"] = True
	return plan
