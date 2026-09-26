# Copyright (c) 2026, ERPNext Extensions contributors
"""Development correction campaign on backup 20260926_155411.

Authoritative DB: 20260926_155411-erp_espadpharmed_com-database.sql.gz
Frozen Production manifest remains HR-PROD-20260926-SEGMENTED-v1 (reference only).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
	COMPANY,
	_gates,
	canaries,
	dash_slice,
	mfg_delta,
	mfg_fingerprint,
	rebuild_bins,
)
from erpnext_extensions.iran_accounting.historical_stock.production_execution import (
	DEFAULT_MANIFEST_ID,
	load_manifest,
	run_locked_rehearsal,
)

BACKUP_ID = "20260926_155411"
BACKUP_PATH = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/backups/"
	"20260926_155411-erp_espadpharmed_com-database.sql.gz"
)
OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_155411"
)


def _jdump(name: str, obj) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def backup_identity() -> dict:
	st = BACKUP_PATH.stat()
	sha = hashlib.sha256(BACKUP_PATH.read_bytes()).hexdigest()
	counts = {
		"sle": frappe.db.count("Stock Ledger Entry"),
		"se": frappe.db.count("Stock Entry"),
		"riv": frappe.db.count("Repost Item Valuation"),
		"bin": frappe.db.count("Bin"),
	}
	latest = frappe.db.sql(
		"""SELECT MAX(posting_datetime), MAX(creation) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0"""
	)[0]
	ge_12 = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE creation >= '2026-09-26 12:00:00'"""
	)[0][0]
	probes = {
		vn: bool(frappe.db.exists("Stock Entry", vn))
		for vn in (
			"MAT-STE-2026-30470",
			"MAT-STE-2026-28696",
			"MAT-STE-2026-33937",
			"MAT-STE-2026-36853",
			"MAT-STE-2026-36933",
			"MAT-STE-2026-37090",
		)
	}
	out = {
		"backup_id": BACKUP_ID,
		"path": str(BACKUP_PATH),
		"size_bytes": st.st_size,
		"sha256": sha,
		"counts": counts,
		"latest_sle_posting": str(latest[0]),
		"latest_sle_creation": str(latest[1]),
		"sle_created_on_or_after_20260926_12": int(ge_12),
		"probes": probes,
		"looks_like_155411": int(ge_12) > 0
		and str(latest[1] or "").startswith("2026-09-26 15")
		and all(probes.values()),
		"captured_at": str(now_datetime()),
	}
	_jdump("backup_identity.json", out)
	return out


def _extra_kpis() -> dict:
	qty0_nz = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND ABS(actual_qty)<1e-9 AND ABS(stock_value)>1e-6"""
	)[0][0]
	qty_pos_rate0 = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND actual_qty>1e-9 AND stock_value>1e-6
		  AND ABS(IFNULL(valuation_rate,0))<1e-9"""
	)[0][0]
	neg_stock = frappe.db.sql(
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
		"qty0_nonzero_stock_value": int(qty0_nz),
		"qty_pos_stock_value_pos_rate0": int(qty_pos_rate0),
		"neg_stock_qty_after": int(neg_stock),
		"neg_rate": int(neg_rate),
	}


def _identity_compression(scan: dict) -> dict:
	"""Raw findings vs unique vouchers / causal roots from Scan All payloads."""
	wr = scan.get("wrong_rate") or {}
	zr = scan.get("zero_rate") or {}
	i4 = scan.get("i4") or {}
	i1 = scan.get("i1") or {}
	dash = scan.get("dashboard") or {}
	pz = scan.get("patient_zero_vouchers") or {}
	return {
		"raw_findings": {
			"wrong_rate_raw": wr.get("raw_count") or dash.get("Wrong Rate"),
			"wrong_rate_active": wr.get("count") or dash.get("Wrong Rate"),
			"zero_rate_raw": zr.get("raw_count") or dash.get("Zero Rate Raw"),
			"zero_rate_actionable": zr.get("actionable_count") or dash.get("Zero Rate"),
			"i4_raw": (i4.get("count") if isinstance(i4, dict) else None) or dash.get("I4 Leftover"),
			"i1": (i1.get("count") if isinstance(i1, dict) else None) or dash.get("I1 Negative Rate"),
			"patient_zero_findings": dash.get("Patient Zero Findings"),
			"failed_riv_raw": dash.get("Failed RIV Raw"),
			"posting_order_raw": (scan.get("posting_order") or {}).get("raw_row_count"),
		},
		"unique_vouchers": {
			"patient_zero": scan.get("patient_zero_count"),
			"wrong_rate_waiting_roots": dash.get("Wrong Rate Waiting Roots"),
			"wrong_rate_manual_roots": dash.get("Wrong Rate Manual Roots"),
			"zero_rate_waiting_roots": dash.get("Zero Rate Waiting Roots"),
			"i4_root_identities": dash.get("I4 Root Identities"),
		},
		"unique_causal_roots": {
			"patient_zero_vouchers": list(pz)[:80] if isinstance(pz, dict) else pz,
			"patient_zero_by_topic": scan.get("patient_zero_by_topic"),
		},
		"dependency_edges": {
			"wrong_rate_waiting": dash.get("Wrong Rate WAITING"),
			"wrong_rate_ready": dash.get("Wrong Rate READY"),
			"i4_waiting": dash.get("WAITING_I4"),
			"i1_waiting": dash.get("WAITING_I1"),
			"riv_waiting": dash.get("RIV WAITING"),
			"gl_waiting": dash.get("GL WAITING"),
			"blocked": dash.get("Blocked"),
		},
	}


