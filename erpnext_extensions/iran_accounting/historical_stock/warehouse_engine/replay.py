# Copyright (c) 2026, ERPNext Extensions contributors
"""Controlled warehouse-scoped apply — timestamp reorder + full identity MA replay."""

from __future__ import annotations

from time import perf_counter

import frappe
from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import READY_WAREHOUSE_REPLAY
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner import plan_warehouse_repair
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	replay_item_warehouse,
	sync_transfer_incoming_rates,
)


def apply_warehouse_repair(row: dict, *, dry_run=True) -> dict:
	"""Apply one warehouse-escalation posting-order candidate when READY.

	Writes all Item+Warehouse SLEs from the window start (MA-safe), not only the
	inverted pair. Does not invoke RIV. Selective GL only when valuation changes.
	"""
	t0 = perf_counter()
	planned = plan_warehouse_repair(row)
	if planned.get("planner_status") != READY_WAREHOUSE_REPLAY or not planned.get("eligible"):
		return {
			"ok": False,
			"aborted": True,
			"dry_run": dry_run,
			"planner_status": planned.get("planner_status"),
			"reason": planned.get("reason"),
			"required_action": planned.get("required_action"),
			"planned": planned,
		}

	analysis = planned.get("warehouse_analysis") or {}
	item = analysis.get("item")
	warehouse = analysis.get("warehouse")
	from_dt = get_datetime(analysis.get("from_datetime"))
	moves = row.get("moves") or []
	write_vouchers = set(planned.get("affected_vouchers") or [])
	# Always include moved + pair vouchers
	for m in moves:
		if m.get("document"):
			write_vouchers.add(m["document"])
	for vn in (row.get("inbound_document"), row.get("outbound_document")):
		if vn:
			write_vouchers.add(vn)

	if dry_run:
		return {
			"ok": True,
			"dry_run": True,
			"planner_status": READY_WAREHOUSE_REPLAY,
			"item": item,
			"warehouse": warehouse,
			"from_datetime": str(from_dt),
			"moves": moves,
			"write_vouchers": sorted(write_vouchers),
			"estimated_sql": planned.get("sql_updates"),
			"validation": planned.get("warehouse_validation"),
			"message": "Dry run only — no writes",
		}

	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = f"wh_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	try:
		# 1) Timestamp reorder
		for m in moves:
			_update_posting_datetime(m["document"], get_datetime(m["new"]))

		# 2) Full identity MA replay — write all vouchers in window from from_dt
		#    (write_vouchers=None writes entire series). Prefer explicit set when small.
		rows_ahead = int((planned.get("warehouse_simulation") or {}).get("row_count") or 0)
		write_set = None if rows_ahead and rows_ahead <= 200 else write_vouchers
		rep = replay_item_warehouse(
			item,
			warehouse,
			from_dt,
			ignore_inversion_artifacts=True,
			write_vouchers=write_set,
			allow_unrelated_poison=True,  # MA-dependent other batches are intentional
			trust_simulated_series=True,
		)
		if not rep.get("ok"):
			frappe.db.rollback(save_point=savepoint)
			return {
				"ok": False,
				"aborted": True,
				"dry_run": False,
				"reason": rep.get("reason") or rep.get("status"),
				"replay": rep,
			}

		# 3) Transfer value-neutral sync for pair
		for vn in {row.get("inbound_document"), row.get("outbound_document")} - {None}:
			sync_transfer_incoming_rates(vn)

		# 4) Assert qty_after on negative voucher is non-negative
		_assert_outbound_non_negative(row.get("outbound_document") or row.get("negative_voucher"), item, warehouse)

		# 5) Selective GL for touched Stock Entries
		gl = _selective_gl(sorted(set(rep.get("touched_vouchers") or []) | write_vouchers))

		frappe.db.commit()
		# 6) Re-plan to confirm COMPLETE
		after = plan_warehouse_repair(row)
		return {
			"ok": True,
			"dry_run": False,
			"planner_status": READY_WAREHOUSE_REPLAY,
			"item": item,
			"warehouse": warehouse,
			"from_datetime": str(from_dt),
			"moves_applied": moves,
			"replay": rep,
			"gl": gl,
			"sql_updates_executed": int(rep.get("written") or 0) + len(moves) + int(gl.get("rebuilt") or 0),
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"riv": "NOT_INVOKED",
			"after_plan_status": after.get("planner_status"),
			"validation_before": planned.get("warehouse_validation"),
		}
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		return {"ok": False, "aborted": True, "dry_run": False, "error": str(exc)}
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def _update_posting_datetime(voucher_no: str, new_dt) -> None:
	from erpnext_extensions.iran_accounting.stock_posting_order.ordering import format_date, format_time

	# Prefer Stock Entry; fall back to SLE-only update path used by posting-order repair
	if frappe.db.exists("Stock Entry", voucher_no):
		frappe.db.set_value(
			"Stock Entry",
			voucher_no,
			{
				"posting_date": format_date(new_dt),
				"posting_time": format_time(new_dt),
				"set_posting_time": 1,
			},
			update_modified=False,
		)
	frappe.db.sql(
		"""
		UPDATE `tabStock Ledger Entry`
		SET posting_date=%s, posting_time=%s, posting_datetime=%s
		WHERE voucher_no=%s AND is_cancelled=0
		""",
		(format_date(new_dt), format_time(new_dt), new_dt, voucher_no),
	)


def _assert_outbound_non_negative(voucher, item, warehouse) -> None:
	if not voucher:
		return
	from frappe.utils import flt

	rows = frappe.db.sql(
		"""
		SELECT qty_after_transaction FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
		""",
		(voucher, item, warehouse),
		as_dict=True,
	)
	for r in rows:
		if flt(r.qty_after_transaction) < -0.0001:
			frappe.throw(f"Outbound {voucher} still negative qty_after={r.qty_after_transaction}")


def _selective_gl(vouchers: list[str]) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher

	rebuilt = []
	skipped = []
	for vn in vouchers:
		if not vn or not frappe.db.exists("Stock Entry", vn):
			skipped.append({"voucher": vn, "reason": "not Stock Entry"})
			continue
		try:
			out = rebuild_gl_for_voucher(vn, dry_run=False)
			rebuilt.append({"voucher": vn, "out": {k: out.get(k) for k in ("ok", "status", "written") if isinstance(out, dict)}})
		except Exception as exc:
			skipped.append({"voucher": vn, "error": str(exc)})
	return {"rebuilt": len(rebuilt), "skipped": skipped, "rows": rebuilt}
