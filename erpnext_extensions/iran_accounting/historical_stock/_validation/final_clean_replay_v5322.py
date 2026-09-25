# Copyright (c) 2026, ERPNext Extensions contributors
"""Final clean replay after restore (v5.3.22).

Applies proven deterministic roots only:
  posting-order MTFM negatives → leftover MA → Wrong Rate EXACT waves
(PR / SABB / reco provenance). Classification-only legitimacy fixes are
code-path (allow_zero, scrap WH, free-PR-after-depletion).

Does not invent rates, fabricate inventory, or change manufacturing qty.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import frappe
from frappe.utils import flt

COMPANY = "اسپاد فارمد دارو"
OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_final_5322"
)


def _gates():
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import (
		capture_safety_fingerprint,
	)

	fp = capture_safety_fingerprint()
	neg_after = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001"""
	)[0][0]
	neg_rate = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND (
			incoming_rate < -0.0001 OR outgoing_rate < -0.0001 OR valuation_rate < -0.0001
		)"""
	)[0][0]
	return {
		"i1": fp.get("i1"),
		"neg_qty": fp.get("neg_qty"),
		"bin_mismatch": fp.get("bin_value_mismatch"),
		"neg_after": int(neg_after),
		"neg_rate": int(neg_rate),
		"mfg": dict(fp.get("manufacturing") or {}),
	}


def _canaries():
	rates = {
		flt(x[0])
		for x in frappe.db.sql(
			"""SELECT ROUND(IFNULL(NULLIF(outgoing_rate,0), incoming_rate), 2)
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND is_cancelled=0
			  AND ABS(IFNULL(outgoing_rate,0)+IFNULL(incoming_rate,0)) > 0
			LIMIT 8""",
			"MAT-STE-2026-28696",
		)
	}
	svd = flt(
		frappe.db.sql(
			"""SELECT SUM(stock_value_difference) FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND is_cancelled=0""",
			"MAT-STE-2026-28696",
		)[0][0]
	)
	adj = frappe.db.sql(
		"""SELECT ROUND(debit,2) FROM `tabGL Entry`
		WHERE voucher_no=%s AND is_cancelled=0 AND account LIKE %s""",
		("MAT-STE-2026-30470", "%621301%"),
	)
	ma = flt(
		frappe.db.sql(
			"""SELECT valuation_rate FROM `tabBin` WHERE item_code=%s LIMIT 1""",
			"16100066",
		)[0][0]
	)
	z = frappe.db.sql(
		"""SELECT valuation_rate, stock_value, actual_qty FROM `tabBin`
		WHERE item_code=%s LIMIT 1""",
		"16100226",
	)[0]
	ok = all(abs(r - 346357) < 1 for r in rates) and abs(svd) < 1
	ok = ok and adj and abs(flt(adj[0][0]) - 8) < 0.01
	ok = ok and abs(ma - 5005344.521745) < 1.0
	ok = ok and abs(flt(z[0])) < 1e-6 and abs(flt(z[1])) < 1e-6
	return ok, {
		"28696_rates": sorted(rates),
		"28696_svd": svd,
		"30470_621301_debit": adj,
		"16100066_vr": ma,
		"16100226": {"vr": z[0], "sv": z[1], "qty": z[2]},
	}


def _abort_if_unsafe(tag, mfg0, *, require_canaries=True, allow_baseline_neg=False, allow_bin_mismatch=False):
	g = _gates()
	cok, cdet = _canaries()
	mdelta = {k: flt(g["mfg"].get(k)) - flt(mfg0.get(k)) for k in mfg0}
	payload = {"tag": tag, "gates": g, "canaries_ok": cok, "canaries": cdet, "mfg_delta": mdelta}
	bad = bool(g["i1"]) or bool(g["neg_rate"])
	if not allow_baseline_neg:
		bad = bad or bool(g["neg_after"])
	if not allow_bin_mismatch:
		bad = bad or bool(g["bin_mismatch"])
	if require_canaries and not cok:
		bad = True
	if any(abs(v) > 1e-6 for v in mdelta.values()):
		bad = True
	if bad:
		(OUT / f"ABORT_{tag}.json").write_text(json.dumps(payload, indent=2, default=str))
		raise RuntimeError(f"safety abort at {tag}: {payload}")
	return payload


def _apply_posting_order():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.api import (
		repair_posting_order_selected,
	)

	po = run_full_history_scan(company=COMPANY)
	rows = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		ps = str(r.get("planner_status") or "")
		if ps.startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			rows.append(r)
	if not rows:
		return {"path": "po", "applied_n": 0, "scanned": len(po.get("rows") or [])}
	# PO-only (no warehouse-wide RIV) — prior campaign proven path.
	applied = repair_posting_order_selected(rows=rows, dry_run=False)
	frappe.db.commit()
	return {
		"path": "po",
		"ready": len(rows),
		"applied_n": len((applied or {}).get("applied") or []),
		"aborted": (applied or {}).get("aborted"),
		"blocked_n": len((applied or {}).get("blocked") or []),
	}


def _apply_leftover_ma():
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		classify_leftover_ma_identity,
	)
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	ITEM = "16100066"
	WH = "انبار ملزومات مصرفی اسپاد"
	c = classify_leftover_ma_identity(ITEM, WH)
	if not c.get("eligible"):
		return {"skipped": True, "class": c.get("leftover_ma_status")}
	row = {**c, "topic": "LEFTOVER_MA", "item": ITEM, "warehouse": WH}
	live = execute(plan(row), dry_run=False)
	frappe.db.commit()
	return {"ok": live.get("ok"), "primary_state": live.get("primary_state"), "expected": c.get("expected_ma")}


def _drain_wrong_exact(limit_vouchers=200):
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	done = []
	while len(done) < limit_vouchers:
		scan = scan_wrong_rates(company=COMPANY, limit=5000)
		ready = [
			r
			for r in (scan.get("rows") or [])
			if r.get("eligible") and "READY" in str(r.get("planner_status") or "")
		]
		by_v = {}
		for r in ready:
			by_v.setdefault(r.get("voucher"), r)
		todo = [v for v in by_v if v not in done]
		if not todo:
			break
		for v in todo[:25]:
			live = execute(plan(dict(by_v[v])), dry_run=False)
			frappe.db.commit()
			done.append(v)
			yield {
				"voucher": v,
				"ok": live.get("ok"),
				"state": live.get("primary_state"),
				"src": by_v[v].get("source_of_truth"),
				"expected": by_v[v].get("expected"),
			}


def run():
	OUT.mkdir(parents=True, exist_ok=True)
	from erpnext_extensions import __version__

	log = {"version": __version__, "steps": []}
	mfg0 = _gates()["mfg"]
	# Clean backup baseline: negatives / unmet canaries are expected until repaired.
	log["pass0_gates"] = _abort_if_unsafe(
		"pass0", mfg0, require_canaries=False, allow_baseline_neg=True
	)

	# fingerprint
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	scan0 = scan_all(company=COMPANY)
	(OUT / "pass0_scan.json").write_text(json.dumps(scan0, indent=2, default=str))
	log["pass0_integrity"] = (scan0.get("dashboard") or {}).get("Integrity Score")

	po = _apply_posting_order()
	log["steps"].append({"po": po})
	log["after_po"] = _abort_if_unsafe(
		"after_po",
		mfg0,
		require_canaries=False,
		allow_baseline_neg=False,
		allow_bin_mismatch=True,
	)

	lma = _apply_leftover_ma()
	log["steps"].append({"lma": lma})
	# LMA establishes 16100066; 28696/30470 may still need WR waves
	log["after_lma"] = _abort_if_unsafe(
		"after_lma",
		mfg0,
		require_canaries=False,
		allow_baseline_neg=False,
		allow_bin_mismatch=True,
	)

	wave = []
	for entry in _drain_wrong_exact():
		wave.append(entry)
		_abort_if_unsafe(
			f"wr_{len(wave)}",
			mfg0,
			require_canaries=False,
			allow_baseline_neg=False,
			allow_bin_mismatch=True,
		)
	log["steps"].append({"wrong_exact_applied": len(wave), "sample": wave[:20]})
	(OUT / "wrong_exact_wave.json").write_text(json.dumps(wave, indent=2, default=str))

	scan1 = scan_all(company=COMPANY)
	(OUT / "after_repairs_scan.json").write_text(json.dumps(scan1, indent=2, default=str))
	log["after_repairs_integrity"] = (scan1.get("dashboard") or {}).get("Integrity Score")
	log["after_repairs_dashboard"] = {
		k: (scan1.get("dashboard") or {}).get(k)
		for k in (
			"Integrity Score",
			"Wrong Rate",
			"Zero Rate",
			"Patient Zero",
			"Failed RIV Actionable",
			"Broken Bin",
			"Broken GL",
			"Ready to Repair",
			"Proven Legitimate Zero",
			"I1 Negative Rate",
		)
	}
	log["final_gates"] = _abort_if_unsafe(
		"final", mfg0, require_canaries=True, allow_baseline_neg=False, allow_bin_mismatch=False
	)
	log["final_canaries"] = _canaries()
	(OUT / "replay_log.json").write_text(json.dumps(log, indent=2, default=str))
	return log