def pass0() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all
	from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
		scan_manufacture_anomalies,
	)

	scan = scan_all(company=COMPANY)
	mfg_scan = scan_manufacture_anomalies(company=COMPANY, limit=5000)
	out = {
		"backup_id": BACKUP_ID,
		"gates": _gates(),
		"canaries": canaries(),
		"mfg": mfg_fingerprint(),
		"dashboard": dash_slice(scan),
		"dashboard_full": scan.get("dashboard"),
		"patient_zero_count": scan.get("patient_zero_count"),
		"patient_zero_by_topic": scan.get("patient_zero_by_topic"),
		"identities": _identity_compression(scan),
		"extra_kpis": _extra_kpis(),
		"manufacture_scan": {
			"count": mfg_scan.get("count"),
			"scanned": mfg_scan.get("scanned"),
			"by_status": mfg_scan.get("by_status") or mfg_scan.get("by_class"),
			"sample": (mfg_scan.get("rows") or [])[:15],
		},
		"scan_timing": scan.get("timing"),
		"scan_duration_s": scan.get("duration_s"),
		"topics": {
			"wrong_rate": scan.get("wrong_rate"),
			"zero_rate": scan.get("zero_rate"),
			"i4": scan.get("i4"),
			"i1": scan.get("i1"),
			"gl": scan.get("gl"),
			"sle_bin": scan.get("sle_bin"),
			"failed_riv": scan.get("failed_riv"),
			"leftover_ma": scan.get("leftover_ma"),
			"posting_order": scan.get("posting_order"),
		},
		"captured_at": str(now_datetime()),
	}
	_jdump("pass0.json", out)
	return {
		"backup_id": BACKUP_ID,
		"dashboard": out["dashboard"],
		"identities": out["identities"],
		"extra_kpis": out["extra_kpis"],
		"manufacture_scan": {
			k: out["manufacture_scan"].get(k) for k in ("count", "scanned", "by_status")
		},
		"gates": out["gates"],
		"duration_s": out["scan_duration_s"],
	}


def revalidate_prior_canaries() -> dict:
	"""Live re-derivation of prior canaries on 155411. No hard-coded authority."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		classify_leftover_ma_identity,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		is_precision_dust_inbound,
		scan_i4_leftover,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	out = {"backup_id": BACKUP_ID, "canaries": canaries()}

	# 30470 residual: GL already in canaries(); add SLE SVD
	sle_30470 = frappe.db.sql(
		"""SELECT item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference
		FROM `tabStock Ledger Entry` WHERE voucher_no=%s AND is_cancelled=0""",
		"MAT-STE-2026-30470",
		as_dict=True,
	)
	out["30470_sle"] = {
		"rows": sle_30470,
		"svd_sum": round(sum(flt(r.stock_value_difference) for r in sle_30470), 4),
	}

	# 28696 full SLE (canaries() rates empty if SVD≈0)
	sle_28696 = frappe.db.sql(
		"""SELECT item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference, voucher_detail_no
		FROM `tabStock Ledger Entry` WHERE voucher_no=%s AND is_cancelled=0""",
		"MAT-STE-2026-28696",
		as_dict=True,
	)
	det_28696 = frappe.db.sql(
		"""SELECT item_code, qty, basic_rate, valuation_rate, amount, basic_amount,
		          s_warehouse, t_warehouse
		FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx""",
		"MAT-STE-2026-28696",
		as_dict=True,
	)
	out["28696_live"] = {
		"purpose": frappe.db.get_value("Stock Entry", "MAT-STE-2026-28696", "purpose"),
		"details": det_28696,
		"sles": sle_28696,
		"svd_sum": round(sum(flt(r.stock_value_difference) for r in sle_28696), 4),
	}

	# 16100066 leftover MA
	out["16100066_classify"] = classify_leftover_ma_identity(
		"16100066", "انبار ملزومات مصرفی اسپاد"
	)

	# 16100226 leftover MA
	out["16100226_classify"] = classify_leftover_ma_identity(
		"16100226", "انبار ملزومات مصرفی اسپاد"
	)
	hist_226 = frappe.db.sql(
		"""SELECT voucher_no, posting_datetime, actual_qty, qty_after_transaction,
		          stock_value, valuation_rate, incoming_rate, outgoing_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		("16100226", "انبار ملزومات مصرفی اسپاد"),
		as_dict=True,
	)
	out["16100226_history_n"] = len(hist_226)
	out["16100226_last5"] = hist_226[-5:]

	# 36853 I4
	i4_scan = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date="2026-09-26", limit=5000)
	i4_36853 = [
		r
		for r in (i4_scan.get("rows") or [])
		if (r.get("voucher") or "") in ("MAT-STE-2026-36853",) or (r.get("item") == "30100022")
	]
	out["36853"] = {
		"i4_rows": i4_36853,
		"reconstruct": reconstruct_manufacture_valuation("MAT-STE-2026-36853")
		if frappe.db.exists("Stock Entry", "MAT-STE-2026-36853")
		else None,
	}
	# I4 dust cases
	dust = []
	for r in i4_scan.get("rows") or []:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_no": r.get("voucher"), "item_code": r.get("item"), "warehouse": r.get("warehouse"), "is_cancelled": 0},
			[
				"name",
				"actual_qty",
				"qty_after_transaction",
				"stock_value",
				"incoming_rate",
				"valuation_rate",
			],
			as_dict=True,
		)
		if not sle:
			continue
		dust.append(
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"warehouse": r.get("warehouse"),
				"i4_status": r.get("i4_status") or r.get("status"),
				"qty_after": sle.qty_after_transaction,
				"actual_qty": sle.actual_qty,
				"stock_value": sle.stock_value,
				"incoming_rate": sle.incoming_rate,
				"qty_x_rate": flt(sle.actual_qty) * flt(sle.incoming_rate),
				"is_dust": is_precision_dust_inbound(sle),
			}
		)
	out["i4_all"] = {
		"count": i4_scan.get("count"),
		"by_status": i4_scan.get("by_status"),
		"dust_math": dust,
	}

	# 36933 / 37090
	out["36933"] = {
		"native": {
			k: reconstruct_manufacture_valuation("MAT-STE-2026-36933").get(k)
			for k in (
				"classification",
				"consumed_value",
				"scrap_value",
				"expected_target_rate",
				"current_target_rate",
				"reason",
				"eligible",
			)
		},
		"scrap_fg": analyze_manufacture_scrap_fg("MAT-STE-2026-36933"),
	}
	out["37090"] = {
		"native": {
			k: reconstruct_manufacture_valuation("MAT-STE-2026-37090").get(k)
			for k in (
				"classification",
				"consumed_value",
				"scrap_value",
				"expected_target_rate",
				"current_target_rate",
				"reason",
				"eligible",
			)
		},
		"scrap_fg": analyze_manufacture_scrap_fg("MAT-STE-2026-37090"),
	}

	# 13200001 earliest SLE (read-only)
	sle_132 = frappe.db.sql(
		"""SELECT name, voucher_no, posting_datetime, actual_qty, incoming_rate,
		          outgoing_rate, valuation_rate, stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 5""",
		"13200001",
		as_dict=True,
	)
	out["13200001_earliest"] = sle_132
	_jdump("canaries_deep.json", out)
	return {
		"30470_621301": (out["canaries"].get("30470") or {}).get("uses_621301"),
		"30470_622515": (out["canaries"].get("30470") or {}).get("uses_622515_for_stock"),
		"28696_svd": out["28696_live"]["svd_sum"],
		"16100066": {
			k: out["16100066_classify"].get(k)
			for k in ("leftover_ma_status", "eligible", "expected_ma", "actual_qty", "stock_value")
		},
		"16100226": {
			k: out["16100226_classify"].get(k)
			for k in ("leftover_ma_status", "eligible", "expected_ma", "actual_qty", "stock_value")
		},
		"33937": out["canaries"].get("33937"),
		"36853_i4_n": len(i4_36853),
		"i4_by": out["i4_all"]["by_status"],
		"36933": out["36933"]["native"],
		"36933_scrap": {
			k: out["36933"]["scrap_fg"].get(k)
			for k in ("classification", "eligible", "exploded_count", "reason", "consumed_value")
		},
		"37090": out["37090"]["native"],
		"37090_scrap": {
			k: out["37090"]["scrap_fg"].get(k)
			for k in ("classification", "eligible", "exploded_count", "reason")
		},
	}


