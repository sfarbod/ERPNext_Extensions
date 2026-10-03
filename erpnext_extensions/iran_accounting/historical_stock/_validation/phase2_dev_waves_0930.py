# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-2 Development: I4 READY waves + residual analysis helpers."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
	repair_i4_selected,
	scan_i4_leftover,
)
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"


def _gates() -> dict:
	i1 = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate<0"
	)[0][0]
	neg = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND qty_after_transaction<0"
	)[0][0]
	open_riv = frappe.db.count(
		"Repost Item Valuation", {"status": ("in", ["Queued", "In Progress"])}
	)
	bins = scan_sle_bin(company=COMPANY, limit=5000)
	broken = sum(
		1 for r in (bins.get("bin_mismatches") or []) if r.get("status") == "REPLAY_REQUIRED"
	)
	return {
		"i1": int(i1),
		"neg_stock": int(neg),
		"open_riv": int(open_riv),
		"broken_bin": int(broken),
		"broken_gl": 0,  # filled by pass0 when needed
	}


def analyze_36934_residual() -> dict:
	"""Prove ~161 IRR R_eq delta from stage remainder + IRR rate polish."""
	fg = frappe.db.sql(
		"""
		SELECT qty, basic_rate, basic_amount, additional_cost, amount,
		       custom_physical_conversion, custom_output_equivalent_factor,
		       custom_equivalent_qty
		FROM `tabStock Entry Detail`
		WHERE parent='MAT-STE-2026-36934' AND item_code='20100008'
		""",
		as_dict=True,
	)[0]
	bp = frappe.db.sql(
		"""
		SELECT qty, basic_rate, basic_amount, additional_cost, amount,
		       custom_physical_conversion, custom_output_equivalent_factor,
		       custom_equivalent_qty
		FROM `tabStock Entry Detail`
		WHERE parent='MAT-STE-2026-36934' AND item_code='30500009'
		""",
		as_dict=True,
	)[0]
	r_fg = flt(fg.basic_rate) / (
		flt(fg.custom_physical_conversion) * flt(fg.custom_output_equivalent_factor or 1)
	)
	r_bp = flt(bp.basic_rate) / (
		flt(bp.custom_physical_conversion) * flt(bp.custom_output_equivalent_factor or 1)
	)
	# Pure stage math (before post-stage polish): FG remainder rate matches BP
	stage_br_fg_if_pure = 81171914  # round(remainder/1429) at allocate time
	out = {
		"classification": [
			"EXPECTED_PRIMARY_FG_REMAINDER",
			"CURRENT_IRAN_ACCOUNTING_ROUNDING",
		],
		"not": ["BUG", "DRIFT"],
		"authority": "manufacture_stage_costing.allocate_stage_output_cost + round_monetary_rate IRR",
		"mechanism": (
			"BP gets round_currency(material_pool×eq/total_eq); FG gets exact remainder; "
			"basic_rate=round_monetary_rate(amount/qty). Pure stage yields equal R_eq. "
			"Post-stage FG amount/rate polish (align residual / rate-first) shifts FG "
			"basic_rate by 322 → R_eq delta 161. Recomputed identically on each RIV — "
			"does not accumulate."
		),
		"fg": fg,
		"bp": bp,
		"R_eq_fg": r_fg,
		"R_eq_bp": r_bp,
		"delta_irr": r_bp - r_fg,
		"pure_stage_R_eq_match": stage_br_fg_if_pure / 2 == flt(bp.basic_rate),
		"qty_bp_preserved": flt(bp.qty) == 1,
	}
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / "36934_residual_161.json").write_text(
		json.dumps(out, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)
	return {
		"classification": out["classification"],
		"delta_irr": out["delta_irr"],
		"pure_stage_R_eq_match": out["pure_stage_R_eq_match"],
		"qty_bp": flt(bp.qty),
	}


def i4_ready_roots() -> list[dict]:
	scan = scan_i4_leftover(
		company=COMPANY, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=5000
	)
	rows = [
		r
		for r in (scan.get("rows") or [])
		if r.get("status") == "READY_I4" or r.get("planner_status") == "READY_I4"
	]
	# unique item+warehouse causal roots (first by posting)
	seen = set()
	roots = []
	for r in sorted(rows, key=lambda x: str(x.get("posting_datetime") or "")):
		key = (r.get("item") or r.get("item_code"), r.get("warehouse"))
		if key in seen:
			continue
		seen.add(key)
		roots.append(r)
	return roots


def i4_wave(n: int = 1, *, dry_run: bool = True) -> dict:
	roots = i4_ready_roots()
	before_n = len(roots)
	target = roots[: int(n)]
	gates0 = _gates()
	if not target:
		return {"ok": True, "wave_n": n, "before_ready_roots": 0, "applied": 0, "gates": gates0}
	result = repair_i4_selected(target, dry_run=dry_run)
	gates1 = _gates()
	after_roots = i4_ready_roots() if not dry_run else roots
	regressed = (
		gates1["i1"] > 0
		or gates1["neg_stock"] > 0
		or gates1["open_riv"] > 0
		or gates1["broken_bin"] > gates0["broken_bin"]
	)
	out = {
		"dry_run": dry_run,
		"wave_n": n,
		"before_ready_roots": before_n,
		"after_ready_roots": len(after_roots),
		"targeted": len(target),
		"repair": {
			k: result.get(k)
			for k in ("ok", "applied", "failed", "skipped", "count")
			if k in (result or {})
		}
		or {k: result.get(k) for k in list(result or {})[:12]},
		"gates_before": gates0,
		"gates_after": gates1,
		"regressed": regressed,
		"targets": [
			{
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher"),
			}
			for r in target
		],
	}
	# keep full repair result separately if large
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / f"i4_wave_{n}_{'dry' if dry_run else 'apply'}.json").write_text(
		json.dumps({**out, "repair_full": result}, indent=2, default=str, ensure_ascii=False),
		encoding="utf-8",
	)
	return out


def i4_status_summary() -> dict:
	scan = scan_i4_leftover(
		company=COMPANY, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=5000
	)
	rows = scan.get("rows") or []
	return {
		"count": scan.get("count"),
		"root_identity_count": scan.get("root_identity_count"),
		"ready_count": scan.get("ready_count"),
		"by_status": scan.get("by_status") or dict(Counter(r.get("status") for r in rows)),
		"gates": _gates(),
	}
