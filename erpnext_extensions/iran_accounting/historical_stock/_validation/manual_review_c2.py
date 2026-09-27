# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only Candidate-2 residual classification. Never writes SLE/GL/RIV."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import frappe
from frappe.utils import flt

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_225046_c2"
)
COMPANY = "اسپاد فارمد دارو"


def _jdump(name: str, obj) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def collect_manual_review() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import scan_leftover_ma
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	i4 = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=200)
	i4_rows = []
	for r in i4.get("rows") or []:
		i4_rows.append(
			{
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"status": r.get("i4_status") or r.get("planner_status") or r.get("status"),
				"qty_after": r.get("qty_after") or r.get("qty_after_transaction"),
				"stock_value": r.get("stock_value"),
				"valuation_rate": r.get("valuation_rate"),
				"reason": r.get("reason") or r.get("manual_reason") or r.get("classification"),
			}
		)

	po = run_full_history_scan(company=COMPANY)
	po_rows = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		po_rows.append(
			{
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"outbound": r.get("outbound_document") or r.get("voucher"),
				"inbound": r.get("inbound_document") or r.get("inbound"),
				"detection": r.get("detection"),
				"planner_status": r.get("planner_status"),
				"search_window": r.get("search_window"),
				"sql_updates": r.get("sql_updates"),
				"reason": r.get("reason"),
				"status": r.get("status") or r.get("optimizer_status"),
			}
		)

	fr = scan_failed_riv(company=COMPANY, limit=80)
	fr_actionable = []
	for r in fr.get("rows") or []:
		st = r.get("riv_reconcile_status") or ""
		if st in ("HISTORICAL_ONLY", "SUPERSEDED_BY_SUCCESSFUL_REPAIR"):
			continue
		fr_actionable.append(
			{
				"name": r.get("name"),
				"item": r.get("item_code") or r.get("item"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher_no"),
				"posting_date": str(r.get("posting_date") or ""),
				"reconcile": st,
				"riv_status": r.get("riv_status") or r.get("status"),
				"error": str(r.get("error_class") or r.get("error_log") or "")[:240],
			}
		)

	wrong = scan_wrong_rates(company=COMPANY, limit=5000)
	wr_by = Counter()
	wr_manual_reasons = Counter()
	wr_ready_ids = {}
	wr_manual_sample = []
	for raw in wrong.get("rows") or []:
		r = attach_plan(dict(raw))
		st = str(r.get("planner_status") or "")
		wr_by[st.split("_")[0] if st else "NONE"] += 1
		if st.startswith("READY"):
			key = (r.get("item") or r.get("item_code"), r.get("warehouse"), r.get("batch") or r.get("batch_no") or "")
			wr_ready_ids.setdefault(key, []).append(r.get("voucher") or r.get("voucher_no"))
		if st.startswith("MANUAL") or "MANUAL" in st:
			reason = r.get("manual_reason") or r.get("manual_lane") or r.get("source_of_truth") or "UNSPECIFIED"
			wr_manual_reasons[str(reason)] += 1
			if len(wr_manual_sample) < 25:
				wr_manual_sample.append(
					{
						"item": r.get("item") or r.get("item_code"),
						"warehouse": r.get("warehouse"),
						"voucher": r.get("voucher") or r.get("voucher_no"),
						"purpose": r.get("purpose"),
						"source": r.get("source_of_truth") or r.get("rate_source"),
						"manual_reason": r.get("manual_reason"),
						"manual_lane": r.get("manual_lane"),
						"confidence": r.get("confidence"),
					}
				)

	zero = scan_zero_rate_rows(company=COMPANY, limit=5000)
	zr_reasons = Counter()
	zr_legit = 0
	zr_sample = []
	for r in zero.get("rows") or []:
		prov = r.get("zero_provenance") or r.get("classification") or r.get("planner_status") or ""
		zr_reasons[str(prov)[:80]] += 1
		if str(r.get("planner_status") or "").startswith("LEGIT") or "LEGITIMATE" in str(prov).upper():
			zr_legit += 1
		if len(zr_sample) < 20 and not str(prov).upper().startswith("LEGIT"):
			zr_sample.append(
				{
					"item": r.get("item") or r.get("item_code"),
					"warehouse": r.get("warehouse"),
					"voucher": r.get("voucher") or r.get("voucher_no"),
					"qty": r.get("qty") or r.get("actual_qty"),
					"rate": r.get("valuation_rate"),
					"value": r.get("stock_value"),
					"provenance": str(prov)[:80],
					"status": r.get("planner_status"),
				}
			)

	try:
		lm = scan_leftover_ma(company=COMPANY, limit=50)
	except TypeError:
		lm = scan_leftover_ma()
	lm_rows = []
	for r in (lm.get("rows") or [])[:15]:
		lm_rows.append(
			{
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"status": r.get("planner_status") or r.get("status"),
				"qty": r.get("qty_after") or r.get("qty_after_transaction"),
				"rate": r.get("valuation_rate"),
				"value": r.get("stock_value"),
			}
		)

	rate0 = frappe.db.sql(
		"""SELECT sle.item_code, sle.warehouse, sle.voucher_no, sle.posting_datetime,
		          sle.qty_after_transaction, sle.stock_value, sle.valuation_rate, sle.actual_qty
		FROM `tabStock Ledger Entry` sle
		INNER JOIN (
			SELECT item_code, warehouse, MAX(CONCAT(posting_datetime, creation)) mx
			FROM `tabStock Ledger Entry` WHERE is_cancelled=0
			GROUP BY item_code, warehouse
		) x ON x.item_code=sle.item_code AND x.warehouse=sle.warehouse
		  AND CONCAT(sle.posting_datetime, sle.creation)=x.mx
		WHERE sle.is_cancelled=0
		  AND sle.qty_after_transaction > 0.0001
		  AND ABS(sle.stock_value) > 1
		  AND ABS(IFNULL(sle.valuation_rate,0)) <= 1e-6
		ORDER BY sle.item_code, sle.warehouse
		LIMIT 80""",
		as_dict=True,
	)

	out = {
		"i4_n": len(i4_rows),
		"i4": i4_rows,
		"po_n": len(po_rows),
		"po": po_rows,
		"failed_riv_actionable_n": len(fr_actionable),
		"failed_riv_by_reconcile": dict(Counter(r["reconcile"] for r in fr_actionable)),
		"failed_riv": fr_actionable,
		"wrong_status": dict(wr_by),
		"wrong_manual_reasons": dict(wr_manual_reasons),
		"wrong_ready_identities": len(wr_ready_ids),
		"wrong_manual_sample": wr_manual_sample,
		"zero_reasons": dict(zr_reasons),
		"zero_legit": zr_legit,
		"zero_sample": zr_sample,
		"leftover_ma": lm_rows,
		"terminal_rate0_n": len(rate0),
		"terminal_rate0": rate0[:40],
	}
	_jdump("manual_review_raw.json", out)
	return {
		"i4_n": out["i4_n"],
		"po_n": out["po_n"],
		"failed_riv_n": out["failed_riv_actionable_n"],
		"failed_riv_by": out["failed_riv_by_reconcile"],
		"wrong_status": out["wrong_status"],
		"wrong_manual_reasons": out["wrong_manual_reasons"],
		"wrong_ready_identities": out["wrong_ready_identities"],
		"zero_reason_n": len(zr_reasons),
		"leftover_ma_n": len(lm_rows),
		"terminal_rate0_n": len(rate0),
	}