def preflight_baseline() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.production_execution import preflight

	pf = preflight()
	_jdump("preflight_155411.json", pf)
	return pf


def scan_negative_fg() -> dict:
	"""Find Manufacture vouchers whose FG basic_rate is negative (conservation break)."""
	rows = frappe.db.sql(
		"""
		SELECT se.name voucher, sed.item_code, sed.qty, sed.basic_rate, sed.basic_amount,
		       se.work_order, se.job_card, se.posting_date
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
		WHERE se.docstatus=1 AND se.purpose='Manufacture'
		  AND IFNULL(sed.is_finished_item,0)=1
		  AND sed.basic_rate < -0.5
		ORDER BY se.posting_date, se.name
		""",
		as_dict=True,
	)
	out = {"count": len(rows), "rows": rows}
	_jdump("negative_fg.json", out)
	return out


def apply_baseline() -> dict:
	"""Apply frozen 104-root Production allowlist as Development regression baseline."""
	mfg0 = mfg_fingerprint()
	g0 = _gates()
	result = run_locked_rehearsal(tag="BASELINE_155411")
	rebuild_bins()
	out = {
		"backup_id": BACKUP_ID,
		"manifest": DEFAULT_MANIFEST_ID,
		"gates0": g0,
		"gates1": _gates(),
		"canaries": canaries(),
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
		"rehearsal": {
			k: result.get(k)
			for k in (
				"applied_ok",
				"applied_n",
				"blocked",
				"ok",
				"manifest_id",
				"canaries_ok",
			)
			if isinstance(result, dict)
		}
		if isinstance(result, dict)
		else {"raw_keys": list(result)[:20] if isinstance(result, dict) else str(type(result))},
		"result_tail": {k: result.get(k) for k in list(result or {})[:15]}
		if isinstance(result, dict)
		else None,
	}
	_jdump("baseline_155411.json", out)
	return out


