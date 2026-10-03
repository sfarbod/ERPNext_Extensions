# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-3 finalize: PO check, analyzers, inventory, preflight, leftover MA."""

from __future__ import annotations

import json
from pathlib import Path

import frappe

from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_reconcile_0930 import (
	leftover_ma_status,
	run_preflight,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase3_iran_native_waves_0930 import (
	_canaries,
	inventory_iran_native_roots,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase3_toolgap_matrix_0930 import (
	analyze_i4_blockers,
	build_wr_toolgap_matrix,
)
from erpnext_extensions.iran_accounting.historical_stock.failed_riv_causal import (
	analyze_failed_riv_actionable,
)
from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def check_posting_order() -> dict:
	scan = run_full_history_scan(company=COMPANY)
	rows = scan.get("rows") or []
	actionable = [
		r
		for r in rows
		if str(r.get("optimizer_status") or r.get("status") or "") != "NO_REPAIR_NEEDED"
	]
	pair = [
		r
		for r in rows
		if {r.get("inbound_document"), r.get("outbound_document")}
		& {"MAT-STE-2026-33163", "MAT-STE-2026-33338"}
		or {r.get("inbound_document"), r.get("outbound_document")}
		== {"MAT-STE-2026-33163", "MAT-STE-2026-33338"}
	]
	# also search full including dropped — re-scan won't include dropped; check summary
	summary = scan.get("summary") or {}
	out = {
		"actionable_n": len(actionable),
		"independent_production_flows": summary.get("independent_production_flows"),
		"no_repair_needed": summary.get("NO_REPAIR_NEEDED"),
		"pair_still_actionable": [
			{
				"in": r.get("inbound_document"),
				"out": r.get("outbound_document"),
				"status": r.get("status"),
				"opt": r.get("optimizer_status"),
				"cross_time_class": r.get("cross_time_class"),
				"owo": r.get("outbound_work_order"),
				"iwo": r.get("inbound_work_order"),
			}
			for r in actionable
			if {r.get("inbound_document"), r.get("outbound_document")}
			== {"MAT-STE-2026-33163", "MAT-STE-2026-33338"}
			or r.get("inbound_document") in ("MAT-STE-2026-33163", "MAT-STE-2026-33338")
			or r.get("outbound_document") in ("MAT-STE-2026-33163", "MAT-STE-2026-33338")
		],
		"actionable_sample": [
			{
				"in": r.get("inbound_document"),
				"out": r.get("outbound_document"),
				"status": r.get("optimizer_status") or r.get("status"),
				"cls": r.get("cross_time_class"),
			}
			for r in actionable[:10]
		],
		"summary_keys": {k: summary.get(k) for k in list(summary)[:20]},
	}
	_dump("phase3_posting_order.json", out)
	return out


def run_finalize() -> dict:
	po = check_posting_order()
	matrix = build_wr_toolgap_matrix()
	i4 = analyze_i4_blockers()
	fr = analyze_failed_riv_actionable(company=COMPANY, limit=500)
	_dump("phase3_failed_riv_causal.json", fr)
	inv = inventory_iran_native_roots()
	lma = leftover_ma_status()
	pre = run_preflight()
	can = _canaries()
	out = {
		"posting_order": po,
		"matrix_top": matrix.get("top_families"),
		"raw_tool_gap": matrix.get("raw_tool_gap"),
		"i4": i4,
		"failed_riv": {
			"actionable_n": fr.get("actionable_n"),
			"by_status": fr.get("by_status"),
			"causal_roots_n": fr.get("causal_roots_n"),
		},
		"iran_inventory": inv,
		"leftover_ma": lma,
		"preflight": pre,
		"canaries": can,
	}
	_dump("phase3_finalize.json", out)
	return out
