# Copyright (c) 2026 — diagnose MANUAL I4 that fail residual clear
from __future__ import annotations

import json
import os

import frappe
from frappe.utils import flt, get_datetime, nowdate

ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_v5216/i4"
)


def run():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
		_fetch_previous,
		_fetch_sles,
		replay_series,
		sle_poison_reason,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

	s = scan_i4_leftover(company="اسپاد فارمد دارو", from_date="2026-03-21", to_date=nowdate(), limit=5000)
	manual = [
		r
		for r in s["rows"]
		if r.get("i4_status") == "MANUAL" and r.get("previous_healthy") and not r.get("pz_residual_clears_in_sim")
	]
	out = []
	for r in manual:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			r["sle"],
			[
				"name",
				"voucher_no",
				"actual_qty",
				"qty_after_transaction",
				"stock_value",
				"stock_value_difference",
				"incoming_rate",
				"outgoing_rate",
				"valuation_rate",
				"posting_datetime",
			],
			as_dict=True,
		)
		prev = _fetch_previous(r["item"], r["warehouse"], sle.posting_datetime)
		rows = _fetch_sles(r["item"], r["warehouse"], get_datetime(sle.posting_datetime), before=False)
		opening_qty = D(prev.qty_after_transaction) if prev else D(0)
		opening_value = D(prev.stock_value) if prev else D(0)
		series = replay_series(rows[:1], opening_qty, opening_value) if rows else []
		step = series[0] if series else {}
		out.append(
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"warehouse": r.get("warehouse"),
				"sle_actual_qty": flt(sle.actual_qty),
				"sle_qty_after": flt(sle.qty_after_transaction),
				"sle_stock_value": flt(sle.stock_value),
				"sle_svd": flt(sle.stock_value_difference),
				"sle_incoming": flt(sle.incoming_rate),
				"sle_outgoing": flt(sle.outgoing_rate),
				"prev_qty_after": flt(prev.qty_after_transaction) if prev else None,
				"prev_stock_value": flt(prev.stock_value) if prev else None,
				"prev_valuation_rate": flt(prev.valuation_rate) if prev else None,
				"sim_qty_after": flt(step.get("qty_after_transaction") or 0) if step else None,
				"sim_stock_value": flt(step.get("stock_value") or 0) if step else None,
				"poison": sle_poison_reason(sle),
				"pattern": _pattern(sle, prev, step),
			}
		)
	from collections import Counter

	summary = {
		"count": len(out),
		"patterns": dict(Counter(x["pattern"] for x in out)),
		"rows": out,
	}
	os.makedirs(ARTIFACT, exist_ok=True)
	path = os.path.join(ARTIFACT, "manual_no_clear_diagnosis.json")
	with open(path, "w", encoding="utf-8") as f:
		json.dump(summary, f, indent=2, default=str, ensure_ascii=False)
	print("wrote", path)
	print("patterns", summary["patterns"])
	return summary


def _pattern(sle, prev, step):
	aq = flt(sle.actual_qty)
	if abs(aq) <= 0.0001:
		return "ZERO_QTY_MOVEMENT_LEFTOVER"
	if aq < 0:
		return "OUTBOUND_LEFTOVER"
	if aq > 0 and abs(flt(sle.incoming_rate)) < 0.0001:
		return "INBOUND_ZERO_RATE_LEFTOVER"
	if step and abs(flt(step.get("stock_value") or 0)) > 1:
		return "SIM_RESIDUAL_REMAINS"
	return "OTHER"