def forensics_manufacture(voucher: str) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	ev = reconstruct_manufacture_valuation(voucher)
	an = analyze_manufacture_scrap_fg(voucher)
	details = frappe.db.sql(
		"""SELECT name, idx, item_code, qty, basic_rate, valuation_rate, amount, basic_amount,
		          is_finished_item, is_scrap_item, secondary_item_type, valuation_type,
		          s_warehouse, t_warehouse, additional_cost, allow_zero_valuation_rate
		FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx""",
		voucher,
		as_dict=True,
	)
	sles = frappe.db.sql(
		"""SELECT name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference, stock_value, qty_after_transaction,
		          voucher_detail_no, batch_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0 ORDER BY creation""",
		voucher,
		as_dict=True,
	)
	gl = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0 GROUP BY account""",
		voucher,
		as_dict=True,
	)
	out = {
		"voucher": voucher,
		"se": frappe.db.get_value(
			"Stock Entry",
			voucher,
			["purpose", "work_order", "job_card", "posting_date", "posting_time", "total_additional_costs"],
			as_dict=True,
		),
		"details": details,
		"sles": sles,
		"gl": gl,
		"gl_diff": sum(flt(r.d) for r in gl) - sum(flt(r.c) for r in gl),
		"reconstruct": {
			k: ev.get(k)
			for k in (
				"classification",
				"eligible",
				"consumed_value",
				"scrap_value",
				"byproduct_value",
				"expected_target_rate",
				"current_target_rate",
				"reason",
				"poisoned_sources",
			)
		},
		"analyze": {
			k: an.get(k)
			for k in (
				"classification",
				"eligible",
				"exploded_count",
				"reason",
				"consumed_value",
				"documented_scrap_value",
				"corrected_scrap_value",
				"conservation",
				"scrap_plans",
				"fg_plan",
			)
		},
	}
	_jdump(f"forensics_{voucher.replace('/', '_')}.json", out)
	return {
		"classification": ev.get("classification"),
		"consumed": ev.get("consumed_value"),
		"scrap": ev.get("scrap_value"),
		"expected_fg": ev.get("expected_target_rate"),
		"current_fg": ev.get("current_target_rate"),
		"reason": ev.get("reason"),
		"analyze_class": an.get("classification"),
		"analyze_reason": an.get("reason"),
		"gl_diff": out["gl_diff"],
	}


def forensics_25274() -> dict:
	return forensics_manufacture("MAT-STE-2026-25274")


def forensics_36933() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	voucher = "MAT-STE-2026-36933"
	ev = reconstruct_manufacture_valuation(voucher)
	an = analyze_manufacture_scrap_fg(voucher)
	details = frappe.db.sql(
		"""SELECT name, item_code, qty, basic_rate, valuation_rate, amount, basic_amount,
		          is_finished_item, is_scrap_item, secondary_item_type, valuation_type,
		          s_warehouse, t_warehouse, additional_cost, allow_zero_valuation_rate
		FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx""",
		voucher,
		as_dict=True,
	)
	sles = frappe.db.sql(
		"""SELECT name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference, stock_value, qty_after_transaction,
		          voucher_detail_no, batch_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0 ORDER BY creation""",
		voucher,
		as_dict=True,
	)
	gl = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0 GROUP BY account""",
		voucher,
		as_dict=True,
	)
	out = {
		"voucher": voucher,
		"se": frappe.db.get_value(
			"Stock Entry",
			voucher,
			["purpose", "work_order", "job_card", "posting_date", "posting_time", "total_additional_costs"],
			as_dict=True,
		),
		"details": details,
		"sles": sles,
		"gl": gl,
		"gl_diff": sum(flt(r.d) for r in gl) - sum(flt(r.c) for r in gl),
		"reconstruct": {
			k: ev.get(k)
			for k in (
				"classification",
				"eligible",
				"consumed_value",
				"scrap_value",
				"byproduct_value",
				"expected_target_rate",
				"current_target_rate",
				"reason",
				"fg_rows",
				"poisoned_sources",
			)
		},
		"analyze": an,
	}
	_jdump("forensics_36933.json", out)
	return {
		"classification": ev.get("classification"),
		"consumed": ev.get("consumed_value"),
		"scrap": ev.get("scrap_value"),
		"expected_fg": ev.get("expected_target_rate"),
		"current_fg": ev.get("current_target_rate"),
		"analyze_class": an.get("classification"),
		"gl_diff": out["gl_diff"],
	}


