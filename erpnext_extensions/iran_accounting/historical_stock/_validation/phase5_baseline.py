# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5 baseline — KPI consistency (esp. I4 63 vs 65) + MANUAL/WAITING inventory.

Read-mostly: may write a FRESH metrics snapshot only.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from time import perf_counter

COMPANY = "اسپاد فارمد دارو"


def run(*, save_snapshot: int = 1, wrong_limit: int = 5000):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		save_metrics_snapshot,
		worker_queue_status,
		load_metrics_snapshot,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.scan import _i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import (
		count_wrong_rate_buckets,
		wrong_rate_bucket,
	)
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	out = {
		"phase": "PHASE_5_BASELINE",
		"company": COMPANY,
		"version": "5.3.0",
		"worker": worker_queue_status("long"),
		"active_riv": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabRepost Item Valuation`
			WHERE docstatus=1 AND status IN ('Queued','In Progress')
			"""
		)[0][0],
		"neg": {
			"valuation": frappe.db.sql(
				"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
			)[0][0],
			"incoming": frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabStock Ledger Entry`
				WHERE is_cancelled=0 AND actual_qty>0 AND incoming_rate < -0.0001
				"""
			)[0][0],
			"fg": frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
				JOIN `tabStock Entry` se ON se.name=sle.voucher_no
				WHERE sle.is_cancelled=0 AND se.purpose='Manufacture' AND sle.actual_qty>0
				  AND (sle.valuation_rate < -0.0001 OR sle.incoming_rate < -0.0001)
				"""
			)[0][0],
		},
	}

	# ---- I4 discrepancy ----
	sql_raw_sles = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0
		  AND ABS(qty_after_transaction) < 0.0001
		  AND ABS(IFNULL(stock_value,0)) > 1
		"""
	)[0][0]
	sql_raw_voucher = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT item_code, warehouse, voucher_no
			FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0
			  AND ABS(qty_after_transaction) < 0.0001
			  AND ABS(IFNULL(stock_value,0)) > 1
		) t
		"""
	)[0][0]
	sql_raw_identity = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT item_code, warehouse
			FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0
			  AND ABS(qty_after_transaction) < 0.0001
			  AND ABS(IFNULL(stock_value,0)) > 1
		) t
		"""
	)[0][0]
	sql_company_voucher = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT sle.item_code, sle.warehouse, sle.voucher_no
			FROM `tabStock Ledger Entry` sle
			JOIN `tabStock Entry` se ON se.name=sle.voucher_no AND sle.voucher_type='Stock Entry'
			WHERE sle.is_cancelled=0 AND se.company=%s
			  AND ABS(sle.qty_after_transaction) < 0.0001
			  AND ABS(IFNULL(sle.stock_value,0)) > 1
		) t
		""",
		COMPANY,
	)[0][0]
	sql_company_identity = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT sle.item_code, sle.warehouse
			FROM `tabStock Ledger Entry` sle
			JOIN `tabStock Entry` se ON se.name=sle.voucher_no AND sle.voucher_type='Stock Entry'
			WHERE sle.is_cancelled=0 AND se.company=%s
			  AND ABS(sle.qty_after_transaction) < 0.0001
			  AND ABS(IFNULL(sle.stock_value,0)) > 1
		) t
		""",
		COMPANY,
	)[0][0]
	# Also include non-SE voucher types (Purchase Receipt etc.)
	sql_all_company_voucher = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT sle.item_code, sle.warehouse, sle.voucher_no
			FROM `tabStock Ledger Entry` sle
			WHERE sle.is_cancelled=0 AND sle.company=%s
			  AND ABS(sle.qty_after_transaction) < 0.0001
			  AND ABS(IFNULL(sle.stock_value,0)) > 1
		) t
		""",
		COMPANY,
	)[0][0]
	sql_all_company_identity = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT sle.item_code, sle.warehouse
			FROM `tabStock Ledger Entry` sle
			WHERE sle.is_cancelled=0 AND sle.company=%s
			  AND ABS(sle.qty_after_transaction) < 0.0001
			  AND ABS(IFNULL(sle.stock_value,0)) > 1
		) t
		""",
		COMPANY,
	)[0][0]

	t0 = perf_counter()
	i4 = scan_i4_leftover(company=COMPANY, limit=5000)
	i4_s = round(perf_counter() - t0, 3)
	i4_rows = i4.get("rows") or []
	i4_pz = set()
	i4_identities = set()
	for r in i4_rows:
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse")
		if item and wh:
			i4_identities.add((item, wh))
		pz = r.get("patient_zero")
		if isinstance(pz, dict) and pz.get("voucher_no"):
			i4_pz.add((item, wh, pz.get("voucher_no")))
		elif r.get("voucher"):
			# self-root
			i4_pz.add((item, wh, r.get("voucher")))

	snap = load_metrics_snapshot(COMPANY) or {}
	out["i4_discrepancy"] = {
		"phase4_report_used": "scan_i4_leftover.count (classified voucher rows, company-scoped)",
		"phase4_value": 63,
		"dashboard_snapshot_value": (snap.get("dashboard") or {}).get("I4 Leftover"),
		"snapshot_source": snap.get("source"),
		"snapshot_scanned_at": snap.get("scanned_at"),
		"live_scan_i4_count": i4.get("count"),
		"live_scan_seconds": i4_s,
		"live_by_status": i4.get("by_status"),
		"SQL_RAW_SLE_ROWS_SITEWIDE": sql_raw_sles,
		"SQL_DISTINCT_VOUCHER_SITEWIDE": sql_raw_voucher,
		"SQL_DISTINCT_IDENTITY_SITEWIDE": sql_raw_identity,
		"SQL_DISTINCT_VOUCHER_SE_COMPANY": sql_company_voucher,
		"SQL_DISTINCT_IDENTITY_SE_COMPANY": sql_company_identity,
		"SQL_DISTINCT_VOUCHER_ALL_VT_COMPANY": sql_all_company_voucher,
		"SQL_DISTINCT_IDENTITY_ALL_VT_COMPANY": sql_all_company_identity,
		"scan_fallback__i4_leftover": _i4_leftover(),
		"I4_RAW": sql_all_company_voucher,
		"I4_ACTIONABLE": int(i4.get("count") or 0),
		"I4_ROOT_IDENTITIES": len(i4_identities),
		"I4_PATIENT_ZERO_KEYS": len(i4_pz),
		"ready": sum(1 for r in i4_rows if r.get("eligible") or r.get("i4_status") == "READY_I4"),
		"explanation_candidates": [],
	}
	# Explain 63 vs 65
	expl = out["i4_discrepancy"]["explanation_candidates"]
	live = int(i4.get("count") or 0)
	sql_fb = int(_i4_leftover())
	if live != sql_fb:
		expl.append(
			f"scan_i4_leftover.count={live} vs scan._i4_leftover SQL fallback={sql_fb} "
			"(fallback counts SLE rows without company filter / without voucher dedup consistency)."
		)
	if sql_all_company_voucher != live:
		expl.append(
			f"SQL distinct voucher company={sql_all_company_voucher} vs classified scan={live} "
			"(scan may filter via planner / NOT_I4 / company join path)."
		)
	if sql_all_company_identity in (17, 65) or sql_all_company_voucher in (63, 65):
		expl.append(
			f"Identity count={sql_all_company_identity}, voucher count={sql_all_company_voucher} — "
			"dashboard may show voucher-level while phase notes cite identity roots (~17)."
		)

	# ---- Wrong Rate MANUAL inventory ----
	t0 = perf_counter()
	wrong = scan_wrong_rates(company=COMPANY, limit=wrong_limit)
	out["wrong_seconds"] = round(perf_counter() - t0, 3)
	wrows = [attach_plan(dict(r)) for r in (wrong.get("rows") or [])]
	wb = count_wrong_rate_buckets(wrows)
	manual = [r for r in wrows if wrong_rate_bucket(r) == "manual"]
	waiting = [r for r in wrows if wrong_rate_bucket(r) == "waiting"]
	ready = [r for r in wrows if wrong_rate_bucket(r) == "ready"]

	from erpnext_extensions.iran_accounting.historical_stock.manual_reason import (
		classify_wrong_manual_reason,
		summarize_manual_groups,
	)

	for r in manual:
		classify_wrong_manual_reason(r)

	manual_summary = summarize_manual_groups(manual)
	out["wrong"] = {
		"RAW": len(wrows),
		"ACTIONABLE": wb.get("active"),
		"READY": wb.get("ready"),
		"WAITING": wb.get("waiting"),
		"MANUAL": wb.get("manual"),
		"COMPLETE": wb.get("complete"),
		"buckets": wb,
		"by_flag": dict(wrong.get("by_flag") or {}),
		"MATCHED_BUT_CORRUPT": sum(
			1
			for r in wrows
			if r.get("flag") == "MATCHED_BUT_CORRUPT"
			or "MATCHED_BUT_CORRUPT" in (r.get("flags") or [])
		),
		"manual_by_reason": manual_summary.get("by_reason"),
		"WRONG_MANUAL_RAW": len(manual),
		"WRONG_MANUAL_ROOT_CHAINS": manual_summary.get("root_chains"),
		"WRONG_MANUAL_UNIQUE_ITEM_WAREHOUSE": manual_summary.get("unique_item_warehouse"),
		"WRONG_MANUAL_UNIQUE_PATIENT_ZERO": manual_summary.get("unique_patient_zero"),
		"manual_reason_samples": manual_summary.get("samples"),
	}

	# ---- Zero ----
	t0 = perf_counter()
	zero = scan_zero_rate_rows(company=COMPANY, limit=8000)
	out["zero_seconds"] = round(perf_counter() - t0, 3)
	zrows = zero.get("rows") or []
	out["zero"] = {
		"RAW": zero.get("raw_count"),
		"ACTIONABLE": zero.get("actionable_count"),
		"RECONSTRUCTABLE": zero.get("reconstructable_count"),
		"WAITING": zero.get("waiting_upstream_count"),
		"MR_USER_REVIEW": zero.get("material_receipt_user_review_count"),
		"by_status": dict(zero.get("by_status") or {}),
		"by_purpose": dict(zero.get("by_purpose") or {}),
		"by_kpi_bucket": dict(zero.get("by_kpi_bucket") or {}),
	}

	# ---- Posting Order ----
	t0 = perf_counter()
	po = run_full_history_scan(company=COMPANY)
	out["po_seconds"] = round(perf_counter() - t0, 3)
	porows = [attach_plan(dict(r)) for r in (po.get("rows") or [])]
	po_actionable = [
		r
		for r in porows
		if (r.get("optimizer_status") or r.get("status")) not in ("NO_REPAIR_NEEDED",)
	]
	out["posting_order"] = {
		"RAW": len(porows),
		"ACTIONABLE": len(po_actionable),
		"by_optimizer": dict(Counter(str(r.get("optimizer_status") or r.get("status")) for r in porows)),
		"by_planner": dict(Counter(str(r.get("planner_status") or "") for r in po_actionable)),
		"by_confidence": dict(Counter(str(r.get("confidence") or "") for r in po_actionable)),
	}

	# ---- Failed RIV / I1 / blockers ----
	riv = scan_failed_riv(limit=2000)
	i1 = scan_i1_negative_rate(company=COMPANY, limit=200)
	out["failed_riv"] = {
		"RAW": riv.get("raw_count") or riv.get("count"),
		"ACTIONABLE": riv.get("actionable_count"),
		"by_reconcile": riv.get("by_reconcile"),
	}
	out["i1"] = {"RAW": i1.get("count"), "ACTIONABLE": i1.get("count")}
	if frappe.db.exists("DocType", "Historical Repair Blocker"):
		out["blockers"] = dict(
			frappe.db.sql(
				"""
				SELECT lane, COUNT(*) FROM `tabHistorical Repair Blocker`
				WHERE status!='RESOLVED' GROUP BY lane
				"""
			)
		)
	else:
		out["blockers"] = {}

	# Patient zero from wrong+zero+i4
	patients = set()
	for r in list(wrows) + list(zrows) + list(i4_rows):
		pz = r.get("patient_zero")
		if isinstance(pz, dict) and pz.get("voucher_no"):
			patients.add(pz["voucher_no"])
		elif isinstance(pz, str) and pz:
			patients.add(pz)
	out["patient_zero"] = {
		"UNIQUE_VOUCHERS": len(patients),
		"note": "Deduplicated patient_zero voucher references across Wrong/Zero/I4 scans",
	}

	# Canonical KPI card contract
	out["kpi_contract"] = {
		"I4 Leftover": {
			"RAW": out["i4_discrepancy"]["I4_RAW"],
			"ACTIONABLE": out["i4_discrepancy"]["I4_ACTIONABLE"],
			"ROOT_IDENTITIES": out["i4_discrepancy"]["I4_ROOT_IDENTITIES"],
			"primary_card": "ACTIONABLE",
		},
		"Wrong Rate": {
			"RAW": out["wrong"]["RAW"],
			"ACTIONABLE": out["wrong"]["ACTIONABLE"],
			"READY": out["wrong"]["READY"],
			"WAITING": out["wrong"]["WAITING"],
			"MANUAL": out["wrong"]["MANUAL"],
			"primary_card": "ACTIONABLE",
		},
		"Zero Rate": {
			"RAW": out["zero"]["RAW"],
			"ACTIONABLE": out["zero"]["ACTIONABLE"],
			"WAITING": out["zero"]["WAITING"],
			"RECONSTRUCTABLE": out["zero"]["RECONSTRUCTABLE"],
			"primary_card": "ACTIONABLE",
		},
		"Posting Order": {
			"RAW": out["posting_order"]["RAW"],
			"ACTIONABLE": out["posting_order"]["ACTIONABLE"],
			"primary_card": "ACTIONABLE",
		},
		"Failed RIV": {
			"RAW": out["failed_riv"]["RAW"],
			"ACTIONABLE": out["failed_riv"]["ACTIONABLE"],
			"primary_card": "ACTIONABLE",
		},
		"Patient Zero": {
			"ROOT_IDENTITIES": out["patient_zero"]["UNIQUE_VOUCHERS"],
			"primary_card": "ROOT_IDENTITIES",
		},
	}

	gate = (
		out["active_riv"] == 0
		and out["neg"]["valuation"] == 0
		and out["neg"]["incoming"] == 0
		and out["neg"]["fg"] == 0
		and out["worker"].get("available")
		and out["i1"]["RAW"] == 0
	)
	out["gate_ok"] = gate
	out["verdict"] = "PHASE_5_BASELINE_READY" if gate else "PHASE_5_BASELINE_BLOCKED"

	# Resolve I4 explanation definitively
	disc = out["i4_discrepancy"]
	if disc["I4_ACTIONABLE"] == disc["dashboard_snapshot_value"]:
		disc["resolved"] = (
			f"Live classified I4_ACTIONABLE={disc['I4_ACTIONABLE']} matches phase4_final snapshot. "
			f"I4_RAW (distinct voucher, company)={disc['I4_RAW']}; "
			f"I4_ROOT_IDENTITIES={disc['I4_ROOT_IDENTITIES']}. "
			"A dashboard showing 65 was almost certainly SQL_RAW / unscoped / identity-mixed "
			f"(sitewide distinct voucher={disc['SQL_DISTINCT_VOUCHER_SITEWIDE']}, "
			f"fallback _i4_leftover={disc['scan_fallback__i4_leftover']})."
		)
	else:
		disc["resolved"] = (
			f"Live I4_ACTIONABLE={disc['I4_ACTIONABLE']} vs snapshot "
			f"{disc['dashboard_snapshot_value']} — rescan drift after Phase 4."
		)

	if int(save_snapshot) and gate:
		dash = {
			"Integrity Score Version": "5.3.0",
			"I4 Leftover": disc["I4_ACTIONABLE"],
			"I4 Raw": disc["I4_RAW"],
			"I4 Root Identities": disc["I4_ROOT_IDENTITIES"],
			"Wrong Rate": out["wrong"]["ACTIONABLE"],
			"Wrong Rate Raw": out["wrong"]["RAW"],
			"Wrong Rate READY": out["wrong"]["READY"],
			"Wrong Rate WAITING": out["wrong"]["WAITING"],
			"Wrong Rate MANUAL": out["wrong"]["MANUAL"],
			"Zero Rate": out["zero"]["ACTIONABLE"],
			"Zero Rate Raw": out["zero"]["RAW"],
			"Posting Order": out["posting_order"]["ACTIONABLE"],
			"Posting Order Raw": out["posting_order"]["RAW"],
			"Failed RIV": out["failed_riv"]["ACTIONABLE"],
			"Failed RIV Raw": out["failed_riv"]["RAW"],
			"Failed RIV Actionable": out["failed_riv"]["ACTIONABLE"],
			"I1 Negative Rate": out["i1"]["ACTIONABLE"],
			"Patient Zero": out["patient_zero"]["UNIQUE_VOUCHERS"],
			"User Action Required": out["blockers"].get("USER_ACTION_REQUIRED", 0),
			"Tool Limit": out["blockers"].get("TOOL_LIMIT", 0),
			"Matched But Corrupt": out["wrong"]["MATCHED_BUT_CORRUPT"],
		}
		snap2 = save_metrics_snapshot(
			company=COMPANY,
			dashboard=dash,
			timing={"wrong": out["wrong_seconds"], "zero": out["zero_seconds"], "po": out["po_seconds"], "i4": i4_s},
			source="phase5_baseline",
			extra={"kpi_contract": out["kpi_contract"], "i4_discrepancy": disc["resolved"]},
		)
		frappe.db.commit()
		out["snapshot"] = {"freshness": snap2.get("freshness"), "scanned_at": snap2.get("scanned_at")}

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
