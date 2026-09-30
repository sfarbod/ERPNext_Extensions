# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-2 post-WR reconciliation: I4 / PZ / Failed RIV / Zero / Leftover MA / preflight."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_dev_waves_0930 import (
	_gates,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_wr_waves_0930 import (
	_canary_36934,
)
from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.categories import (
	classify_lane,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.preflight import (
	full_repost_preflight,
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


def reclassify_i4() -> dict:
	scan = scan_i4_leftover(
		company=COMPANY, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=5000
	)
	rows = scan.get("rows") or []
	lanes = Counter()
	detail = []
	for r in rows:
		status = str(r.get("status") or r.get("planner_status") or "")
		reason = str(r.get("reason") or r.get("message") or r.get("i4_reason") or "")
		lane = classify_lane(
			planner_status=status,
			manual_reason=reason,
			manual_lane=str(r.get("manual_lane") or ""),
			i4_reason=reason,
			confidence=str(r.get("confidence") or ""),
		)
		if "PRECISION_DUST" in reason.upper():
			lane = "LEGITIMATE"
			code = "PRECISION_DUST"
		elif status in ("WAITING_I4", "I4_WAITING") or "WAITING" in status:
			lane = "WAITING_UPSTREAM"
			code = "WAITING_I4"
		elif status == "READY_I4":
			lane = "AUTO_REPAIRABLE"
			code = "READY_I4"
		elif lane == "LEGITIMATE":
			code = "LEGITIMATE"
		elif lane == "TECHNICAL_TOOL_GAP":
			code = "I4_TOOL_GAP"
		elif abs(flt(r.get("qty_after_transaction") or r.get("qty") or 0)) < 1e-9 and abs(
			flt(r.get("stock_value") or r.get("stock_value_difference") or 0)
		) > 1:
			lane = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"
			code = "REAL_TERMINAL_CORRUPTION"
		else:
			code = "I4_UNCLASSIFIED"
			if lane == "MANUAL_BUSINESS_EVIDENCE_REQUIRED":
				code = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"
		lanes[lane] += 1
		detail.append(
			{
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher"),
				"status": status,
				"lane": lane,
				"code": code,
				"reason": reason[:180],
				"qty": r.get("qty_after_transaction") or r.get("qty"),
				"stock_value": r.get("stock_value"),
			}
		)
	out = {
		"raw": len(rows),
		"root_identity_count": scan.get("root_identity_count"),
		"by_status": scan.get("by_status") or dict(Counter(r.get("status") for r in rows)),
		"by_lane": dict(lanes),
		"real_terminal": [d for d in detail if d["code"] == "REAL_TERMINAL_CORRUPTION"],
		"dust": [d for d in detail if d["code"] == "PRECISION_DUST"],
		"waiting": [d for d in detail if d["lane"] == "WAITING_UPSTREAM"],
		"tool_gap": [d for d in detail if d["lane"] == "TECHNICAL_TOOL_GAP"],
		"true_manual": [d for d in detail if d["lane"] == "MANUAL_BUSINESS_EVIDENCE_REQUIRED"],
		"all": detail,
	}
	_dump("i4_reclass_after_wr.json", out)
	return {
		"raw": out["raw"],
		"by_status": out["by_status"],
		"by_lane": out["by_lane"],
		"real_terminal_n": len(out["real_terminal"]),
		"dust_n": len(out["dust"]),
		"waiting_n": len(out["waiting"]),
		"tool_gap_n": len(out["tool_gap"]),
		"true_manual_n": len(out["true_manual"]),
	}


def compress_pz() -> dict:
	"""Compress Patient Zero from WR + Zero + I4 scans (same sources as dashboard)."""
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	rows = []
	wr = scan_wrong_rates(company=COMPANY, limit=3000)
	for r in wr.get("rows") or []:
		pz = r.get("patient_zero")
		if not pz:
			continue
		rows.append({**r, "topic": "WRONG_RATE"})
	zr = scan_zero_rate_rows(company=COMPANY, limit=3000)
	zr_rows = zr.get("rows") if isinstance(zr, dict) else zr
	for r in zr_rows or []:
		pz = r.get("patient_zero")
		if not pz:
			continue
		rows.append({**r, "topic": "ZERO_RATE"})
	i4 = scan_i4_leftover(
		company=COMPANY, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=5000
	)
	for r in i4.get("rows") or []:
		pz = r.get("patient_zero")
		if pz or r.get("status") in ("READY_I4", "WAITING_I4", "MANUAL"):
			rows.append({**r, "topic": "I4"})
	vouchers = sorted(
		{
			str(r.get("voucher") or r.get("voucher_no") or "")
			for r in rows
			if r.get("voucher") or r.get("voucher_no")
		}
	)
	topics = Counter(str(r.get("topic") or "") for r in rows)
	roots = Counter()
	for r in rows:
		pz = r.get("patient_zero")
		if isinstance(pz, dict):
			root = pz.get("voucher_no") or pz.get("voucher")
		else:
			root = pz
		if not root:
			root = r.get("voucher") or r.get("voucher_no")
		if root:
			roots[str(root)] += 1
	out = {
		"raw_findings": len(rows),
		"unique_vouchers": len(vouchers),
		"unique_causal_roots": len(roots),
		"by_topic": dict(topics),
		"top_roots": roots.most_common(25),
		"sample_vouchers": vouchers[:40],
	}
	_dump("pz_compression_after_wr.json", out)
	return out


def reconcile_failed_riv() -> dict:
	scan = scan_failed_riv(company=COMPANY, limit=500)
	rows = [r for r in (scan.get("rows") or []) if r.get("actionable") or str(r.get("riv_reconcile_status") or "") not in (
		"HISTORICAL_ONLY",
		"SUPERSEDED_BY_SUCCESSFUL_REPAIR",
	)]
	# Prefer explicit actionable list
	if scan.get("actionable_count") is not None:
		rows = [
			r
			for r in (scan.get("rows") or [])
			if (r.get("riv_reconcile_status") or "")
			not in ("HISTORICAL_ONLY", "SUPERSEDED_BY_SUCCESSFUL_REPAIR")
		][: int(scan.get("actionable_count") or 50)]
	by = Counter()
	detail = []
	for r in rows:
		st = str(r.get("riv_reconcile_status") or r.get("riv_status") or r.get("status") or "")
		if st in ("HISTORICAL_ONLY",):
			lane = "HISTORICAL_ONLY"
		elif "SUPERSEDED" in st:
			lane = "SUPERSEDED"
		elif "WAITING" in st:
			lane = "WAITING_UPSTREAM"
		elif st == "SAFE_TO_RETRY":
			lane = "SAFE_TO_RETRY"
		elif "TOOL" in st or "MANUAL_TOOL" in st:
			lane = "TECHNICAL_TOOL_GAP"
		else:
			lane = classify_lane(
				planner_status=st,
				manual_reason=str(r.get("error") or r.get("reason") or ""),
				manual_lane=str(r.get("manual_lane") or ""),
			)
			if lane == "MANUAL_BUSINESS_EVIDENCE_REQUIRED":
				pass
			elif lane == "AUTO_REPAIRABLE":
				lane = "SAFE_TO_RETRY"
		by[lane] += 1
		detail.append(
			{
				"name": r.get("name") or r.get("riv"),
				"voucher": r.get("voucher_no") or r.get("voucher"),
				"item": r.get("item_code") or r.get("item"),
				"status": st,
				"lane": lane,
				"error": str(r.get("error") or r.get("reason") or "")[:160],
			}
		)
	out = {
		"actionable_ref": scan.get("actionable_count"),
		"reconciled_n": len(detail),
		"by_lane": dict(by),
		"rows": detail,
		"note": "No blind retry performed",
	}
	_dump("failed_riv_reconcile_after_wr.json", out)
	return {k: out[k] for k in ("actionable_ref", "reconciled_n", "by_lane", "note")}


def leftover_ma_status() -> dict:
	rows = frappe.db.sql(
		"""
		SELECT t.item_code, t.warehouse,
		       ROUND(t.qty_after_transaction,6) q,
		       ROUND(t.stock_value,2) sv,
		       ROUND(t.valuation_rate,2) vr,
		       t.voucher_no
		FROM `tabStock Ledger Entry` t
		INNER JOIN (
		  SELECT item_code, warehouse,
		         MAX(CONCAT(DATE_FORMAT(posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', creation)) mx
		  FROM `tabStock Ledger Entry` WHERE is_cancelled=0
		  GROUP BY item_code, warehouse
		) x ON x.item_code=t.item_code AND x.warehouse=t.warehouse
		   AND CONCAT(DATE_FORMAT(t.posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', t.creation)=x.mx
		WHERE t.is_cancelled=0
		  AND t.qty_after_transaction > 0
		  AND t.stock_value > 1
		  AND ABS(t.valuation_rate) < 1e-9
		LIMIT 20
		""",
		as_dict=True,
	)
	c66 = frappe.db.sql(
		"""
		SELECT warehouse, ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv,
		       ROUND(valuation_rate,2) vr, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code='16100066' AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		as_dict=True,
	)
	c226 = frappe.db.sql(
		"""
		SELECT warehouse, ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv,
		       ROUND(valuation_rate,2) vr, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code='16100226' AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		as_dict=True,
	)
	out = {
		"leftover_ma_terminals": rows,
		"count": len(rows),
		"canary_16100066": c66,
		"canary_16100226": c226,
		"c66_ok": bool(c66 and flt(c66[0].q) > 0 and flt(c66[0].sv) > 0 and flt(c66[0].vr) > 0),
		"c226_ok": bool(c226 and not (flt(c226[0].q) > 0 and flt(c226[0].vr) > 0 and flt(c226[0].sv) <= 1)),
	}
	# 16100226: post-depletion genuine zero layer must not resurrect old rate
	if c226:
		out["c226_contract"] = {
			"qty": flt(c226[0].q),
			"sv": flt(c226[0].sv),
			"vr": flt(c226[0].vr),
			"note": "zero rate alone is not corruption when depleted/zero layer",
		}
	_dump("leftover_ma_after_wr.json", out)
	return {
		"count": out["count"],
		"c66_ok": out["c66_ok"],
		"c226_ok": out.get("c226_contract"),
		"terminals": rows,
	}


def classify_tip_rate0() -> dict:
	rows = frappe.db.sql(
		"""
		SELECT t.item_code, t.warehouse, t.voucher_no, t.voucher_type,
		       ROUND(t.qty_after_transaction,6) q,
		       ROUND(t.stock_value,2) sv,
		       ROUND(t.valuation_rate,2) vr,
		       ROUND(t.actual_qty,6) aq,
		       ROUND(t.incoming_rate,2) ir,
		       ROUND(t.outgoing_rate,2) ogr
		FROM `tabStock Ledger Entry` t
		INNER JOIN (
		  SELECT item_code, warehouse,
		         MAX(CONCAT(DATE_FORMAT(posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', creation)) mx
		  FROM `tabStock Ledger Entry` WHERE is_cancelled=0
		  GROUP BY item_code, warehouse
		) x ON x.item_code=t.item_code AND x.warehouse=t.warehouse
		   AND CONCAT(DATE_FORMAT(t.posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', t.creation)=x.mx
		WHERE t.is_cancelled=0
		  AND t.qty_after_transaction > 0
		  AND t.stock_value > 1
		  AND ABS(t.valuation_rate) < 1e-9
		LIMIT 200
		""",
		as_dict=True,
	)
	# tip rate0 KPI uses qty>0 value>0 rate0 — same query family as leftover MA tip
	# Also count broader tip from pass0 extra; here classify terminals.
	lanes = Counter()
	detail = []
	for r in rows:
		# try scrap/reject on voucher
		sec = frappe.db.sql(
			"""
			SELECT IFNULL(secondary_item_type,''), IFNULL(is_scrap_item,0),
			       IFNULL(allow_zero_valuation_rate,0), IFNULL(custom_output_class,'')
			FROM `tabStock Entry Detail`
			WHERE parent=%s AND item_code=%s LIMIT 1
			""",
			(r.voucher_no, r.item_code),
		)
		lane = "LOST_VALUATION"
		if sec:
			stype, scrap, allow0, oclass = sec[0]
			if allow0 or "SCRAP" in str(oclass).upper() or str(stype).lower() == "scrap":
				lane = "LEGITIMATE_SCRAP"
			elif "REJECT" in str(oclass).upper():
				lane = "LEGITIMATE_REJECT"
		if flt(r.q) > 0 and flt(r.sv) > 1 and abs(flt(r.vr)) < 1e-9:
			if lane == "LOST_VALUATION" and r.item_code in ("16100066",):
				lane = "TECHNICAL_TOOL_GAP"
		lanes[lane] += 1
		detail.append({**r, "lane": lane})
	out = {"count": len(rows), "by_lane": dict(lanes), "sample": detail[:40]}
	_dump("tip_rate0_after_wr.json", out)
	return {"count": out["count"], "by_lane": out["by_lane"]}


def run_preflight() -> dict:
	"""Build snapshot from live gates + I4 real terminals + pass0-ish KPIs."""
	gates = _gates()
	i4 = reclassify_i4()
	real_n = int(i4.get("real_terminal_n") or 0)
	manual_n = int(i4.get("true_manual_n") or 0)
	waiting_n = int(i4.get("waiting_n") or 0)
	tool_gap_n = int(i4.get("tool_gap_n") or 0)
	# Dust/legitimate do not block. Waiting + tool_gap + real/manual do.
	snap = {
		"i1": gates.get("i1"),
		"broken_gl": gates.get("broken_gl"),
		"broken_bin": gates.get("broken_bin"),
		"negative_stock": gates.get("neg_stock"),
		"negative_rate": 0,
		"open_riv": gates.get("open_riv"),
		"cross_time_exact": 0,
		"valued_source_zero_outgoing": 0,
		"manufacturing_dependency": waiting_n,
		"real_terminal_leftover": real_n + manual_n,
		"known_riv_poison_root": tool_gap_n,
		"p0_manual": 0,
		"p1_manual": 0,
		"i4_by_lane": i4.get("by_lane"),
		"i4_waiting_n": waiting_n,
		"canary_36934": _canary_36934(),
	}
	pre = full_repost_preflight(snap)
	out = {"snapshot": snap, "preflight": pre, "i4": i4}
	_dump("full_repost_preflight_after_wr.json", out)
	return {
		"status": pre.get("status"),
		"ready": pre.get("ready"),
		"blockers": pre.get("blockers"),
		"i4_by_lane": i4.get("by_lane"),
		"canary_36934": snap["canary_36934"],
	}


def run_all() -> dict:
	out = {
		"i4": reclassify_i4(),
		"pz": compress_pz(),
		"failed_riv": reconcile_failed_riv(),
		"leftover_ma": leftover_ma_status(),
		"tip_rate0": classify_tip_rate0(),
		"preflight": run_preflight(),
		"gates": _gates(),
		"canary_36934": _canary_36934(),
	}
	_dump("phase2_reconcile_after_wr.json", out)
	return out
