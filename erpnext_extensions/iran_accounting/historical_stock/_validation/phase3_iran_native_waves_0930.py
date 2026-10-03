# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-3: Iran native historical canary + waves for manufacture tool-gap roots."""

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
		"16100226": {
			**(c226[0] if c226 else {}),
			"ok": bool(c226),  # valued tip is OK; zero-rate alone not corruption
		},
	}


def inventory_iran_native_roots() -> dict:
	"""Unique Manufacture vouchers from WR tool-gap families needing Iran native."""
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import wrong_rate_bucket
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase3_toolgap_matrix_0930 import (
		_family_for_row,
		_voucher_iran_profile,
	)

	scan = scan_wrong_rates(company=COMPANY, limit=3000)
	vouchers = set()
	profiles = {}
	for r in scan.get("rows") or []:
		if wrong_rate_bucket(r) not in ("tool_gap", "ready", "manual"):
			continue
		v = r.get("voucher") or r.get("voucher_no")
		if not v:
			continue
		if v not in profiles:
			profiles[v] = _voucher_iran_profile(v)
		if (profiles[v] or {}).get("purpose") != "Manufacture":
			continue
		fam = _family_for_row(r, profiles[v] or {})
		if fam in (
			"PRODUCT_REJECT",
			"VR_SCRAP",
			"BY_PRODUCT",
			"STAGE_CO_PRODUCT",
			"AFG",
			"STAGE_EQUIVALENT",
			"UNPROMOTED_LIKELY",
			"OTHER",
		):
			vouchers.add(v)

	ready = []
	healthy = []
	waiting = []
	manual = []
	for v in sorted(vouchers):
		if v == "MAT-STE-2026-36934":
			# canary already repaired — skip mutate
			ev = analyze_iran_native_historical(v)
			healthy.append({"voucher": v, "classification": ev.get("classification"), "canary": True})
			continue
		ev = analyze_iran_native_historical(v)
		cls = ev.get("classification")
		entry = {
			"voucher": v,
			"classification": cls,
			"families": ev.get("families"),
			"delta_n": ev.get("delta_n"),
			"reason": ev.get("reason"),
		}
		if cls == EXACT_REPAIRABLE:
			ready.append(entry)
		elif cls == ALREADY_HEALTHY:
			healthy.append(entry)
		elif cls == "WAITING_UPSTREAM":
			waiting.append(entry)
		else:
			manual.append(entry)

	out = {
		"vouchers_scanned": len(vouchers),
		"exact_repairable": len(ready),
		"already_healthy": len(healthy),
		"waiting": len(waiting),
		"manual": len(manual),
		"ready_roots": ready,
		"healthy_sample": healthy[:20],
		"waiting_sample": waiting[:20],
		"manual_sample": manual[:20],
	}
	_dump("phase3_iran_native_inventory.json", out)
	return {
		k: out[k]
		for k in (
			"vouchers_scanned",
			"exact_repairable",
			"already_healthy",
			"waiting",
			"manual",
		)
	} | {"ready_vouchers": [r["voucher"] for r in ready[:40]]}


def _load_ready() -> list[str]:
	path = OUT / "phase3_iran_native_inventory.json"
	if not path.exists():
		inventory_iran_native_roots()
	data = json.loads(path.read_text(encoding="utf-8"))
	# live filter
	live = []
	for r in data.get("ready_roots") or []:
		v = r["voucher"]
		ev = analyze_iran_native_historical(v)
		if ev.get("classification") == EXACT_REPAIRABLE:
			live.append(v)
	return live


def iran_native_wave(n: int = 1, *, dry_run: bool = True) -> dict:
	t0 = perf_counter()
	ready = _load_ready()
	target = ready[: int(n)]
	gates0 = _gates()
	can0 = _canaries()
	results = []
	for v in target:
		res = apply_iran_native_historical(v, dry_run=dry_run)
		results.append({"voucher": v, "result": res})
		if not (
			res.get("applied")
			or res.get("dry_run")
			or res.get("skipped") == "already_healthy"
			or (dry_run and res.get("ok"))
		):
			if not dry_run and not res.get("applied"):
				break
	gates1 = _gates()
	can1 = _canaries()
	regressed = (
		gates1["i1"] > 0
		or gates1["neg_stock"] > 0
		or gates1["broken_bin"] > gates0["broken_bin"]
		or not can1["36934"].get("qty_ok")
		or not can1["36934"].get("class_ok")
		or not can1["16100066"].get("ok")
	)
	out = {
		"dry_run": dry_run,
		"wave_n": n,
		"live_ready": len(ready),
		"targeted": len(target),
		"ok_count": sum(
			1
			for r in results
			if (r["result"].get("applied") or r["result"].get("dry_run") and r["result"].get("ok"))
		),
		"applied_count": sum(1 for r in results if r["result"].get("applied")),
		"results": results,
		"gates_before": gates0,
		"gates_after": gates1,
		"canaries_after": can1,
		"regressed": regressed,
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump(f"phase3_iran_wave_{n}_{'dry' if dry_run else 'apply'}.json", out)
	return {
		k: out[k]
		for k in (
			"dry_run",
			"wave_n",
			"live_ready",
			"targeted",
			"ok_count",
			"applied_count",
			"regressed",
			"gates_after",
			"canaries_after",
			"elapsed",
		)
	} | {"vouchers": [r["voucher"] for r in results]}
