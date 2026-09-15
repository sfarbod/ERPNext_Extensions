# Copyright (c) 2026, ERPNext Extensions contributors
"""Warehouse SAFE_GROUP campaign apply — savepoint, joint reorder, multi-identity MA.

Never applies unless optimizer marks READY_WAREHOUSE_CAMPAIGN and every touched
Item+Warehouse identity clears simulation.
"""

from __future__ import annotations

from time import perf_counter

import frappe
from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine import (
	READY_WAREHOUSE_CAMPAIGN,
	READY_WAREHOUSE_REPLAY,
	WAREHOUSE_CAMPAIGN_COMPLETE,
)
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
	optimize_identity_campaign,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	replay_item_warehouse,
	sync_transfer_incoming_rates,
)


def plan_warehouse_campaign(pairs: list[dict], *, item: str | None = None, warehouse: str | None = None) -> dict:
	"""Plan (read-only) a warehouse campaign from pair rows."""
	if not pairs:
		return {"ok": False, "reason": "no pairs", "eligible": False}
	item = item or pairs[0].get("item") or pairs[0].get("item_code")
	warehouse = warehouse or pairs[0].get("warehouse")
	campaign = optimize_identity_campaign(pairs, item=item, warehouse=warehouse)
	# Attach multi-identity verification
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
		verify_touched_identities,
	)

	multi = verify_touched_identities(campaign)
	campaign["multi_identity"] = multi
	if campaign.get("eligible") and not multi.get("all_clear"):
		campaign["eligible"] = False
		campaign["planner_status"] = multi.get("blocker_status") or "UNSAFE_CROSS_IDENTITY"
		campaign["reason"] = multi.get("reason") or "touched identity failed simulation"
		campaign["required_action"] = "Do not apply — cross-identity simulation failed"
		campaign["group_class"] = "UNSAFE_GROUP"
	campaign["ok"] = bool(campaign.get("eligible"))
	return campaign


