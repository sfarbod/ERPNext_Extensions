# Copyright (c) 2026, ERPNext Extensions contributors
"""Probe + optionally apply the 6 terminal I4 / known RIV poison identities."""

from __future__ import annotations

import json
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
	classify_i4_row,
	preview_i4_replay,
	repair_i4_selected,
)
from erpnext_extensions.iran_accounting.historical_stock.poisoned_opening import (
	analyze_identity_opening,
)

OUT = Path(
	frappe.get_site_path("private", "files", "hr_correction_20260930")
)

IDENTITIES = [
	("13200107", "انبار پایکار خط تولید اسپاد فارمد"),
	("13200177", "انبار پایکار خط تولید اسپاد فارمد"),
	("30300014", "انبار Quarantine محصول نیمه ساخته اسپاد"),
	("13200410", "انبار پایکار خط تولید اسپاد فارمد"),
	("30200020", "انبار پایکار خط تولید اسپاد فارمد"),
	("30100033", "انبارک انتقال فیلینگ به اتوکلاو - E"),
]


def _tip(item, warehouse) -> dict:
	row = frappe.db.sql(
		"""
		SELECT name, voucher_no, posting_datetime,
		       ROUND(qty_after_transaction,6) q,
		       ROUND(stock_value,2) sv,
		       ROUND(valuation_rate,2) vr
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(item, warehouse),
		as_dict=True,
	)
	return row[0] if row else {}


def probe(apply: bool = False) -> dict:
	rows = []
	for item, wh in IDENTITIES:
		c = classify_i4_row(item, wh)
		from_dt = c.get("posting_datetime")
		sim = preview_i4_replay(item, wh, from_dt=from_dt) or {}
		poison_before = analyze_identity_opening(item, wh)
		tip_before = _tip(item, wh)
		entry = {
			"item": item,
			"warehouse": wh,
			"i4_status": c.get("i4_status") or c.get("status"),
			"eligible": c.get("eligible"),
			"patient_voucher": (c.get("patient_zero") or {}).get("voucher_no") or c.get("voucher"),
			"from_dt": from_dt,
			"pz_clears": c.get("pz_residual_clears_in_sim"),
			"previous_healthy": c.get("previous_healthy"),
			"checkpoint": c.get("checkpoint_repair"),
			"sim_summary": {
				k: sim.get(k)
				for k in (
					"ok",
					"clears",
					"tip_qty",
					"tip_value",
					"final_qty",
					"final_value",
					"error",
					"reason",
				)
				if k in sim
			}
			or {k: sim.get(k) for k in list(sim)[:15]},
			"tip_before": {
				"voucher": tip_before.get("voucher_no"),
				"q": flt(tip_before.get("q")),
				"sv": flt(tip_before.get("sv")),
				"vr": flt(tip_before.get("vr")),
			},
			"poison_before": {
				"status": poison_before.get("status"),
				"origin": poison_before.get("origin"),
			},
		}
		rows.append(entry)

	result = {"dry_run": not apply, "count": len(rows), "rows": rows}
	if apply:
		apply_rows = [
			{"item": r["item"], "warehouse": r["warehouse"]}
			for r in rows
			if r.get("eligible") or r.get("i4_status") == "READY_I4"
		]
		applied = repair_i4_selected(apply_rows, dry_run=False)
		result["apply"] = {
			"requested": len(apply_rows),
			"result_keys": sorted((applied or {}).keys()),
			"summary": {
				k: applied.get(k)
				for k in ("repaired", "failed", "skipped", "count", "ok")
				if k in (applied or {})
			},
			"raw_status_counts": applied.get("by_status") or applied.get("status_counts"),
		}
		# Re-measure tips
		after = []
		for item, wh in IDENTITIES:
			tip = _tip(item, wh)
			poison = analyze_identity_opening(item, wh)
			i4 = classify_i4_row(item, wh)
			after.append(
				{
					"item": item,
					"tip": {
						"voucher": tip.get("voucher_no"),
						"q": flt(tip.get("q")),
						"sv": flt(tip.get("sv")),
						"vr": flt(tip.get("vr")),
					},
					"poison_status": poison.get("status"),
					"poison_origin": poison.get("origin"),
					"i4_status": i4.get("i4_status") or i4.get("status"),
					"tip_clear": abs(flt(tip.get("q"))) < 1e-9 and abs(flt(tip.get("sv"))) <= 1.0,
					"tip_healthy": abs(flt(tip.get("q"))) > 1e-9
					and abs(flt(tip.get("sv"))) > 1
					and abs(flt(tip.get("vr"))) > 1e-9,
				}
			)
		result["after"] = after
		result["cleared_n"] = sum(1 for a in after if a["tip_clear"] or a["tip_healthy"])
		result["still_blocking_poison_n"] = sum(
			1 for a in after if a["poison_status"] == "TECHNICAL_TOOL_GAP"
		)

	OUT.mkdir(parents=True, exist_ok=True)
	name = "i4_six_apply.json" if apply else "i4_six_probe.json"
	(OUT / name).write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
	result["path"] = str(OUT / name)
	return result
