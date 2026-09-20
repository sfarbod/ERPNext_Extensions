# Copyright (c) 2026, ERPNext Extensions contributors
"""Fast read-only v5.3.0 business-rule audit (avoids Failed-RIV×preflight N+1)."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from math import log10
from time import perf_counter

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	MATCHED_BUT_CORRUPT,
	QTY_EPS,
	RATE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse import (
	list_scrap_reject_waste_warehouses,
)


def _fp():
	out = {}
	for label, table in [
		("SLE", "tabStock Ledger Entry"),
		("GL", "tabGL Entry"),
		("BIN", "tabBin"),
		("SE", "tabStock Entry"),
		("SED", "tabStock Entry Detail"),
		("RIV", "tabRepost Item Valuation"),
	]:
		c, m = frappe.db.sql(f"SELECT COUNT(*), MAX(modified) FROM `{table}`")[0]
		out[label] = {"count": int(c or 0), "max_modified": str(m)}
	return out


def _dump(outdir, name, payload):
	path = os.path.join(outdir, name)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	return path


def _phases_from_counts(counts: dict) -> list:
	return [
		{"phase": 1, "name": "Negative-rate patient-zero roots", "candidate_count": counts["i1"], "risk": "HIGH", "repost_requirement": "min-scope after root cleared"},
		{"phase": 2, "name": "Posting-order / chronology roots", "candidate_count": counts["po_actionable"], "risk": "MEDIUM", "repost_requirement": "often none"},
		{"phase": 3, "name": "I4 / zero-qty-nonzero-value roots", "candidate_count": counts["i4"] + counts["zero_qty_nz"], "risk": "MEDIUM", "repost_requirement": "selective"},
		{"phase": 4, "name": "Wrong/Zero authoritative-rate reconstruction", "candidate_count": counts["zero_actionable"] + counts["wrong"], "risk": "HIGH", "repost_requirement": "controlled RIV after preflight", "notes": "scrap zeros excluded"},
		{"phase": 5, "name": "Manufacture deterministic reconstruction", "candidate_count": counts["neg_fg"], "risk": "CRITICAL", "repost_requirement": "yes after EXACT preview"},
		{"phase": 6, "name": "Controlled repost waves", "candidate_count": counts["riv_actionable"], "risk": "CRITICAL", "repost_requirement": "RIV preflight REQUIRED"},
		{"phase": 7, "name": "SLE_GL_DRIFT / GL reconciliation", "candidate_count": None, "risk": "HIGH", "repost_requirement": "no — selective GL"},
		{"phase": 8, "name": "Residual manual negative-stock cases", "candidate_count": counts["neg_requires_user"], "risk": "HIGH", "repost_requirement": "user confirms cause"},
	]


def run():
	t0 = perf_counter()
	fp_before = _fp()
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	outdir = frappe.get_site_path("private", "files", "historical_repair_v530_audit")
	os.makedirs(outdir, exist_ok=True)
	scrap_wh = list(list_scrap_reject_waste_warehouses(company))
	print("SCRAP_WH", len(scrap_wh), flush=True)

	sql = {
		"zero_incoming_all": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name=sed.parent
			JOIN `tabItem` i ON i.name=sed.item_code
			WHERE se.docstatus=1 AND i.is_stock_item=1 AND ABS(sed.qty)>%s
			  AND ABS(IFNULL(sed.basic_rate,0))<%s AND ABS(IFNULL(sed.valuation_rate,0))<%s
			  AND IFNULL(sed.allow_zero_valuation_rate,0)=0
			  AND IFNULL(sed.t_warehouse,'')!='' AND IFNULL(sed.s_warehouse,'')=''
			""",
			(QTY_EPS, RATE_EPS, RATE_EPS),
		)[0][0],
		"zero_incoming_scrap_wh": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name=sed.parent
			JOIN `tabItem` i ON i.name=sed.item_code
			WHERE se.docstatus=1 AND i.is_stock_item=1 AND ABS(sed.qty)>%s
			  AND ABS(IFNULL(sed.basic_rate,0))<%s AND ABS(IFNULL(sed.valuation_rate,0))<%s
			  AND IFNULL(sed.allow_zero_valuation_rate,0)=0
			  AND IFNULL(sed.t_warehouse,'')!='' AND IFNULL(sed.s_warehouse,'')=''
			  AND (
			    LOWER(sed.t_warehouse) LIKE '%%reject%%'
			    OR sed.t_warehouse LIKE '%%ضایعات%%'
			    OR LOWER(sed.t_warehouse) LIKE '%%scrap%%'
			    OR LOWER(sed.t_warehouse) LIKE '%%waste%%'
			  )
			""",
			(QTY_EPS, RATE_EPS, RATE_EPS),
		)[0][0],
		"neg_valuation_rate_sle": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate<0"
		)[0][0],
		"neg_incoming_rate_sle": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND incoming_rate<0 AND actual_qty>0"
		)[0][0],
		"neg_fg_se": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name=sed.parent
			WHERE se.docstatus=1 AND se.purpose='Manufacture'
			  AND IFNULL(sed.is_finished_item,0)=1 AND sed.basic_rate<0
			"""
		)[0][0],
		"zero_qty_nz_bin": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabBin` WHERE ABS(actual_qty)<%s AND ABS(stock_value)>1", (QTY_EPS,)
		)[0][0],
		"neg_bin_qty": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabBin` WHERE actual_qty<-%s", (QTY_EPS,)
		)[0][0],
		"hist_neg_sle_qty": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND qty_after_transaction<-%s",
			(QTY_EPS,),
		)[0][0],
		"failed_riv": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabRepost Item Valuation` WHERE docstatus<2 AND status='Failed'"
		)[0][0],
		"earliest_sle": str(
			frappe.db.sql(
				"SELECT MIN(posting_datetime) FROM `tabStock Ledger Entry` WHERE is_cancelled=0"
			)[0][0]
		),
		"neg_rate_root_chains": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM (
			  SELECT item_code, warehouse FROM `tabStock Ledger Entry`
			  WHERE is_cancelled=0 AND actual_qty>0 AND incoming_rate<0
			  GROUP BY item_code, warehouse
			) t
			"""
		)[0][0],
	}
	# Historical-only Failed RIV heuristic: identity has no current negative/exploded SLE rate
	sql["failed_riv_hist_only_est"] = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabRepost Item Valuation` riv
		WHERE riv.docstatus<2 AND riv.status='Failed'
		  AND riv.item_code IS NOT NULL AND riv.warehouse IS NOT NULL
		  AND NOT EXISTS (
		    SELECT 1 FROM `tabStock Ledger Entry` sle
		    WHERE sle.is_cancelled=0 AND sle.item_code=riv.item_code AND sle.warehouse=riv.warehouse
		      AND (
		        (sle.actual_qty>0 AND sle.incoming_rate<0)
		        OR sle.valuation_rate<0
		        OR ABS(sle.valuation_rate) >= 1e12
		        OR ABS(sle.incoming_rate) >= 1e12
		      )
		  )
		"""
	)[0][0]
	print("SQL_DONE", flush=True)

	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.negative_stock_report import (
		build_negative_stock_root_report,
	)

	zero = scan_zero_rate_rows(company=company, limit=8000)
	print("ZERO", zero.get("raw_count"), zero.get("actionable_count"), zero.get("legitimate_scrap_zero_count"), flush=True)

	wrong = scan_wrong_rates(company=company, limit=2000)
	print("WRONG", wrong.get("count"), flush=True)

	i1 = scan_i1_negative_rate(company=company, limit=2000)
	print("I1", i1.get("count"), flush=True)

	i4 = scan_i4_leftover(company=company, from_date="2026-03-21", limit=3000)
	print("I4", i4.get("count"), flush=True)

	neg = build_negative_stock_root_report(company=company, limit_chains=300)
	print("NEG", neg.get("chain_count"), neg.get("by_classification"), flush=True)

	# Posting order
	po_by = Counter()
	po_raw = po_actionable = 0
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

		po = run_full_history_scan(company=company)
		po_rows = po.get("rows") or []
		po_raw = len(po_rows)
		for r in po_rows:
			opt = str(r.get("optimizer_status") or r.get("status") or "")
			if opt in ("NO_REPAIR_NEEDED", "ALREADY_ORDERED", "HEALTHY"):
				continue
			po_actionable += 1
			conf = str(r.get("confidence") or r.get("repair_confidence") or opt or "")
			if "EXACT" in conf:
				po_by["EXACT"] += 1
			elif "RECONSTRUCT" in conf:
				po_by["RECONSTRUCTABLE"] += 1
			elif "LIKELY" in conf:
				po_by["LIKELY"] += 1
			elif "NO_REPAIR" in conf:
				po_by["NO_REPAIR_PATH"] += 1
			else:
				po_by["AMBIGUOUS"] += 1
		print("PO", po_raw, po_actionable, dict(po_by), flush=True)
	except Exception as exc:
		print("PO_ERR", str(exc)[:200], flush=True)

	# Manufacture previews for neg FG only
	neg_fg = frappe.db.sql(
		"""
		SELECT se.name, sed.item_code, sed.basic_rate
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name=sed.parent
		WHERE se.docstatus=1 AND se.purpose='Manufacture'
		  AND IFNULL(sed.is_finished_item,0)=1 AND sed.basic_rate<0
		LIMIT 30
		""",
		as_dict=True,
	)
	mfg = {"exact": 0, "reconstructable": 0, "manual": 0, "errors": 0, "samples": []}
	from erpnext_extensions.iran_accounting.historical_stock.manufacture import preview_manufacture_voucher

	for row in neg_fg[:15]:
		try:
			prev = preview_manufacture_voucher(row.name)
			conf = prev.get("confidence")
			st = prev.get("status")
			if conf == "EXACT":
				mfg["exact"] += 1
			elif st == "RECONSTRUCTABLE":
				mfg["reconstructable"] += 1
			else:
				mfg["manual"] += 1
			if len(mfg["samples"]) < 8:
				mfg["samples"].append(
					{
						"voucher": row.name,
						"fg": row.item_code,
						"current": flt(row.basic_rate),
						"confidence": conf,
						"status": st,
						"eligible": prev.get("eligible"),
						"after_rates_healthy": prev.get("after_rates_healthy"),
						"fg_negative": prev.get("fg_negative"),
					}
				)
		except Exception as exc:
			mfg["errors"] += 1
			mfg["samples"].append({"voucher": row.name, "error": str(exc)[:180]})
	print("MFG", mfg["exact"], mfg["reconstructable"], mfg["manual"], flush=True)

	# RIV preflight sample (closure poison first; full preview only if clean)
	from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import (
		analyze_dependency_closure,
		preview_repost_impact,
	)

	preflight, safe, blocked = [], 0, 0
	targets = []
	for r in (i1.get("rows") or [])[:6]:
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse") or r.get("t_warehouse")
		if item and wh:
			targets.append((item, wh, "i1"))
	for item in ("13100134", "20100067"):
		for (wh,) in frappe.db.sql(
			"SELECT DISTINCT warehouse FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND item_code=%s LIMIT 2",
			(item,),
		):
			targets.append((item, wh, "incident"))
	seen = set()
	for item, wh, tag in targets:
		if (item, wh) in seen:
			continue
		seen.add((item, wh))
		try:
			closure = analyze_dependency_closure(item, wh, max_vouchers=60)
			poison = closure.get("poison_vouchers") or []
			eligible = not poison and not closure.get("manufacture_with_poison_components")
			reason = "SAFE_CLOSURE" if eligible else "POISON_IN_CLOSURE"
			gl_n = 0
			if eligible:
				full = preview_repost_impact(item, wh, company=company, max_vouchers=20)
				eligible = bool(full.get("eligible"))
				reason = full.get("reason")
				gl_n = len(full.get("gl_blockers") or [])
				poison = full.get("poison_blockers") or poison
			preflight.append(
				{
					"item": item,
					"warehouse": wh,
					"tag": tag,
					"eligible": eligible,
					"reason": reason,
					"poison": len(poison) if isinstance(poison, list) else 0,
					"gl_blockers": gl_n,
					"manufacture_vouchers": len(closure.get("manufacture_vouchers") or []),
				}
			)
			safe += int(eligible)
			blocked += int(not eligible)
		except Exception as exc:
			blocked += 1
			preflight.append({"item": item, "warehouse": wh, "tag": tag, "eligible": False, "error": str(exc)[:180]})
	print("PREFLIGHT", safe, blocked, flush=True)

	wr_matched = sum(
		1
		for r in (wrong.get("rows") or [])
		if r.get("matched_but_corrupt") or MATCHED_BUT_CORRUPT in (r.get("flags") or [])
	)
	zero_n = int(zero.get("actionable_count") or 0)
	wrong_n = int(wrong.get("active_count") or wrong.get("count") or 0)
	i1_n = int(i1.get("count") or 0)
	i4_n = int(i4.get("count") or 0)
	riv_actionable = max(0, int(sql["failed_riv"]) - int(sql["failed_riv_hist_only_est"]))
	penalty = (
		po_actionable * 0.25
		+ zero_n * 0.5
		+ wrong_n
		+ i4_n * 0.75
		+ i1_n * 1.25
		+ riv_actionable * 1.5
		+ int(sql["neg_fg_se"]) * 1.25
	)
	integrity = max(0, min(100, round(100 - 18 * log10(1 + penalty))))

	counts = {
		"i1": i1_n,
		"po_actionable": po_actionable,
		"i4": i4_n,
		"zero_qty_nz": int(sql["zero_qty_nz_bin"]),
		"zero_actionable": zero_n,
		"wrong": wrong_n,
		"neg_fg": int(sql["neg_fg_se"]),
		"riv_actionable": riv_actionable,
		"neg_requires_user": int(neg.get("requires_user") or 0),
	}
	phases = _phases_from_counts(counts)

	kpi_table = {
		"Posting Order": {
			"RAW": po_raw,
			"TRUE_ACTIONABLE": po_actionable,
			"NO_ACTION_REQUIRED": max(0, po_raw - po_actionable),
			"EXACT": po_by["EXACT"],
			"RECONSTRUCTABLE": po_by["RECONSTRUCTABLE"],
			"LIKELY": po_by["LIKELY"],
			"AMBIGUOUS": po_by["AMBIGUOUS"],
			"NO_REPAIR_PATH": po_by["NO_REPAIR_PATH"],
		},
		"Zero Rate": {
			"RAW": zero.get("raw_count") or zero.get("count"),
			"TRUE_ACTIONABLE": zero.get("actionable_count"),
			"NO_ACTION_REQUIRED": zero.get("no_action_required_count"),
			"LEGITIMATE_SCRAP_ZERO_RATE": zero.get("legitimate_scrap_zero_count"),
			"TRUE_ZERO_CORRUPTION": zero.get("true_zero_corruption_count"),
			"SQL_incoming_all": sql["zero_incoming_all"],
			"SQL_incoming_scrap_wh": sql["zero_incoming_scrap_wh"],
			"by_zero_reason": zero.get("by_zero_reason"),
			"by_class": zero.get("by_class"),
		},
		"Wrong Rate": {
			"RAW": wrong.get("count"),
			"TRUE_ACTIONABLE": wrong.get("active_count") or wrong.get("count"),
			"MATCHED_BUT_CORRUPT": wr_matched,
			"by_flag": wrong.get("by_flag"),
			"by_kpi_bucket": wrong.get("by_kpi_bucket"),
		},
		"I1": {"RAW": i1_n, "TRUE_ACTIONABLE": i1_n, "eligible": sum(1 for r in (i1.get("rows") or []) if r.get("eligible")), "by_status": i1.get("by_status")},
		"I4": {"RAW": i4_n, "TRUE_ACTIONABLE": i4_n, "eligible": sum(1 for r in (i4.get("rows") or []) if r.get("eligible")), "by_status": i4.get("by_status")},
		"Manufacture_neg_FG": {"RAW": sql["neg_fg_se"], "EXACT": mfg["exact"], "RECONSTRUCTABLE": mfg["reconstructable"], "MANUAL": mfg["manual"], "errors": mfg["errors"]},
		"zero_qty_nonzero_value": {"RAW": sql["zero_qty_nz_bin"], "TRUE_ACTIONABLE": sql["zero_qty_nz_bin"]},
		"negative_valuation_rate": {"RAW": sql["neg_valuation_rate_sle"], "TRUE_ACTIONABLE": sql["neg_valuation_rate_sle"], "root_chains": sql["neg_rate_root_chains"]},
		"negative_incoming_rate": {"RAW": sql["neg_incoming_rate_sle"]},
		"negative_Finished_Good_rate": {"RAW": sql["neg_fg_se"]},
		"negative_Bin": {"RAW": sql["neg_bin_qty"]},
		"historical_negative_SLE": {"RAW": sql["hist_neg_sle_qty"]},
		"Failed RIV": {
			"RAW": sql["failed_riv"],
			"HISTORICAL_ONLY_EST": sql["failed_riv_hist_only_est"],
			"TRUE_ACTIONABLE": riv_actionable,
		},
		"Integrity Score (approx)": integrity,
		"Integrity Score Version": "5.3.0",
	}

	fp_after = _fp()
	summary = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"version": "5.3.0",
		"audit_mode": "fast_no_failed_riv_scan",
		"scrap_reject_waste_warehouses": scrap_wh,
		"fingerprint_before": fp_before,
		"fingerprint_after": fp_after,
		"fingerprint_unchanged": fp_before == fp_after,
		"writes_occurred": fp_before != fp_after,
		"kpi_table": kpi_table,
		"sql_kpi": sql,
		"negative_stock_summary": {
			"chain_count": neg.get("chain_count"),
			"by_classification": neg.get("by_classification"),
			"auto_repairable": neg.get("auto_repairable"),
			"requires_user": neg.get("requires_user"),
			"bin_stale": neg.get("bin_stale"),
			"posting_order_repairable": neg.get("posting_order_repairable"),
			"repost_healable": neg.get("repost_healable"),
		},
		"manufacture_preview": mfg,
		"riv_preflight": {"probed": len(preflight), "safe": safe, "blocked": blocked, "samples": preflight},
		"repost_feasibility": {
			"earliest_reliable_posting_datetime": sql["earliest_sle"],
			"neg_rate_root_chains": sql["neg_rate_root_chains"],
			"safe_immediate_repost_chains": safe,
			"require_root_repair_first": blocked,
			"cannot_repost_safely_now": blocked,
		},
		"master_plan_phases": phases,
		"master_plan_safety": {
			"no_global_riv": True,
			"riv_preflight_required": True,
			"legitimate_scrap_zero_no_action": True,
			"matched_but_corrupt_detected": True,
			"false_rate_rebuild_complete_refused": True,
		},
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"production_access": False,
		"read_only": True,
		"code_limits": [
			"Failed RIV full classify×preflight is O(n) expensive — audit uses SQL HISTORICAL_ONLY estimate",
			"Full SLE_GL_DRIFT / Broken GL live scan deferred (expected-GL cost)",
			"Master Plan phases synthesized from bounded scanners + SQL (not build_master_repair_plan live)",
			"PO EXACT/RECONSTRUCTABLE depends on optimizer confidence labels present in scan rows",
		],
	}
	_dump(outdir, "SUMMARY.json", summary)
	_dump(outdir, "negative_stock_roots.json", neg)
	_dump(outdir, "riv_preflight.json", preflight)
	_dump(outdir, "zero_rate_meta.json", {k: zero.get(k) for k in zero if k != "rows"})
	_dump(outdir, "wrong_rate_meta.json", {k: wrong.get(k) for k in wrong if k != "rows"})
	frappe.db.rollback()
	print(json.dumps({"ok": True, "fingerprint_unchanged": summary["fingerprint_unchanged"], "elapsed": summary["elapsed_seconds"], "integrity": integrity}, default=str), flush=True)
	return summary