def i4_leftover_math() -> dict:
	"""Prove leftover qty×rate vs residual for every I4 identity SLE."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		is_precision_dust_inbound,
		scan_i4_leftover,
	)

	scan = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date="2026-09-26", limit=5000)
	rows = []
	for r in scan.get("rows") or []:
		leftovers = frappe.db.sql(
			"""SELECT name, voucher_no, item_code, warehouse, actual_qty,
			          qty_after_transaction, stock_value, incoming_rate, valuation_rate,
			          outgoing_rate, posting_datetime
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND ABS(qty_after_transaction) < 0.0002 AND ABS(stock_value) > 1
			ORDER BY posting_datetime, creation""",
			(r.get("item"), r.get("warehouse")),
			as_dict=True,
		)
		for sle in leftovers:
			qty = flt(sle.actual_qty)
			rate = flt(sle.incoming_rate) or flt(sle.valuation_rate)
			implied = qty * rate
			residual = flt(sle.stock_value)
			rows.append(
				{
					"identity": f"{r.get('item')}|{r.get('warehouse')}",
					"scan_voucher": r.get("voucher"),
					"i4_status": r.get("i4_status") or r.get("status"),
					"sle": sle.name,
					"voucher": sle.voucher_no,
					"qty_after": sle.qty_after_transaction,
					"actual_qty": qty,
					"rate": rate,
					"qty_x_rate": implied,
					"stock_value": residual,
					"diff": residual - implied,
					"is_dust": is_precision_dust_inbound(sle),
					"envelope_ok": abs(residual - implied) <= 1 and 0 < qty <= 0.0001,
				}
			)
	out = {
		"scan_count": scan.get("count"),
		"by_status": scan.get("by_status"),
		"leftover_sles": rows,
		"dust_proven": [x for x in rows if x["is_dust"] or x["envelope_ok"]],
		"not_dust": [x for x in rows if not x["is_dust"] and not x["envelope_ok"]],
	}
	_jdump("i4_leftover_math.json", out)
	return {
		"scan_count": out["scan_count"],
		"by_status": out["by_status"],
		"dust_n": len(out["dust_proven"]),
		"not_dust_n": len(out["not_dust"]),
		"dust_proven": out["dust_proven"],
		"not_dust": [
			{k: x[k] for k in ("scan_voucher", "i4_status", "qty_after", "actual_qty", "stock_value", "qty_x_rate", "diff")}
			for x in out["not_dust"]
		],
	}


def post_fix_controls() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
		scan_exploded_scrap_fg,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)

	a33 = analyze_manufacture_scrap_fg("MAT-STE-2026-36933")
	a90 = analyze_manufacture_scrap_fg("MAT-STE-2026-37090")
	pop = scan_exploded_scrap_fg(company=COMPANY, limit=500)
	out = {
		"36933": {
			k: a33.get(k)
			for k in ("classification", "eligible", "exploded_count", "reason")
		},
		"37090": {
			k: a90.get(k)
			for k in ("classification", "eligible", "exploded_count", "reason")
		},
		"37090_native": reconstruct_manufacture_valuation("MAT-STE-2026-37090").get("classification"),
		"population": {
			"count": pop.get("count"),
			"by_classification": pop.get("by_classification"),
			"exact_n": len(pop.get("exact") or []),
			"exact": [
				{
					"voucher": r.get("voucher"),
					"reason": r.get("reason"),
					"consumed": r.get("consumed_value"),
					"doc_scrap": r.get("documented_scrap_value"),
					"corr_scrap": r.get("corrected_scrap_value"),
				}
				for r in (pop.get("exact") or [])[:40]
			],
		},
	}
	_jdump("post_fix_controls.json", out)
	return out


def forensics_37090() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	ev = reconstruct_manufacture_valuation("MAT-STE-2026-37090")
	an = analyze_manufacture_scrap_fg("MAT-STE-2026-37090")
	out = {
		"reconstruct": {
			k: ev.get(k)
			for k in (
				"classification",
				"eligible",
				"consumed_value",
				"scrap_value",
				"expected_target_rate",
				"current_target_rate",
				"reason",
			)
		},
		"analyze": an,
	}
	_jdump("forensics_37090.json", out)
	return out


def apply_36853_i4() -> dict:
	"""Re-prove the 2,006,334,850 leftover I4 class on 155411."""
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		repair_i4_selected,
	)

	item, wh = "30100022", "انبار پایکار خط تولید اسپاد فارمد"
	c = classify_i4_row(item, wh, voucher="MAT-STE-2026-36853")
	before = frappe.db.sql(
		"""SELECT voucher_no, actual_qty, qty_after_transaction, stock_value,
		          stock_value_difference, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901','MAT-STE-2026-36763')
		ORDER BY posting_datetime, creation""",
		(item, wh),
		as_dict=True,
	)
	gl_before = frappe.db.sql(
		"""SELECT voucher_no, account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry`
		WHERE voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901') AND is_cancelled=0
		GROUP BY voucher_no, account""",
		as_dict=True,
	)
	g0 = _gates()
	mfg0 = mfg_fingerprint()
	if not (c.get("eligible") or str(c.get("i4_status") or "") == "READY_I4"):
		out = {"ok": False, "reason": c.get("reason") or c.get("i4_status"), "classify": c, "before": before}
		_jdump("i4_36853.json", out)
		return out
	preview = repair_i4_selected([c], dry_run=True)
	live = repair_i4_selected([c], dry_run=False)
	rebuild_bins()
	g1 = _gates()
	after = frappe.db.sql(
		"""SELECT voucher_no, actual_qty, qty_after_transaction, stock_value,
		          stock_value_difference, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901','MAT-STE-2026-36763')
		ORDER BY posting_datetime, creation""",
		(item, wh),
		as_dict=True,
	)
	gl_after = frappe.db.sql(
		"""SELECT voucher_no, account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry`
		WHERE voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901') AND is_cancelled=0
		GROUP BY voucher_no, account""",
		as_dict=True,
	)
	adj = frappe.db.sql(
		"""SELECT voucher_no, account, debit, credit FROM `tabGL Entry`
		WHERE is_cancelled=0 AND account LIKE '621301%%'
		  AND voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901')""",
		as_dict=True,
	)
	residual_before = sum(flt(r.stock_value) for r in before if r.voucher_no == "MAT-STE-2026-36853")
	residual_after = sum(flt(r.stock_value) for r in after if r.voucher_no == "MAT-STE-2026-36853")
	out = {
		"ok": bool(live.get("ok") or live.get("applied")),
		"classify": {
			k: c.get(k)
			for k in ("i4_status", "eligible", "residual_value", "current_value", "reason")
		},
		"preview_ok": preview.get("ok") if isinstance(preview, dict) else True,
		"live": {k: live.get(k) for k in ("ok", "applied", "aborted", "sql_updates_executed", "count") if isinstance(live, dict)},
		"residual_before": residual_before,
		"residual_after": residual_after,
		"gl_before": gl_before,
		"gl_after": gl_after,
		"gl_621301": adj,
		"gates0": g0,
		"gates1": g1,
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
		"sle_before": before,
		"sle_after": after,
	}
	_jdump("i4_36853.json", out)
	return {
		"ok": out["ok"],
		"residual_before": residual_before,
		"residual_after": residual_after,
		"gl_621301_n": len(adj),
		"gates1": g1,
		"mfg_delta": out["mfg_delta"],
	}