def apply_warehouse_campaign(campaign_or_pairs, *, dry_run=True) -> dict:
	"""Apply a READY warehouse campaign under a DB savepoint.

	``campaign_or_pairs`` may be a planned campaign dict or a list of pair rows.
	"""
	t0 = perf_counter()
	if isinstance(campaign_or_pairs, list):
		campaign = plan_warehouse_campaign(campaign_or_pairs)
	else:
		campaign = dict(campaign_or_pairs or {})
		# Re-plan to ensure freshness
		if campaign.get("pairs"):
			campaign = plan_warehouse_campaign(
				campaign["pairs"],
				item=campaign.get("item"),
				warehouse=campaign.get("warehouse"),
			)

	ps = campaign.get("planner_status")
	if not campaign.get("eligible") or ps not in (READY_WAREHOUSE_CAMPAIGN, READY_WAREHOUSE_REPLAY):
		return {
			"ok": False,
			"aborted": True,
			"dry_run": dry_run,
			"planner_status": ps,
			"reason": campaign.get("reason"),
			"required_action": campaign.get("required_action"),
			"campaign": _public(campaign),
		}

	item = campaign["item"]
	warehouse = campaign["warehouse"]
	from_dt = get_datetime(campaign.get("from_datetime"))
	moves = campaign.get("moves") or []
	proposed = {m["document"]: get_datetime(m["new"]) for m in moves if m.get("document") and m.get("new")}

	if dry_run:
		return {
			"ok": True,
			"dry_run": True,
			"planner_status": ps,
			"item": item,
			"warehouse": warehouse,
			"from_datetime": str(from_dt),
			"n_moves": len(moves),
			"n_pairs": campaign.get("n_pairs"),
			"expected_sql": campaign.get("expected_sql"),
			"expected_replay": campaign.get("expected_replay"),
			"multi_identity": campaign.get("multi_identity"),
			"message": "Dry run only — no writes",
			"campaign_id": campaign.get("campaign_id"),
		}

	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = f"whcam_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	checkpoint = {
		"savepoint": savepoint,
		"item": item,
		"warehouse": warehouse,
		"from_datetime": str(from_dt),
		"proposed_times": {k: str(v) for k, v in proposed.items()},
		"n_moves": len(moves),
	}
	try:
		# 1) Joint timestamp reorder
		for m in moves:
			_update_posting_datetime(m["document"], get_datetime(m["new"]))

		# 2) Replay every touched identity that cleared simulation
		multi = campaign.get("multi_identity") or {}
		replays = []
		for ident in multi.get("identities") or []:
			if not ident.get("clears"):
				continue
			rep = replay_item_warehouse(
				ident["item"],
				ident["warehouse"],
				from_dt,
				ignore_inversion_artifacts=True,
				write_vouchers=None if int(ident.get("row_count") or 0) <= 250 else set(proposed),
				allow_unrelated_poison=True,
			)
			replays.append({"item": ident["item"], "warehouse": ident["warehouse"], "replay": rep})
			if not rep.get("ok"):
				frappe.db.rollback(save_point=savepoint)
				return {
					"ok": False,
					"aborted": True,
					"dry_run": False,
					"reason": f"replay failed on {ident['item']} / {ident['warehouse']}: {rep.get('reason') or rep.get('status')}",
					"checkpoint": checkpoint,
					"replays": replays,
				}

		# 3) Transfer value-neutral sync for pair vouchers
		synced = []
		for vn in set(campaign.get("pair_inbounds") or []) | set(campaign.get("pair_outbounds") or []):
			if vn and frappe.db.exists("Stock Entry", vn):
				sync_transfer_incoming_rates(vn)
				synced.append(vn)

		# 4) Assert primary identity outbounds non-negative
		for out in campaign.get("pair_outbounds") or []:
			_assert_outbound_non_negative(out, item, warehouse)

		# 5) Selective GL
		touched = set(proposed)
		for r in replays:
			touched |= set((r.get("replay") or {}).get("touched_vouchers") or [])
		gl = _selective_gl(sorted(touched))

		frappe.db.commit()
		# 6) Post-apply replan (idempotency checkpoint)
		after = plan_warehouse_campaign(
			campaign.get("pairs") or [],
			item=item,
			warehouse=warehouse,
		)
		return {
			"ok": True,
			"dry_run": False,
			"planner_status": WAREHOUSE_CAMPAIGN_COMPLETE,
			"campaign_id": campaign.get("campaign_id"),
			"item": item,
			"warehouse": warehouse,
			"from_datetime": str(from_dt),
			"n_moves": len(moves),
			"n_pairs": campaign.get("n_pairs"),
			"replays": [
				{
					"item": r["item"],
					"warehouse": r["warehouse"],
					"ok": (r.get("replay") or {}).get("ok"),
					"written": (r.get("replay") or {}).get("written"),
				}
				for r in replays
			],
			"synced_transfers": synced,
			"gl": gl,
			"checkpoint": checkpoint,
			"after_plan_status": after.get("planner_status"),
			"after_eligible": after.get("eligible"),
			"sql_updates_executed": sum(int((r.get("replay") or {}).get("written") or 0) for r in replays)
			+ len(moves)
			+ int(gl.get("rebuilt") or 0),
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"riv": "NOT_INVOKED",
			"idempotent_hint": after.get("planner_status")
			not in (READY_WAREHOUSE_CAMPAIGN, READY_WAREHOUSE_REPLAY),
		}
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		return {
			"ok": False,
			"aborted": True,
			"dry_run": False,
			"error": str(exc),
			"checkpoint": checkpoint,
		}
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def _public(campaign: dict) -> dict:
	return {
		k: campaign.get(k)
		for k in (
			"campaign_id",
			"item",
			"warehouse",
			"n_pairs",
			"planner_status",
			"reason",
			"eligible",
			"expected_sql",
			"expected_replay",
			"expected_risk",
			"multi_identity",
		)
	}


def _update_posting_datetime(voucher_no: str, new_dt) -> None:
	from erpnext_extensions.iran_accounting.stock_posting_order.ordering import format_date, format_time

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
			rebuilt.append(
				{"voucher": vn, "out": {k: out.get(k) for k in ("ok", "status", "written") if isinstance(out, dict)}}
			)
		except Exception as exc:
			skipped.append({"voucher": vn, "error": str(exc)})
	return {"rebuilt": len(rebuilt), "skipped": skipped, "rows": rebuilt}
