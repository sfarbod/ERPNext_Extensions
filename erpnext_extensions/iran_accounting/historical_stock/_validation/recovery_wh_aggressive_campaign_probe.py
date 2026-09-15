# Copyright (c) 2026 — include CROSS_ITEM in warehouse campaign simulation
from __future__ import annotations

import json


COMPANY = "اسپاد فارمد دارو"
ITEM = "30300014"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"
INCLUDE_OPTS = (
	"CROSS_TIME_REPAIRABLE",
	"REPAIRABLE_SECONDS",
	"SAME_TIME_REPAIRABLE",
	"MIDNIGHT_REVIEW",
	"CROSS_ITEM_CONFLICT",
)


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.simulator import (
		simulate_warehouse_replay,
	)
	from frappe.utils import get_datetime, flt

	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	rows = []
	for raw in scan.get("rows") or []:
		if (raw.get("item") or raw.get("item_code")) != ITEM:
			continue
		if raw.get("warehouse") != WH:
			continue
		r = attach_plan(dict(raw), cache=cache)
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		if opt not in INCLUDE_OPTS:
			continue
		rows.append(r)

	proposed = {}
	from_candidates = []
	pairs = []
	for r in rows:
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		moves = list(r.get("moves") or [])
		# For CROSS_ITEM / MIDNIGHT without moves, synthesize inbound-before-outbound
		# using proposed times or current inbound + 1s for outbound
		if not moves and r.get("inbound_document") and r.get("outbound_document"):
			in_t = r.get("proposed_inbound_time") or r.get("current_inbound_time")
			out_t = r.get("proposed_outbound_time")
			if not out_t and in_t:
				# place outbound 1s after inbound
				from datetime import timedelta
				out_t = get_datetime(in_t) + timedelta(seconds=1)
			if in_t and out_t:
				# only move outbound (keep inbound), classic pair fix
				moves = [
					{
						"document": r["outbound_document"],
						"old": r.get("current_outbound_time"),
						"new": out_t,
					}
				]
				# if inbound is currently after outbound, also ensure inbound stays
				if get_datetime(r.get("current_inbound_time") or in_t) > get_datetime(
					r.get("current_outbound_time") or out_t
				):
					# inbound should move earlier than outbound — use current outbound as inbound base
					pass
		for m in moves:
			if m.get("document") and m.get("new"):
				proposed[m["document"]] = get_datetime(m["new"])
		if r.get("inbound_document") and r.get("proposed_inbound_time"):
			proposed.setdefault(r["inbound_document"], get_datetime(r["proposed_inbound_time"]))
		if r.get("outbound_document") and r.get("proposed_outbound_time"):
			proposed.setdefault(r["outbound_document"], get_datetime(r["proposed_outbound_time"]))
		for t in (
			r.get("current_inbound_time"),
			r.get("current_outbound_time"),
			r.get("proposed_inbound_time"),
			r.get("proposed_outbound_time"),
		):
			if t:
				from_candidates.append(get_datetime(t))
		pairs.append(
			{
				"opt": opt,
				"in": r.get("inbound_document"),
				"out": r.get("outbound_document"),
				"moves": [
					{"doc": m.get("document"), "old": str(m.get("old")), "new": str(m.get("new"))}
					for m in moves
				],
				"min_b": r.get("min_qty_before"),
				"min_a": r.get("min_qty_after"),
				"conf": r.get("confidence"),
				"reason": (r.get("reason") or "")[:100],
			}
		)

	# Also try forcing CROSS_ITEM: move outbound after inbound current time
	# Build aggressive reorder: every outbound after its inbound
	aggressive = dict(proposed)
	for r in rows:
		inn = r.get("inbound_document")
		out = r.get("outbound_document")
		in_t = r.get("current_inbound_time") or r.get("proposed_inbound_time")
		if inn and out and in_t:
			from datetime import timedelta
			aggressive[out] = get_datetime(in_t) + timedelta(seconds=1)
			# keep inbound at current (or earlier if we have proposed)
			if r.get("proposed_inbound_time"):
				aggressive[inn] = get_datetime(r["proposed_inbound_time"])
			else:
				aggressive[inn] = get_datetime(in_t)

	from_dt = min(from_candidates) if from_candidates else None
	sims = {}
	for label, times in (("with_moves", proposed), ("aggressive_all_pairs", aggressive)):
		if not from_dt or not times:
			sims[label] = {"ok": False, "reason": "missing"}
			continue
		sim = simulate_warehouse_replay(ITEM, WH, from_dt, proposed_times=times)
		sims[label] = {
			"n_moves": len(times),
			"from_dt": str(from_dt),
			"sim_ok": sim.get("ok"),
			"final_qty_unchanged": sim.get("final_qty_unchanged"),
			"final_qty_current": sim.get("final_qty_current"),
			"final_qty_proposed": sim.get("final_qty_proposed"),
			"idempotent": sim.get("idempotent"),
			"neg_qty": sim.get("negative_qty_vouchers") or [],
			"neg_incoming": sim.get("negative_incoming_vouchers") or [],
			"exploded": sim.get("exploded_rate_vouchers") or [],
			"row_count": sim.get("row_count"),
			"expected_bin": sim.get("expected_bin"),
			"first_divergence": sim.get("first_divergence"),
			"clears": bool(
				sim.get("ok")
				and sim.get("final_qty_unchanged")
				and sim.get("idempotent")
				and not (sim.get("negative_qty_vouchers") or [])
				and not (sim.get("negative_incoming_vouchers") or [])
				and not (sim.get("exploded_rate_vouchers") or [])
			),
			"proposed_sample": {k: str(v) for k, v in list(times.items())[:12]},
		}

	# Opening before window
	import frappe
	prev = frappe.db.sql(
		"""
		SELECT voucher_no, qty_after_transaction, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime < %s
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		(ITEM, WH, from_dt),
		as_dict=True,
	)
	out = {
		"n_pairs": len(rows),
		"pairs": pairs,
		"opening_before": prev[0] if prev else None,
		"sims": sims,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:16000])
	return out