def canary_repair_36933() -> dict:
	"""Dry-run → repair scrap+FG → RIV1 → RIV2 → second repair on reproduced 36933."""
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
		repair_and_riv_manufacture_scrap_fg,
		repair_manufacture_scrap_fg,
	)

	g0 = _gates()
	mfg0 = mfg_fingerprint()
	before = analyze_manufacture_scrap_fg("MAT-STE-2026-36933")
	ctrl0 = reconstruct_37090_class()
	dry = repair_manufacture_scrap_fg("MAT-STE-2026-36933", dry_run=True)
	live = repair_and_riv_manufacture_scrap_fg("MAT-STE-2026-36933", dry_run=False)
	mid = analyze_manufacture_scrap_fg("MAT-STE-2026-36933")
	# Second RIV of FG from voucher time
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	se = frappe.db.get_value(
		"Stock Entry", "MAT-STE-2026-36933", ["posting_date", "posting_time"], as_dict=True
	)
	fg = mid.get("fg_plan") or live.get("preview", {}).get("fg") or {}
	riv2 = None
	if fg.get("item") and fg.get("warehouse") and se:
		riv2 = create_and_run_narrow_riv(
			fg["item"], fg["warehouse"], posting_date=se.posting_date, posting_time=se.posting_time
		)
		frappe.db.commit()
	after_riv2 = analyze_manufacture_scrap_fg("MAT-STE-2026-36933")
	second = repair_manufacture_scrap_fg("MAT-STE-2026-36933", dry_run=False)
	g1 = _gates()
	out = {
		"before_class": before.get("classification"),
		"before_fg": (before.get("fg_plan") or {}).get("current_rate"),
		"before_scrap": before.get("documented_scrap_value"),
		"dry_ok": dry.get("ok"),
		"dry_writes": dry.get("economic_writes"),
		"live": {
			k: live.get(k)
			for k in ("ok", "written", "economic_writes", "riv_stable", "reason")
		},
		"post_riv1_class": mid.get("classification"),
		"post_riv1_fg": (mid.get("fg_plan") or mid.get("current_fg_rate")),
		"riv2": {k: (riv2 or {}).get(k) for k in ("ok", "riv_status", "reason")},
		"post_riv2_class": after_riv2.get("classification"),
		"post_riv2_eligible": after_riv2.get("eligible"),
		"second_repair": {
			k: second.get(k) for k in ("ok", "aborted", "reason", "written", "economic_writes")
		},
		"37090_before": ctrl0,
		"37090_after": reconstruct_37090_class(),
		"gates0": g0,
		"gates1": g1,
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
	}
	_jdump("canary_repair_36933.json", out)
	return out


def snapshot_36933() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	ev = reconstruct_manufacture_valuation("MAT-STE-2026-36933")
	an = analyze_manufacture_scrap_fg("MAT-STE-2026-36933")
	fg = frappe.db.get_value(
		"Stock Entry Detail",
		{"parent": "MAT-STE-2026-36933", "is_finished_item": 1},
		["basic_rate", "basic_amount", "amount", "qty"],
		as_dict=True,
	)
	scraps = frappe.db.sql(
		"""SELECT item_code, qty, basic_rate, basic_amount
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND IFNULL(secondary_item_type,'')='Scrap'""",
		"MAT-STE-2026-36933",
		as_dict=True,
	)
	return {
		"native": ev.get("classification"),
		"scrap_fg": an.get("classification"),
		"fg_rate": flt(fg.basic_rate) if fg else None,
		"scrap_rates": [(r.item_code, flt(r.basic_rate), flt(r.qty)) for r in scraps],
		"consumed": ev.get("consumed_value"),
		"scrap": ev.get("scrap_value"),
	}


