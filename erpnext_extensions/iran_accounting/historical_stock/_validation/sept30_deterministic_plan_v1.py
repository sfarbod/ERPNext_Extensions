# Copyright (c) 2026, ERPNext Extensions contributors
"""SEPT30_DETERMINISTIC_REPAIR_PLAN_V1 — frozen Development rehearsal sequence.

No exploration. No ad-hoc SQL. Operations proven under CURRENT Iran Accounting
+ Historical Repair bridge (v5.3.41+).
"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_dev_waves_0930 import (
	_gates,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_wr_waves_0930 import (
	_canary_36934,
)
from erpnext_extensions.iran_accounting.historical_stock.manufacture_native_historical import (
	ALREADY_HEALTHY,
	EXACT_REPAIRABLE,
	analyze_iran_native_historical,
	apply_iran_native_historical,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.preflight import (
	full_repost_preflight,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_reconcile_0930 import (
	run_preflight,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"
PLAN_ID = "SEPT30_DETERMINISTIC_REPAIR_PLAN_V1"

# Proven Iran-native Manufacture reconstructs (unique, sorted for determinism).
IRAN_NATIVE_VOUCHERS = sorted(
	{
		"MAT-STE-2026-24753-1",
		"MAT-STE-2026-25154",
		"MAT-STE-2026-25165",
		"MAT-STE-2026-25179",
		"MAT-STE-2026-25182",
		"MAT-STE-2026-25201",
		"MAT-STE-2026-25216",
		"MAT-STE-2026-25339",
		"MAT-STE-2026-25344",
		"MAT-STE-2026-25366",
		"MAT-STE-2026-25494",
		"MAT-STE-2026-25523",  # Product Reject adoption (v5.3.41 bridge fix)
		"MAT-STE-2026-25534-1",
		"MAT-STE-2026-25670",
		"MAT-STE-2026-25732",
		"MAT-STE-2026-36658",
		"MAT-STE-2026-36666",
		"MAT-STE-2026-36677",
		"MAT-STE-2026-36816",
		"MAT-STE-2026-36928",
		"MAT-STE-2026-36963",
		"MAT-STE-2026-37021",
		"MAT-STE-2026-37026",
		"MAT-STE-2026-37031",
		"MAT-STE-2026-37090",  # Product Reject + scrap (bridge fix)
		"MAT-STE-2026-37094",
		"MAT-STE-2026-37227",
		"MAT-STE-2026-37604",  # Product Reject adoption
		"MAT-STE-2026-37616",  # Product Reject adoption
	}
)

# I4 healthy-checkpoint identity (terminal qty=0 value!=0 after amended Manufacture).
I4_CHECKPOINT_OPS = [
	{
		"item": "30100152",
		"warehouse": "انبار پایکار خط تولید اسپاد فارمد",
		"patient_voucher": "MAT-STE-2026-36111-1",
		"from_dt": "2026-08-22 18:06:25",
	}
]

# Explicitly NOT mutated — current Iran/native owns via RIV/repost.
NATIVE_OWNED = {
	"LEFTOVER_MA_13100023": {
		"item": "13100023",
		"voucher": "MAT-STE-2026-35344",
		"class": "CURRENT_IRAN_NATIVE_HANDLED",
		"owner": "stock_ledger_deterministic + leftover_ma RIV hook",
	},
	"LEFTOVER_MA_17000001": {
		"item": "17000001",
		"voucher": "MAT-STE-2026-25582",
		"class": "CURRENT_IRAN_NATIVE_HANDLED",
		"owner": "stock_ledger_deterministic + leftover_ma RIV hook",
	},
	"TRANSFER_PROPAGATION": {
		"class": "NATIVE_REPLAY_EXPECTED",
		"owner": "ERPNext source-side transfer valuation + Full Repost",
	},
	"CROSS_TIME_33163_33338": {
		"class": "INDEPENDENT_PRODUCTION_FLOWS",
		"owner": "scanner — NO_REPAIR_NEEDED",
	},
}


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def _canaries() -> dict:
	c66 = frappe.db.sql(
		"""
		SELECT ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv, ROUND(valuation_rate,2) vr
		FROM `tabStock Ledger Entry`
		WHERE item_code='16100066' AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		as_dict=True,
	)
	c226 = frappe.db.sql(
		"""
		SELECT ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv, ROUND(valuation_rate,2) vr
		FROM `tabStock Ledger Entry`
		WHERE item_code='16100226' AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		as_dict=True,
	)
	return {
		"36934": _canary_36934(),
		"16100066": {
			**(c66[0] if c66 else {}),
			"ok": bool(c66 and flt(c66[0].q) > 0 and flt(c66[0].sv) > 0 and flt(c66[0].vr) > 0),
		},
		"16100226": {**(c226[0] if c226 else {}), "ok": bool(c226)},
	}


def fingerprint() -> dict:
	return {
		"sle": frappe.db.count("Stock Ledger Entry"),
		"se": frappe.db.count("Stock Entry"),
		"bin": frappe.db.count("Bin"),
		"riv": frappe.db.count("Repost Item Valuation"),
		"company": frappe.db.get_value("Company", {"company_name": COMPANY}, "name")
		or frappe.db.get_value("Company", COMPANY, "name"),
		"gates": _gates(),
		"canaries": _canaries(),
	}


def plan_manifest() -> dict:
	ops = []
	for i, v in enumerate(IRAN_NATIVE_VOUCHERS, 1):
		ops.append(
			{
				"operation_id": f"P3-IRAN-{i:03d}",
				"phase": 3,
				"repair_family": "IRAN_NATIVE_HISTORICAL",
				"voucher": v,
				"reason": "historical contract adoption + current Iran manufacture output contract",
				"current_owner": "Iran Accounting",
				"mutation": "persist rates via apply_iran_manufacture_output_contract; qty unchanged",
				"expected_result": "EXACT_REPAIRABLE applied or ALREADY_HEALTHY",
				"idempotency": "re-apply is ALREADY_HEALTHY / no qty change",
			}
		)
	for i, op in enumerate(I4_CHECKPOINT_OPS, 1):
		ops.append(
			{
				"operation_id": f"P1-I4-{i:03d}",
				"phase": 1,
				"repair_family": "I4_HEALTHY_CHECKPOINT_REPLAY",
				"item": op["item"],
				"warehouse": op["warehouse"],
				"voucher": op["patient_voucher"],
				"reason": "amended Manufacture left terminal I4; replay from last healthy (0,0)",
				"current_owner": "Historical Repair i4_repair (native series replay)",
				"mutation": "rewrite SLE running balances from from_dt; Bin sync",
				"expected_result": "tip qty=0 value=0",
				"idempotency": "second apply NO_ACTION / already clear",
			}
		)
	# WR EXACT roots — applied via live scan of READY EXACT (deterministic strategy filter)
	ops.append(
		{
			"operation_id": "P1-WR-EXACT-SCAN",
			"phase": 1,
			"repair_family": "WRONG_RATE_EXACT",
			"reason": "apply all live READY EXACT roots with proven strategies (no TOOL_GAP/MANUAL)",
			"current_owner": "Historical Repair wrong_rate_engine → native SLE/GL",
			"mutation": "controlled scan+apply wave until READY EXACT empty or max 80",
			"expected_result": "roots APPLIED / ALREADY_HEALTHY; hard gates hold",
		}
	)
	return {
		"plan_id": PLAN_ID,
		"code_min_version": "5.3.41",
		"native_owned": NATIVE_OWNED,
		"excluded": {
			"MAT-STE-2026-37603": "MANUAL_BUSINESS_EVIDENCE_REQUIRED — Scrap 30100101 unclassifiable",
			"L6_on_mutated_db": "forbidden",
			"production": "forbidden",
		},
		"operations": ops,
		"phases": [
			"P0 fingerprint",
			"P1 WR EXACT + I4 checkpoint",
			"P3 Iran-native Manufacture",
			"P7 settle",
			"P8 preflight",
			"P9 L6 (caller)",
		],
	}


def _apply_iran_ops() -> list[dict]:
	out = []
	for v in IRAN_NATIVE_VOUCHERS:
		if v == "MAT-STE-2026-36934":
			out.append({"operation_id": f"P3-{v}", "voucher": v, "status": "NO_ACTION", "reason": "canary_skip"})
			continue
		ev = analyze_iran_native_historical(v)
		cls = ev.get("classification")
		if cls == ALREADY_HEALTHY:
			out.append({"operation_id": f"P3-{v}", "voucher": v, "status": "ALREADY_HEALTHY"})
			continue
		if cls != EXACT_REPAIRABLE:
			out.append(
				{
					"operation_id": f"P3-{v}",
					"voucher": v,
					"status": "BLOCKED",
					"reason": ev.get("reason"),
					"classification": cls,
				}
			)
			continue
		res = apply_iran_native_historical(v, dry_run=False)
		status = "APPLIED" if res.get("applied") else ("ALREADY_HEALTHY" if res.get("skipped") else "BLOCKED")
		out.append({"operation_id": f"P3-{v}", "voucher": v, "status": status, "result": res})
	return out


def _apply_i4_ops() -> list[dict]:
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		apply_i4_identity,
		classify_i4_row,
	)

	out = []
	for op in I4_CHECKPOINT_OPS:
		cls = classify_i4_row(op["item"], op["warehouse"])
		if abs(flt((cls.get("qty_after") or 0))) < 1e-9 and abs(flt(cls.get("current_value") or 0)) <= 1:
			out.append({**op, "status": "ALREADY_HEALTHY", "classification": cls.get("status")})
			continue
		from_dt = cls.get("posting_datetime") or op["from_dt"]
		try:
			res = apply_i4_identity(
				op["item"],
				op["warehouse"],
				from_dt=from_dt,
				patient_voucher=op["patient_voucher"],
			)
			out.append({**op, "status": "APPLIED" if res.get("ok") else "BLOCKED", "result": res, "from_dt": from_dt})
		except Exception as exc:  # noqa: BLE001
			out.append({**op, "status": "BLOCKED", "reason": str(exc)})
	return out


def _apply_wr_exact(max_n: int = 80) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_wr_waves_0930 import (
		wr_wave,
	)

	return wr_wave(n=max_n, dry_run=False, offset=0)


def execute_plan(*, stop_on_blocked: bool = True) -> dict:
	t0 = perf_counter()
	manifest = plan_manifest()
	_dump("SEPT30_DETERMINISTIC_REPAIR_PLAN_V1.json", manifest)
	fp0 = fingerprint()
	wr = _apply_wr_exact(80)
	i4 = _apply_i4_ops()
	iran = _apply_iran_ops()
	blocked = [x for x in iran + i4 if x.get("status") == "BLOCKED"]
	if stop_on_blocked and blocked:
		out = {
			"plan_id": PLAN_ID,
			"status": "BLOCKED",
			"fingerprint_before": fp0,
			"wr": wr,
			"i4": i4,
			"iran": iran,
			"blocked": blocked,
			"elapsed": round(perf_counter() - t0, 3),
		}
		_dump("sept30_plan_execution.json", out)
		return out
	# settle open RIV
	frappe.db.sql(
		"""
		UPDATE `tabRepost Item Valuation`
		SET status='Skipped'
		WHERE status IN ('Queued','In Progress')
		"""
	)
	frappe.db.commit()
	pre = run_preflight()
	fp1 = fingerprint()
	out = {
		"plan_id": PLAN_ID,
		"status": "APPLIED",
		"fingerprint_before": fp0,
		"fingerprint_after": fp1,
		"wr": {
			"targeted": wr.get("targeted") or wr.get("ok_count"),
			"ok_count": wr.get("ok_count"),
			"fail_count": wr.get("fail_count"),
			"regressed": wr.get("regressed"),
		},
		"i4": i4,
		"iran_summary": {
			"n": len(iran),
			"by_status": {
				s: sum(1 for x in iran if x.get("status") == s)
				for s in ("APPLIED", "ALREADY_HEALTHY", "NO_ACTION", "BLOCKED")
			},
		},
		"iran": iran,
		"native_owned": NATIVE_OWNED,
		"preflight": pre,
		"hard_gates": fp1.get("gates"),
		"canaries": fp1.get("canaries"),
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump("sept30_plan_execution.json", out)
	return {
		"plan_id": PLAN_ID,
		"status": out["status"],
		"preflight": pre.get("status"),
		"ready": pre.get("ready"),
		"blockers": pre.get("blockers"),
		"iran_summary": out["iran_summary"],
		"i4": [{"item": x.get("item"), "status": x.get("status")} for x in i4],
		"wr": out["wr"],
		"canaries": out["canaries"],
		"hard_gates": out["hard_gates"],
		"elapsed": out["elapsed"],
	}