def canary_riv_36933_healthy() -> dict:
	"""On 155411, 36933 is already HEALTHY. Prove native RIV does not explode it."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	se = frappe.db.get_value(
		"Stock Entry", "MAT-STE-2026-36933", ["posting_date", "posting_time"], as_dict=True
	)
	fg = frappe.db.get_value(
		"Stock Entry Detail",
		{"parent": "MAT-STE-2026-36933", "is_finished_item": 1},
		["item_code", "t_warehouse", "basic_rate"],
		as_dict=True,
	)
	g0 = _gates()
	mfg0 = mfg_fingerprint()
	before = snapshot_36933()
	ctrl0 = reconstruct_37090_class()
	riv1 = create_and_run_narrow_riv(
		fg.item_code, fg.t_warehouse, posting_date=se.posting_date, posting_time=se.posting_time
	)
	frappe.db.commit()
	mid = snapshot_36933()
	riv2 = create_and_run_narrow_riv(
		fg.item_code, fg.t_warehouse, posting_date=se.posting_date, posting_time=se.posting_time
	)
	frappe.db.commit()
	after = snapshot_36933()
	g1 = _gates()
	out = {
		"fg_item": fg.item_code,
		"fg_warehouse": fg.t_warehouse,
		"before": before,
		"after_riv1": mid,
		"after_riv2": after,
		"riv1": {k: riv1.get(k) for k in ("ok", "riv_name", "riv_status", "reason")},
		"riv2": {k: riv2.get(k) for k in ("ok", "riv_name", "riv_status", "reason")},
		"37090_before": ctrl0,
		"37090_after": reconstruct_37090_class(),
		"stable": before == after,
		"fg_still_healthy": after.get("native") == "HEALTHY" and after.get("scrap_fg") == "HEALTHY",
		"gates0": g0,
		"gates1": g1,
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
	}
	_jdump("canary_riv_36933_healthy.json", out)
	return {
		"riv1_ok": riv1.get("ok"),
		"riv1_status": riv1.get("riv_status"),
		"riv2_ok": riv2.get("ok"),
		"riv2_status": riv2.get("riv_status"),
		"stable": out["stable"],
		"fg_still_healthy": out["fg_still_healthy"],
		"fg_rate": after.get("fg_rate"),
		"37090": out["37090_after"],
		"gates1": g1,
	}


def riv_canary_13200001() -> dict:
	"""Controlled earliest-SLE RIV of 13200001. Watch 36933 + Iran GL bootstrap."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	before = snapshot_36933()
	sle = frappe.db.sql(
		"""SELECT name, warehouse, posting_date, posting_time, posting_datetime,
		          actual_qty, incoming_rate, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		"13200001",
		as_dict=True,
	)
	if not sle:
		return {"ok": False, "reason": "no SLE for 13200001"}
	s = sle[0]
	g0 = _gates()
	riv1 = create_and_run_narrow_riv(
		"13200001", s.warehouse, posting_date=s.posting_date, posting_time=s.posting_time
	)
	frappe.db.commit()
	mid = snapshot_36933()
	g1 = _gates()
	riv2 = create_and_run_narrow_riv(
		"13200001", s.warehouse, posting_date=s.posting_date, posting_time=s.posting_time
	)
	frappe.db.commit()
	after = snapshot_36933()
	g2 = _gates()
	out = {
		"earliest": s,
		"riv1": {k: riv1.get(k) for k in ("ok", "riv_name", "riv_status", "reason")},
		"riv2": {k: riv2.get(k) for k in ("ok", "riv_name", "riv_status", "reason")},
		"36933_before": before,
		"36933_after_riv1": mid,
		"36933_after_riv2": after,
		"36933_stable": before == after,
		"gates0": g0,
		"gates1": g1,
		"gates2": g2,
	}
	_jdump("riv_13200001.json", out)
	return {
		"riv1_ok": riv1.get("ok"),
		"riv1_status": riv1.get("riv_status"),
		"riv2_ok": riv2.get("ok"),
		"riv2_status": riv2.get("riv_status"),
		"36933_before": before.get("native"),
		"36933_after": after.get("native"),
		"36933_scrap_fg_after": after.get("scrap_fg"),
		"36933_stable": before == after,
		"i1": g2.get("i1"),
		"neg_rate": g2.get("neg_rate"),
	}


def _narrow_riv_item_wh(item, warehouse=None):
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	if not warehouse:
		row = frappe.db.sql(
			"""SELECT warehouse, posting_date, posting_time
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation LIMIT 1""",
			item,
			as_dict=True,
		)
		if not row:
			return {"ok": False, "item": item, "reason": "no SLE"}
		warehouse = row[0].warehouse
		posting_date, posting_time = row[0].posting_date, row[0].posting_time
	else:
		row = frappe.db.sql(
			"""SELECT posting_date, posting_time
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation LIMIT 1""",
			(item, warehouse),
			as_dict=True,
		)
		if not row:
			return {"ok": False, "item": item, "warehouse": warehouse, "reason": "no SLE"}
		posting_date, posting_time = row[0].posting_date, row[0].posting_time
	g0 = _gates()
	s33 = snapshot_36933()
	riv = create_and_run_narrow_riv(item, warehouse, posting_date=posting_date, posting_time=posting_time)
	frappe.db.commit()
	g1 = _gates()
	return {
		"item": item,
		"warehouse": warehouse,
		"riv_ok": riv.get("ok"),
		"riv_status": riv.get("riv_status"),
		"reason": riv.get("reason"),
		"i1": g1.get("i1"),
		"neg_rate": g1.get("neg_rate"),
		"neg_after": g1.get("neg_after"),
		"bin": g1.get("bin_mismatch"),
		"36933": snapshot_36933().get("native"),
		"37090": reconstruct_37090_class(),
		"gate_ok": g1.get("i1") == 0 and g1.get("neg_rate") == 0 and g1.get("neg_after") <= g0.get("neg_after"),
	}


def reconstruct_37090_class():
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	return analyze_manufacture_scrap_fg("MAT-STE-2026-37090").get("classification")


def failed_riv_reeval() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv

	scan = scan_failed_riv(limit=2000)
	by = scan.get("by_status") or {}
	by_rec = scan.get("by_reconcile") or {}
	out = {
		"raw": scan.get("count"),
		"actionable": scan.get("actionable_count"),
		"by_status": by,
		"by_reconcile": by_rec,
	}
	_jdump("failed_riv_reeval.json", out)
	return out


def post_scan() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	scan = scan_all(company=COMPANY)
	out = {
		"dashboard": dash_slice(scan),
		"gates": _gates(),
		"canaries": canaries(),
		"36933": snapshot_36933(),
		"37090": reconstruct_37090_class(),
		"extra": _extra_kpis(),
	}
	_jdump("post_scan.json", out)
	return {
		"dashboard": out["dashboard"],
		"gates": out["gates"],
		"36933": out["36933"].get("native"),
		"37090": out["37090"],
		"extra": out["extra"],
	}


def repost_ladder_l2_l4() -> dict:
	"""L2 13100023; L3 same-family trio; L4 cross-family pilots. No L6."""
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)

	g0 = _gates()
	l2 = _narrow_riv_item_wh("13100023")
	l3 = [
		_narrow_riv_item_wh("13100134"),
		_narrow_riv_item_wh("13200001"),  # already proven; idempotency
	]
	# L4 representative identities (earliest SLE of each item)
	l4_items = [
		("16100066", "انبار ملزومات مصرفی اسپاد"),  # leftover MA
		("16100226", "انبار ملزومات مصرفی اسپاد"),  # legitimate zero
		("20100067", None),  # 36933 FG
		("30100255", None),  # 37090 FG
		("30100022", "انبار پایکار خط تولید اسپاد فارمد"),  # 36853 I4
	]
	l4 = []
	for item, wh in l4_items:
		l4.append(_narrow_riv_item_wh(item, wh))
	g1 = _gates()
	out = {
		"l2": l2,
		"l3": l3,
		"l4": l4,
		"gates0": g0,
		"gates1": g1,
		"36933": reconstruct_manufacture_valuation("MAT-STE-2026-36933").get("classification"),
		"37090": reconstruct_37090_class(),
		"all_gate_ok": all(r.get("gate_ok") for r in [l2, *l3, *l4] if r),
	}
	_jdump("repost_ladder_l2_l4.json", out)
	return {
		"l2_ok": l2.get("riv_ok"),
		"l3_ok": [r.get("riv_ok") for r in l3],
		"l4_ok": [r.get("riv_ok") for r in l4],
		"all_gate_ok": out["all_gate_ok"],
		"36933": out["36933"],
		"37090": out["37090"],
		"gates1": g1,
	}


def wave_scrap_fg(n: int = 1) -> dict:
	"""Apply the next n EXACT exploded-scrap+FG roots. Re-analyze each at apply time."""
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
		repair_and_riv_manufacture_scrap_fg,
		scan_exploded_scrap_fg,
	)

	pop = scan_exploded_scrap_fg(company=COMPANY, limit=5000)
	exact = [r for r in (pop.get("exact") or []) if r.get("eligible")]
	exact.sort(key=lambda r: (str(r.get("posting_date") or ""), str(r.get("voucher") or "")))
	targets = exact[: int(n)]
	g0 = _gates()
	mfg0 = mfg_fingerprint()
	ctrl0 = reconstruct_37090_class()
	results = []
	for row in targets:
		voucher = row.get("voucher")
		an = analyze_manufacture_scrap_fg(voucher)
		if not an.get("eligible") or an.get("classification") != "EXACT":
			results.append(
				{
					"voucher": voucher,
					"skipped": True,
					"classification": an.get("classification"),
					"reason": an.get("reason"),
				}
			)
			continue
		live = repair_and_riv_manufacture_scrap_fg(voucher, dry_run=False)
		post = analyze_manufacture_scrap_fg(voucher)
		results.append(
			{
				"voucher": voucher,
				"ok": live.get("ok"),
				"written": live.get("written"),
				"economic_writes": live.get("economic_writes"),
				"riv_stable": live.get("riv_stable"),
				"riv_fg": live.get("riv_fg"),
				"post_class": post.get("classification"),
				"post_eligible": post.get("eligible"),
				"post_fg": (post.get("fg_plan") or {}).get("current_rate") or post.get("current_fg_rate"),
				"reason": live.get("reason"),
			}
		)
	g1 = _gates()
	out = {
		"requested": int(n),
		"population_exact": len(exact),
		"by_classification": pop.get("by_classification"),
		"applied": results,
		"37090_before": ctrl0,
		"37090_after": reconstruct_37090_class(),
		"gates0": g0,
		"gates1": g1,
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
		"gate_ok": (
			g1.get("i1") == 0
			and g1.get("neg_rate") == 0
			and g1.get("bin_mismatch") == 0
			and (g1.get("neg_after") or 0) <= (g0.get("neg_after") or 0)
			and reconstruct_37090_class() == ctrl0
		),
	}
	_jdump(f"wave_scrap_fg_{n}.json", out)
	return {
		"requested": out["requested"],
		"population_exact": out["population_exact"],
		"applied_n": sum(1 for r in results if r.get("ok") and r.get("written")),
		"results": results,
		"37090": out["37090_after"],
		"gate_ok": out["gate_ok"],
		"gates1": g1,
		"mfg_delta": out["mfg_delta"],
	}


def run_clean_replay(tag: str = "A") -> dict:
	"""Restore-time sequence: identity → Pass 0 → baseline → I4 36853 → healthy RIV canaries → L2.

	Stops before earliest-SLE 13100134 (L3). That fanout deadlocks and can leave
	dependent manufacture identities negative. Highest proven safe ladder is L2.
	"""
	ident = backup_identity()
	if not ident.get("looks_like_155411"):
		return {"ok": False, "tag": tag, "reason": "database is not 155411", "ident": ident}
	p0 = pass0()
	base = apply_baseline()
	i4 = apply_36853_i4()
	c33 = canary_riv_36933_healthy()
	c132 = riv_canary_13200001()
	l2 = _narrow_riv_item_wh("13100023")
	pop = scan_scrap_fg_population()
	scan = post_scan()
	out = {
		"tag": tag,
		"ok": bool(
			ident.get("looks_like_155411")
			and (base.get("rehearsal") or {}).get("applied_ok") == 104
			and i4.get("ok")
			and c33.get("riv1_ok")
			and c33.get("riv2_ok")
			and c33.get("fg_still_healthy")
			and c132.get("riv1_ok")
			and l2.get("riv_ok")
			and scan.get("36933") == "HEALTHY"
			and scan.get("37090") == "HEALTHY"
			and (scan.get("gates") or {}).get("i1") == 0
			and (scan.get("gates") or {}).get("neg_rate") == 0
		),
		"identity": {k: ident.get(k) for k in ("looks_like_155411", "sha256", "latest_sle_creation", "size_bytes")},
		"pass0": p0.get("dashboard"),
		"baseline_applied": (base.get("rehearsal") or {}).get("applied_ok"),
		"i4_36853": {k: i4.get(k) for k in ("ok", "residual_before", "residual_after", "gl_621301_n")},
		"36933_riv": c33,
		"13200001_riv": c132,
		"l2": {k: l2.get(k) for k in ("riv_ok", "riv_status", "gate_ok", "36933", "37090")},
		"population": pop,
		"final_scan": scan,
	}
	_jdump(f"clean_replay_{tag}.json", out)
	return out


def scan_scrap_fg_population() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		scan_exploded_scrap_fg,
	)

	pop = scan_exploded_scrap_fg(company=COMPANY, limit=5000)
	neg = scan_negative_fg()
	out = {
		"count": pop.get("count"),
		"by_classification": pop.get("by_classification"),
		"exact_n": len(pop.get("exact") or []),
		"exact": [
			{
				"voucher": r.get("voucher"),
				"reason": r.get("reason"),
				"consumed": r.get("consumed_value"),
				"doc_scrap": r.get("documented_scrap_value"),
				"corr_scrap": r.get("corrected_scrap_value"),
				"exploded": r.get("exploded_count"),
			}
			for r in (pop.get("exact") or [])
		],
		"waiting": [
			{"voucher": r.get("voucher"), "reason": r.get("reason")}
			for r in (pop.get("rows") or [])
			if r.get("classification") == "WAITING_UPSTREAM"
		],
		"manual": [
			{"voucher": r.get("voucher"), "reason": r.get("reason")}
			for r in (pop.get("rows") or [])
			if r.get("classification") == "MANUAL"
		],
		"negative_fg": neg,
	}
	_jdump("scrap_fg_population.json", out)
	return {
		"count": out["count"],
		"by_classification": out["by_classification"],
		"exact_n": out["exact_n"],
		"waiting_n": len(out["waiting"]),
		"manual_n": len(out["manual"]),
		"negative_fg_n": neg.get("count"),
	}
